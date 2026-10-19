# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# Imported pytest fixtures are intentionally also named in test parameters.
# ruff: noqa: F811

import uuid

import pytest

from plane.db.models import Issue, IssueRelation, Project, ProjectMember
from plane.tests.contract.api.test_work_item_pages import no_external_tasks, project, work_item  # noqa: F401

pytestmark = [pytest.mark.django_db, pytest.mark.contract]


def url(workspace, project, item, remove=False):
    base = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/work-items/{item.id}/relations/"
    return base + ("remove/" if remove else "")


@pytest.mark.parametrize(
    "relation_type",
    [
        "blocked_by",
        "blocking",
        "duplicate",
        "relates_to",
        "start_after",
        "start_before",
        "finish_after",
        "finish_before",
    ],
)
def test_removal_from_either_direction(api_key_client, workspace, project, work_item, relation_type):
    other = Issue.objects.create(workspace=workspace, project=project, name="Other")
    created = api_key_client.post(
        url(workspace, project, work_item), {"relation_type": relation_type, "issues": [str(other.id)]}, format="json"
    )
    assert created.status_code == 201, created.content
    for item in (work_item, other):
        assert any(api_key_client.get(url(workspace, project, item)).json().values())
    # Alternate which endpoint removes the relation, including reverse relations.
    anchor, related = (
        (other, work_item)
        if relation_type in ("blocking", "start_after", "finish_after", "duplicate")
        else (work_item, other)
    )
    removed = api_key_client.post(
        url(workspace, project, anchor, True), {"related_issue": str(related.id)}, format="json"
    )
    assert removed.status_code == 204, removed.content
    for item in (work_item, other):
        assert not any(api_key_client.get(url(workspace, project, item)).json().values())
    # A missing relation on two existing items is an idempotent success.
    assert (
        api_key_client.post(
            url(workspace, project, anchor, True), {"related_issue": str(related.id)}, format="json"
        ).status_code
        == 204
    )


@pytest.mark.parametrize("from_reverse", [False, True])
def test_removes_both_stored_directions_without_touching_other_edges(
    api_key_client, workspace, project, work_item, from_reverse
):
    other = Issue.objects.create(workspace=workspace, project=project, name="Other")
    retained = Issue.objects.create(workspace=workspace, project=project, name="Retain")
    for a, b in ((work_item, other), (other, work_item), (work_item, retained)):
        IssueRelation.objects.create(
            workspace=workspace, project=project, issue=a, related_issue=b, relation_type="blocked_by"
        )
    anchor, related = (other, work_item) if from_reverse else (work_item, other)
    assert (
        api_key_client.post(
            url(workspace, project, anchor, True), {"related_issue": str(related.id)}, format="json"
        ).status_code
        == 204
    )
    assert not any(api_key_client.get(url(workspace, project, other)).json().values())
    remaining = api_key_client.get(url(workspace, project, work_item)).json()
    assert remaining["blocked_by"] == [{"issue_id": str(retained.id), "project_id": str(project.id)}]
    assert api_key_client.get(url(workspace, project, retained)).json()["blocking"] == [
        {"issue_id": str(work_item.id), "project_id": str(project.id)}
    ]


def test_missing_relation_invalid_input_scope_and_permissions(
    api_key_client, workspace, project, work_item, create_user
):
    other = Issue.objects.create(workspace=workspace, project=project, name="Other")
    endpoint = url(workspace, project, work_item, True)
    assert api_key_client.post(endpoint, {"related_issue": str(other.id)}, format="json").status_code == 204
    for body in ({}, {"related_issue": "bad"}, {"related_issue": None}):
        assert api_key_client.post(endpoint, body, format="json").status_code == 400
    assert api_key_client.post(endpoint, {"related_issue": str(uuid.uuid4())}, format="json").status_code == 404
    foreign_project = Project.objects.create(workspace=workspace, name="Foreign", identifier="FOREIGN")
    ProjectMember.objects.create(workspace=workspace, project=foreign_project, member=create_user, role=20)
    assert (
        api_key_client.post(
            url(workspace, foreign_project, work_item, True), {"related_issue": str(other.id)}, format="json"
        ).status_code
        == 404
    )
    ProjectMember.objects.filter(project=project, member=create_user).update(role=5)
    assert api_key_client.post(endpoint, {"related_issue": str(other.id)}, format="json").status_code == 403
    ProjectMember.objects.filter(project=project, member=create_user).update(is_active=False)
    assert api_key_client.post(endpoint, {"related_issue": str(other.id)}, format="json").status_code == 403
