# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

import uuid
from unittest.mock import patch

import pytest
from django.utils import timezone

from plane.db.models import Page, PageVersion, Project, ProjectMember, ProjectPage, User, Workspace, WorkspaceMember
from plane.api.page_locks import page_lock_redis

pytestmark = [pytest.mark.django_db, pytest.mark.contract]


@pytest.fixture
def project(workspace, create_user):
    project = Project.objects.create(workspace=workspace, name="API", identifier="API")
    ProjectMember.objects.create(workspace=workspace, project=project, member=create_user, role=20)
    return project


def url(workspace, project=None, page=None, archive=False):
    base = f"/api/v1/workspaces/{workspace.slug}/"
    if project:
        base += f"projects/{project.id}/"
    base += "pages/"
    if page:
        base += f"{page.id}/"
    return base + ("archive/" if archive else "")


def make_page(workspace, owner, project=None, **kwargs):
    page = Page.objects.create(workspace=workspace, owned_by=owner, is_global=project is None, **kwargs)
    if project:
        ProjectPage.objects.create(workspace=workspace, project=project, page=page)
    return page


@pytest.mark.parametrize("project_scope", [False, True])
def test_lifecycle(api_key_client, workspace, project, create_user, project_scope):
    scope = project if project_scope else None
    response = api_key_client.post(
        url(workspace, scope), {"name": "SDK", "description_html": "<p>Original</p>"}, format="json"
    )
    assert response.status_code == 201, response.data
    page = Page.objects.get(pk=response.data["id"])
    assert page.is_global == (not project_scope)
    assert page.owned_by == create_user
    assert page.description_stripped == "Original"
    listing = api_key_client.get(url(workspace, scope))
    assert listing.status_code == 200, listing.data
    assert str(page.id) in {item["id"] for item in listing.data["results"]}
    detail = url(workspace, scope, page)
    assert api_key_client.get(detail).data["description_html"] == "<p>Original</p>"
    assert api_key_client.delete(detail).status_code == 400
    # Emulate a document previously edited in the browser.
    page.description_binary = b"old-yjs-document"
    page.save()
    response = api_key_client.put(detail, {"name": "Updated", "description_html": "<p>New</p>"}, format="json")
    assert response.status_code == 200, response.data
    page.refresh_from_db()
    assert page.description_html == "<p>New</p>"
    assert page.description_binary is None
    assert bytes(PageVersion.objects.get(page=page).description_binary) == b"old-yjs-document"
    assert api_key_client.post(url(workspace, scope, page, True)).status_code == 204
    assert api_key_client.put(detail, {"name": "blocked"}, format="json").status_code == 400
    assert api_key_client.delete(url(workspace, scope, page, True)).status_code == 204
    assert api_key_client.post(url(workspace, scope, page, True)).status_code == 204
    with patch("celery.app.task.Task.delay"):
        assert api_key_client.delete(detail).status_code == 204
    assert api_key_client.get(detail).status_code == 404


def test_page_scope_and_privacy(api_key_client, workspace, project, create_user):
    other = User.objects.create(email="other@plane.test", username=f"other-{uuid.uuid4().hex}")
    private = make_page(workspace, other, project, access=1)
    assert api_key_client.get(url(workspace, project, private)).status_code == 404
    foreign_project = Project.objects.create(workspace=workspace, name="Private", identifier="PRIV")
    foreign = make_page(workspace, other, foreign_project)
    assert api_key_client.get(url(workspace, project, foreign)).status_code == 404
    assert api_key_client.get(url(workspace, foreign_project)).status_code == 403
    # A project page must never be accessible through the workspace-global route.
    assert api_key_client.get(url(workspace, page=foreign)).status_code == 404
    assert api_key_client.get(url(workspace)).data["results"] == []
    removed = make_page(workspace, create_user, project)
    ProjectPage.objects.filter(page=removed).update(deleted_at=timezone.now())
    assert api_key_client.get(url(workspace, project, removed)).status_code == 404


def test_cross_workspace_and_inactive_membership(api_key_client, workspace, project, create_user):
    elsewhere = Workspace.objects.create(name="Elsewhere", slug="elsewhere", owner=create_user)
    page = make_page(elsewhere, create_user)
    assert api_key_client.get(url(workspace, page=page)).status_code == 404
    assert api_key_client.get(url(elsewhere)).status_code == 403
    WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(is_active=False)
    assert api_key_client.get(url(workspace, project)).status_code == 403


def test_guests_cannot_write_even_own_pages(api_key_client, workspace, project, create_user):
    ProjectMember.objects.filter(project=project, member=create_user).update(role=5)
    page = make_page(workspace, create_user, project)
    assert api_key_client.get(url(workspace, project, page)).status_code == 200
    assert (
        api_key_client.post(url(workspace, project), {"name": "No", "description_html": ""}, format="json").status_code
        == 403
    )
    assert api_key_client.put(url(workspace, project, page), {"name": "No"}, format="json").status_code == 403


def test_locked_and_invalid_updates(api_key_client, workspace, create_user):
    page = make_page(workspace, create_user, is_locked=True)
    assert api_key_client.put(url(workspace, page=page), {"description_html": "new"}, format="json").status_code == 400
    assert api_key_client.put(url(workspace, page=page), {}, format="json").status_code == 400
    assert api_key_client.get(url(workspace), {"order_by": "owned_by__password"}).status_code == 400


def test_live_document_lock_prevents_content_loss(api_key_client, workspace, create_user):
    page = make_page(workspace, create_user, description_html="<p>Keep me</p>")
    redis = page_lock_redis()
    key = f"plane:page-write:{page.id}"
    redis.set(key, "live:test-editor")
    try:
        assert (
            api_key_client.put(url(workspace, page=page), {"description_html": "<p>New</p>"}, format="json").status_code
            == 409
        )
        page.refresh_from_db()
        assert page.description_html == "<p>Keep me</p>"
        assert redis.get(key) == b"live:test-editor"
    finally:
        redis.delete(key)
    assert (
        api_key_client.put(url(workspace, page=page), {"description_html": "<p>New</p>"}, format="json").status_code
        == 200
    )
    assert redis.get(key) is None


def test_children_are_not_silently_modified(api_key_client, workspace, create_user):
    parent = make_page(workspace, create_user)
    child = make_page(workspace, create_user, parent=parent)
    assert api_key_client.post(url(workspace, page=parent, archive=True)).status_code == 400
    child.refresh_from_db()
    assert child.archived_at is None


def test_unauthenticated_and_invalid_key(api_client, workspace):
    assert api_client.get(url(workspace)).status_code in (401, 403)
    api_client.credentials(HTTP_X_API_KEY="invalid")
    assert api_client.get(url(workspace)).status_code in (401, 403)


def test_pagination(api_key_client, workspace, create_user):
    for name in ("A", "B", "C"):
        make_page(workspace, create_user, name=name)
    first = api_key_client.get(url(workspace), {"per_page": 2, "order_by": "name"})
    assert first.status_code == 200, first.data
    assert [page["name"] for page in first.data["results"]] == ["A", "B"]
    assert first.data["next_page_results"] is True
    second = api_key_client.get(
        url(workspace), {"per_page": 2, "order_by": "name", "cursor": first.data["next_cursor"]}
    )
    assert [page["name"] for page in second.data["results"]] == ["C"]
    assert api_key_client.get(url(workspace), {"per_page": 101}).status_code == 400


def test_cache_clear_does_not_remove_page_locks():
    from django.core.cache import cache

    redis = page_lock_redis()
    key = "plane:page-write:cache-clear-test"
    redis.set(key, "live:test")
    try:
        cache.clear()
        assert redis.get(key) == b"live:test"
    finally:
        redis.delete(key)


def test_rename_invalidates_cached_editor_title(api_key_client, workspace, create_user):
    page = make_page(
        workspace, create_user, name="Old", description_binary=b"old title and body", description_html="<p>Body</p>"
    )
    response = api_key_client.put(url(workspace, page=page), {"name": "New"}, format="json")
    assert response.status_code == 200
    page.refresh_from_db()
    assert page.name == "New"
    assert page.description_html == "<p>Body</p>"
    assert page.description_binary is None
    assert bytes(PageVersion.objects.get(page=page).description_binary) == b"old title and body"
