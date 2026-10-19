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
    assert set(association["page"]) == {
        "id",
        "name",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
        "archived_at",
        "is_global",
        "access",
        "url",
    }
    assert association["page"]["url"] == f"http://testserver/{workspace.slug}/projects/{project.id}/pages/{page.id}"
    assert len(response.content) <= 1024
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


def test_full_content_opt_in_preserves_legacy_payload(api_key_client, workspace, project, work_item, create_user):
    page = make_page(
        workspace, create_user, project, description_html="<p>Read me</p>", description_json={"doc": "keep"}
    )
    project_url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/pages/{page.id}/"
    legacy_page = api_key_client.get(project_url).json()
    collection = url(workspace, project, work_item)
    first = api_key_client.post(collection + "?with_content=true", {"page_id": str(page.id)}, format="json")
    assert first.status_code == 201
    assert first.json()["page"] == legacy_page
    detail = url(workspace, project, work_item, first.json()["id"])
    for params in ({"with_content": "true"}, {"with_content": "true", "body_format": "html"}):
        assert api_key_client.get(detail, params).json()["page"] == legacy_page
        assert api_key_client.get(collection, params).json()["results"][0]["page"] == legacy_page
    duplicate = api_key_client.post(collection + "?with_content=true", {"page_id": str(page.id)}, format="json")
    assert duplicate.status_code == 200
    assert duplicate.json() == first.json()


PREAMBLE = "<p>Intro 🏠 &amp; café</p>\n"
SUMMARY = '<H2 class="editor-heading-block" DATA-ID="summary">Executive <em>summary</em></H2><p>Root 🍀</p>'
CHILD = '<h3 data-id="detail">Detail</h3><p>Child</p>'
DEEP = "<h4>No id</h4><p>Deep</p>"
NEXT = '<h2 data-id="next">Next</h2><p>End</p>'
DOCUMENT = PREAMBLE + SUMMARY + CHILD + DEEP + NEXT
ROOT_TEXT = "Executive summaryRoot 🍀DetailChildNo idDeep"


@pytest.fixture
def document(api_key_client, workspace, project, work_item, create_user):
    page = make_page(workspace, create_user, project, name="Structured", description_html=DOCUMENT)
    association = attach(api_key_client, workspace, project, work_item, page).json()
    return page, url(workspace, project, work_item), url(workspace, project, work_item, association["id"])


def test_outline_and_sections_preserve_heading_hierarchy_and_markup(api_key_client, document):
    page, collection, detail = document
    outline = api_key_client.get(detail, {"sections": "true"}).json()["page"]
    assert outline["section_count"] == 5
    assert outline["total_chars"] == len("Intro 🏠 & café\n" + ROOT_TEXT + "NextEnd")
    assert outline["outline"] == [
        {"id": "preamble", "level": 0, "title": "", "text_chars": len("Intro 🏠 & café\n")},
        {"id": "summary", "level": 2, "title": "Executive summary", "text_chars": len(ROOT_TEXT)},
        {"id": "detail", "level": 3, "title": "Detail", "text_chars": len("DetailChildNo idDeep")},
        {"id": "s3", "level": 4, "title": "No id", "text_chars": len("No idDeep")},
        {"id": "next", "level": 2, "title": "Next", "text_chars": len("NextEnd")},
    ]
    assert not {"body", "description_html", "description_json", "description_stripped"}.intersection(outline)
    listed = api_key_client.get(collection, {"sections": "true"}).json()["results"][0]["page"]
    assert listed == outline
    result = api_key_client.get(detail, {"section": ["summary", "preamble", "s3"]}).json()["page"]
    assert [row["id"] for row in result["sections"]] == ["summary", "preamble", "s3"]
    assert result["sections"][0] == {
        "id": "summary",
        "level": 2,
        "title": "Executive summary",
        "body": SUMMARY + CHILD + DEEP,
        "body_format": "html",
        "text_chars": len(ROOT_TEXT),
        "total_chars": len(SUMMARY + CHILD + DEEP),
        "truncated": False,
    }
    assert result["sections"][1]["body"] == PREAMBLE
    assert result["sections"][2]["body"] == DEEP
    assert "Next" not in result["sections"][0]["body"]
    assert not {"outline", "body", "description_html", "description_json", "description_stripped"}.intersection(result)


@pytest.mark.parametrize("html,text", [("<p>Only intro</p>", "Only intro"), ("", "")])
def test_no_headings_is_one_reachable_preamble(api_key_client, workspace, project, work_item, create_user, html, text):
    page = make_page(workspace, create_user, project, description_html=html)
    association = attach(api_key_client, workspace, project, work_item, page).json()
    detail = url(workspace, project, work_item, association["id"])
    data = api_key_client.get(detail, {"sections": "true"}).json()["page"]
    assert data["section_count"] == 1
    assert data["outline"] == [{"id": "preamble", "level": 0, "title": "", "text_chars": len(text)}]
    result = api_key_client.get(detail, {"section": "preamble", "body_format": "text"}).json()["page"]["sections"][0]
    assert result["body"] == text
    assert result["total_chars"] == len(text)


def test_section_text_and_unicode_truncation(api_key_client, document):
    page, collection, detail = document
    full = api_key_client.get(detail, {"section": "summary", "body_format": "text"}).json()["page"]["sections"][0]
    assert full["body"] == ROOT_TEXT
    assert full["text_chars"] == full["total_chars"] == len(ROOT_TEXT)
    params = {"section": "preamble", "body_format": "text", "limit_chars": 7}
    truncated = api_key_client.get(detail, params).json()["page"]["sections"][0]
    assert truncated["body"] == "Intro 🏠"
    assert truncated["truncated"] is True
    assert truncated["text_chars"] == truncated["total_chars"] == len("Intro 🏠 & café\n")
    assert truncated["body"].encode().decode() == "Intro 🏠"
    html = api_key_client.get(detail, {"section": "summary", "limit_chars": 10}).json()["page"]["sections"][0]
    assert html["body"] == SUMMARY[:10]
    assert html["total_chars"] == len(SUMMARY + CHILD + DEEP)
    assert html["truncated"] is True
    exact = api_key_client.get(detail, {"section": "summary", "limit_chars": len(SUMMARY + CHILD + DEEP)}).json()[
        "page"
    ]["sections"][0]
    assert exact["truncated"] is False


def test_full_text_and_full_truncation_do_not_leak_other_body_formats(api_key_client, document):
    page, collection, detail = document
    expected = "Intro 🏠 & café\n" + ROOT_TEXT + "NextEnd"
    text = api_key_client.get(detail, {"with_content": "true", "body_format": "text"}).json()["page"]
    assert text["body"] == expected == page.description_stripped
    assert text["total_chars"] == text["text_chars"] == len(expected)
    assert text["truncated"] is False
    assert not {"description_html", "description_json", "description_stripped"}.intersection(text)
    capped = api_key_client.get(detail, {"with_content": "true", "limit_chars": 9}).json()["page"]
    assert capped["body"] == DOCUMENT[:9]
    assert capped["body_format"] == "html"
    assert capped["total_chars"] == len(DOCUMENT)
    assert capped["text_chars"] == len(expected)
    assert capped["truncated"] is True
    assert not {"description_html", "description_json", "description_stripped"}.intersection(capped)


INVALID_CONTENT = [
    ({"with_content": "yes"}, "with_content"),
    ({"with_content": "TRUE"}, "with_content"),
    ({"with_content": ""}, "with_content"),
    ({"sections": "1"}, "sections"),
    ({"sections": "FALSE"}, "sections"),
    ({"with_content": "true", "sections": "true"}, "sections"),
    ({"with_content": "true", "sections": "false"}, "sections"),
    ({"with_content": "true", "section": "summary"}, "section"),
    ({"sections": "true", "section": "summary"}, "section"),
    ({"sections": "true", "body_format": "html"}, "body_format"),
    ({"sections": "true", "limit_chars": "500"}, "limit_chars"),
    ({"body_format": "html"}, "body_format"),
    ({"with_content": "false", "body_format": "text"}, "body_format"),
    ({"limit_chars": "500"}, "limit_chars"),
    ({"sections": "false", "limit_chars": "500"}, "limit_chars"),
    ({"section": "summary", "body_format": "markdown"}, "body_format"),
    ({"with_content": "true", "body_format": "HTML"}, "body_format"),
    ({"section": "summary", "limit_chars": "0"}, "limit_chars"),
    ({"section": "summary", "limit_chars": "-1"}, "limit_chars"),
    ({"section": "summary", "limit_chars": "100001"}, "limit_chars"),
    ({"section": "summary", "limit_chars": "1.5"}, "limit_chars"),
    ({"section": "summary", "limit_chars": "no"}, "limit_chars"),
    ({"section": "summary", "limit_chars": "9" * 4400}, "limit_chars"),
    ({"section": ""}, "section"),
    ({"section": ["summary"] * 11}, "section"),
    ({"with_content": ["true", "false"]}, "with_content"),
    ({"body_format": ["html", "text"], "section": "summary"}, "body_format"),
    ({"limit_chars": ["1", "2"], "section": "summary"}, "limit_chars"),
    ({"sections": ["true", "false"]}, "sections"),
]


@pytest.mark.parametrize("operation", ["list", "retrieve", "attach"])
@pytest.mark.parametrize("params,field", INVALID_CONTENT)
def test_invalid_content_query_is_a_field_error(
    api_key_client, workspace, project, work_item, document, operation, params, field
):
    from urllib.parse import urlencode

    page, collection, detail = document
    if operation == "attach":
        response = api_key_client.post(
            collection + "?" + urlencode(params, doseq=True), {"page_id": str(page.id)}, format="json"
        )
    else:
        response = api_key_client.get(collection if operation == "list" else detail, params)
    assert response.status_code == 400, response.content
    assert field in response.json(), response.json()
    assert response.json()[field]


@pytest.mark.parametrize("operation", ["list", "retrieve", "attach"])
def test_unknown_section_ids_are_named_and_failed_attach_rolls_back(
    api_key_client, workspace, project, work_item, create_user, document, operation
):
    page, collection, detail = document
    if operation == "attach":
        fresh = make_page(workspace, create_user, project, description_html=DOCUMENT)
        response = api_key_client.post(
            collection + "?section=missing-one&section=missing-two", {"page_id": str(fresh.id)}, format="json"
        )
        assert str(fresh.id) not in {row["page"]["id"] for row in api_key_client.get(collection).json()["results"]}
    else:
        response = api_key_client.get(
            collection if operation == "list" else detail, {"section": ["missing-one", "missing-two"]}
        )
    assert response.status_code == 400, response.content
    assert "section" in response.json()
    assert "missing-one" in str(response.json()["section"])
    assert "missing-two" in str(response.json()["section"])


def test_false_switches_limits_and_repeatable_sections(api_key_client, document):
    page, collection, detail = document
    light = api_key_client.get(detail).json()
    for params in ({"with_content": "false"}, {"sections": "false"}, {"with_content": "false", "sections": "false"}):
        assert api_key_client.get(detail, params).json() == light
    assert (
        api_key_client.get(detail, {"with_content": "false", "sections": "true"}).json()["page"]["section_count"] == 5
    )
    sections = api_key_client.get(
        detail, {"sections": "false", "section": ["next"] * 10, "limit_chars": 100000}
    ).json()["page"]["sections"]
    assert len(sections) == 1
    assert sections[0]["id"] == "next"
    assert sections[0]["truncated"] is False


def test_project_page_reads_keep_defaults_and_support_explicit_content_modes(
    api_key_client, workspace, project, document
):
    page, collection, detail = document
    base = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/pages/"
    legacy = api_key_client.get(base + f"{page.id}/").json()
    assert legacy["description_html"] == DOCUMENT
    assert "description_json" in legacy
    assert "url" not in legacy
    assert api_key_client.get(base).json()["results"][0] == legacy
    for params in ({"with_content": "false"}, {"sections": "true"}, {"section": "summary", "body_format": "text"}):
        expected = api_key_client.get(detail, params).json()["page"]
        assert api_key_client.get(base + f"{page.id}/", params).json() == expected
        assert api_key_client.get(base, params).json()["results"][0] == expected
    assert api_key_client.get(base + f"{page.id}/", {"body_format": "text"}).status_code == 400
    assert api_key_client.get(base, {"limit_chars": 500}).status_code == 400


def test_missing_resource_messages_and_visibility_order_are_unchanged(
    api_key_client, workspace, project, work_item, create_user
):
    missing = api_key_client.get(url(workspace, project, work_item, uuid.uuid4()))
    assert missing.status_code == 404
    assert missing.json() == {"detail": "No IssuePage matches the given query."}
    assert api_key_client.get("/api/v1/not-a-real-route/").json() == {"error": "Page not found."}
    private_owner = User.objects.create(email="hidden-section@plane.test", username="hidden-section")
    private = make_page(workspace, private_owner, project, access=1, description_html=DOCUMENT)
    ProjectMember.objects.filter(project=project, member=create_user).update(role=5)
    response = api_key_client.post(
        url(workspace, project, work_item) + "?section=summary", {"page_id": str(private.id)}, format="json"
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "params",
    [
        {"sections": "true"},
        {"section": ["summary", "s3"], "body_format": "text"},
        {"with_content": "true", "body_format": "text", "limit_chars": 7},
    ],
)
def test_attach_accepts_selective_modes_on_new_and_existing_pair(
    api_key_client, workspace, project, work_item, create_user, params
):
    from urllib.parse import urlencode

    page = make_page(workspace, create_user, project, description_html=DOCUMENT)
    collection = url(workspace, project, work_item)
    endpoint = collection + "?" + urlencode(params, doseq=True)
    first = api_key_client.post(endpoint, {"page_id": str(page.id)}, format="json")
    assert first.status_code == 201, first.content
    second = api_key_client.post(endpoint, {"page_id": str(page.id)}, format="json")
    assert second.status_code == 200
    assert first.json() == second.json()
    detail = url(workspace, project, work_item, first.json()["id"])
    assert api_key_client.get(detail, params).json() == first.json()
