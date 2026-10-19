# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# Imported pytest fixtures are intentionally also named in test parameters.
# ruff: noqa: F811

import uuid
from unittest.mock import patch

import pytest
from django.utils import timezone
from django.db import IntegrityError, transaction

from plane.db.models import Issue, IssuePage, Project, ProjectMember, ProjectPage, User, Workspace, WorkspaceMember
from plane.tests.contract.api.test_pages import make_page, project  # noqa: F401

pytestmark = [pytest.mark.django_db, pytest.mark.contract]


@pytest.fixture(autouse=True)
def no_external_tasks(settings):
    settings.WEB_URL = "http://testserver"
    with patch("celery.app.task.Task.delay"):
        yield


@pytest.fixture
def work_item(workspace, project):
    return Issue.objects.create(workspace=workspace, project=project, name="Anchor")


def url(workspace, project, work_item, association=None):
    base = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/work-items/{work_item.id}/pages/"
    return base + (f"{association}/" if association else "")


def test_attachment_lifecycle(api_key_client, workspace, project, work_item, create_user):
    page = make_page(workspace, create_user, project, name="Artifact", description_html="<p>Read me</p>")
    collection = url(workspace, project, work_item)
    response = api_key_client.post(collection, {"page_id": str(page.id)}, format="json")
    assert response.status_code == 201, response.content
    association = response.json()
    assert association["id"] != str(page.id)
    assert association["page"]["id"] == str(page.id)
    assert association["page"]["description_html"] == "<p>Read me</p>"
    assert association["issue"] == str(work_item.id)
    assert association["project"] == str(project.id)
    assert association["workspace"] == str(workspace.id)
    assert association["created_by"] == str(create_user.id)
    assert association["created_at"] and association["updated_at"]
    assert api_key_client.get(collection).json()["results"] == [association]
    detail = url(workspace, project, work_item, association["id"])
    assert api_key_client.get(detail).json() == association
    assert api_key_client.get(url(workspace, project, work_item, page.id)).status_code == 404
    assert api_key_client.delete(detail).status_code == 204
    assert api_key_client.get(collection).json()["results"] == []
    assert api_key_client.get(detail).status_code == 404
    assert api_key_client.delete(detail).status_code == 404


def attach(client, workspace, project, work_item, page):
    return client.post(url(workspace, project, work_item), {"page_id": str(page.id)}, format="json")


def test_attach_is_idempotent_and_reattach_after_detach(api_key_client, workspace, project, work_item, create_user):
    page = make_page(workspace, create_user, project)
    first = attach(api_key_client, workspace, project, work_item, page)
    second = attach(api_key_client, workspace, project, work_item, page)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json() == second.json()
    assert len(api_key_client.get(url(workspace, project, work_item)).json()["results"]) == 1
    assert api_key_client.delete(url(workspace, project, work_item, first.json()["id"])).status_code == 204
    third = attach(api_key_client, workspace, project, work_item, page)
    assert third.status_code == 201
    assert third.json()["id"] != first.json()["id"]
    assert api_key_client.delete(url(workspace, project, work_item, first.json()["id"])).status_code == 404


def test_detach_without_attach(api_key_client, workspace, project, work_item):
    assert api_key_client.delete(url(workspace, project, work_item, uuid.uuid4())).status_code == 404


def test_cursor_pagination(api_key_client, workspace, project, work_item, create_user):
    ids = set()
    for name in ("First", "Second", "Third"):
        page = make_page(workspace, create_user, project, name=name)
        ids.add(attach(api_key_client, workspace, project, work_item, page).json()["id"])
    collection = url(workspace, project, work_item)
    first = api_key_client.get(collection, {"per_page": 2})
    assert first.status_code == 200
    assert {
        "next_cursor",
        "prev_cursor",
        "next_page_results",
        "prev_page_results",
        "count",
        "total_pages",
        "total_results",
        "results",
    } <= first.json().keys()
    assert first.json()["count"] == 2
    assert first.json()["total_results"] == 3
    assert first.json()["total_pages"] == 2
    assert first.json()["next_page_results"] is True
    second = api_key_client.get(collection, {"per_page": 2, "cursor": first.json()["next_cursor"]})
    assert second.status_code == 200
    assert second.json()["count"] == 1
    assert second.json()["next_page_results"] is False
    assert second.json()["prev_page_results"] is True
    assert {row["id"] for row in first.json()["results"] + second.json()["results"]} == ids
    for per_page in (0, -1, 101, "bad"):
        assert api_key_client.get(collection, {"per_page": per_page}).status_code == 400
    assert api_key_client.get(collection, {"cursor": "bad"}).status_code == 400


def test_archived_and_deleted_pages_are_hidden(api_key_client, workspace, project, work_item, create_user):
    page = make_page(workspace, create_user, project)
    association = attach(api_key_client, workspace, project, work_item, page).json()["id"]
    detail = url(workspace, project, work_item, association)
    page.archived_at = timezone.localdate()
    page.save()
    assert attach(api_key_client, workspace, project, work_item, page).status_code == 404
    assert api_key_client.get(url(workspace, project, work_item)).json()["results"] == []
    assert api_key_client.get(detail).status_code == 404
    assert api_key_client.delete(detail).status_code == 404
    page.archived_at = None
    page.deleted_at = timezone.now()
    page.save()
    assert api_key_client.get(url(workspace, project, work_item)).json()["results"] == []
    assert api_key_client.get(detail).status_code == 404
    assert attach(api_key_client, workspace, project, work_item, page).status_code == 404


def test_scope_excludes_other_projects_workspaces_and_global_pages(
    api_key_client, workspace, project, work_item, create_user
):
    elsewhere = Workspace.objects.create(name="Elsewhere", slug="elsewhere", owner=create_user)
    other_project = Project.objects.create(workspace=workspace, name="Other", identifier="OTHER")
    pages = [
        make_page(workspace, create_user, other_project),
        make_page(elsewhere, create_user),
        make_page(workspace, create_user),
    ]
    # Even an explicit project link does not turn a global Page into a project Page.
    ProjectPage.objects.create(workspace=workspace, project=project, page=pages[-1])
    for page in pages:
        assert attach(api_key_client, workspace, project, work_item, page).status_code == 404
    assert api_key_client.get(url(workspace, project, work_item)).json()["results"] == []


def test_private_page_visibility_and_visible_write_denial(api_key_client, workspace, project, work_item, create_user):
    page = make_page(workspace, create_user, project, access=1)
    association = attach(api_key_client, workspace, project, work_item, page).json()["id"]
    detail = url(workspace, project, work_item, association)
    ProjectMember.objects.filter(project=project, member=create_user).update(role=5)
    assert api_key_client.get(detail).status_code == 200
    assert attach(api_key_client, workspace, project, work_item, page).status_code == 403
    assert api_key_client.delete(detail).status_code == 403
    other = User.objects.create(email="private@plane.test", username="private-owner")
    page.owned_by = other
    page.save()
    for role in (5, 15, 20):
        ProjectMember.objects.filter(project=project, member=create_user).update(role=role)
        assert attach(api_key_client, workspace, project, work_item, page).status_code == 404
        assert api_key_client.get(url(workspace, project, work_item)).json()["results"] == []
        assert api_key_client.get(detail).status_code == 404
        assert api_key_client.delete(detail).status_code == 404


def test_association_is_scoped_to_item_and_membership(api_key_client, workspace, project, work_item, create_user):
    page = make_page(workspace, create_user, project)
    association = attach(api_key_client, workspace, project, work_item, page).json()["id"]
    other = Issue.objects.create(workspace=workspace, project=project, name="Other")
    assert api_key_client.get(url(workspace, project, other, association)).status_code == 404
    assert api_key_client.delete(url(workspace, project, other, association)).status_code == 404
    ProjectMember.objects.filter(project=project, member=create_user).update(is_active=False)
    assert api_key_client.get(url(workspace, project, work_item)).status_code == 403
    assert attach(api_key_client, workspace, project, work_item, page).status_code == 403
    ProjectMember.objects.filter(project=project, member=create_user).update(is_active=True)
    WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(is_active=False)
    assert api_key_client.get(url(workspace, project, work_item)).status_code == 403


def test_deleted_work_item_is_missing(api_key_client, workspace, project, work_item, create_user):
    page = make_page(workspace, create_user, project)
    association = attach(api_key_client, workspace, project, work_item, page).json()["id"]
    work_item.deleted_at = timezone.now()
    work_item.save()
    assert api_key_client.get(url(workspace, project, work_item)).status_code == 404
    assert attach(api_key_client, workspace, project, work_item, page).status_code == 404
    assert api_key_client.get(url(workspace, project, work_item, association)).status_code == 404
    assert api_key_client.delete(url(workspace, project, work_item, association)).status_code == 404


def test_input_auth_and_unknown_path(api_key_client, api_client, workspace, project, work_item):
    collection = url(workspace, project, work_item)
    for body in ({}, {"page_id": "bad"}, {"page_id": None}):
        assert api_key_client.post(collection, body, format="json").status_code == 400
    api_client.credentials()
    assert api_client.get(collection).status_code in (401, 403)
    api_client.credentials(HTTP_X_API_KEY="invalid")
    assert api_client.get(collection).status_code in (401, 403)
    assert api_client.get("/api/v1/not-a-real-route/").json() == {"error": "Page not found."}


def test_live_pair_constraint_and_deleted_association_filter(
    api_key_client, workspace, project, work_item, create_user
):
    page = make_page(workspace, create_user, project)
    attached = attach(api_key_client, workspace, project, work_item, page).json()
    # Exercise the database invariant independently of the endpoint's get_or_create.
    with pytest.raises(IntegrityError), transaction.atomic():
        IssuePage.objects.create(issue=work_item, page=page, project=project)
    IssuePage.objects.filter(pk=attached["id"]).delete()
    assert api_key_client.get(url(workspace, project, work_item)).json()["results"] == []
    assert api_key_client.delete(url(workspace, project, work_item, attached["id"])).status_code == 404
    assert attach(api_key_client, workspace, project, work_item, page).status_code == 201


def test_global_and_removed_project_links_are_hidden_for_existing_associations(
    api_key_client, workspace, project, work_item, create_user
):
    page = make_page(workspace, create_user, project)
    association = attach(api_key_client, workspace, project, work_item, page).json()["id"]
    detail = url(workspace, project, work_item, association)
    page.is_global = True
    page.save()
    assert api_key_client.get(url(workspace, project, work_item)).json()["results"] == []
    assert api_key_client.get(detail).status_code == 404
    assert api_key_client.delete(detail).status_code == 404
    page.is_global = False
    page.save()
    ProjectPage.objects.filter(project=project, page=page).update(deleted_at=timezone.now())
    assert attach(api_key_client, workspace, project, work_item, page).status_code == 404
    assert api_key_client.get(url(workspace, project, work_item)).json()["results"] == []
    assert api_key_client.get(detail).status_code == 404
    assert api_key_client.delete(detail).status_code == 404
