# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

from contextlib import contextmanager
import uuid

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.permissions import SAFE_METHODS
from rest_framework.response import Response

from plane.api.views.base import BaseAPIView
from plane.api.serializers.page import PageSerializer, PageCreateSerializer, PageUpdateSerializer, PageReadSerializer
from plane.api.page_content import CONTENT_PARAMETERS, PageContentOptions, defer_page_bodies
from plane.db.models import Page, PageVersion, Project, ProjectMember, ProjectPage, Workspace, WorkspaceMember
from plane.api.page_locks import page_lock_redis
from plane.utils.openapi import create_paginated_response


class PageInUse(APIException):
    status_code = 409
    default_detail = "Page is open in the editor or another API write is in progress. Close it before retrying."


@contextmanager
def page_write_lock(page_id):
    # The live server holds this lock from load until its final store/unload.
    # No expiry: fail closed after a crash instead of allowing a stale editor
    # to overwrite an API write. Restart live and clear its locks to recover.
    redis = page_lock_redis()
    key = f"plane:page-write:{page_id}"
    owner = f"api:{uuid.uuid4()}"
    if not redis.set(key, owner, nx=True):
        raise PageInUse()
    try:
        yield
    finally:
        redis.eval("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end", 1, key, owner)


def page_docs(scope, action, **kwargs):
    return extend_schema(tags=["Pages"], operation_id=f"{action}_{scope}_page", **kwargs)


class PageEndpoint(BaseAPIView):
    serializer_class = PageSerializer
    scope = "workspace"
    defer_write_permission = False
    schema_include_put = True

    def check_permissions(self, request):
        super().check_permissions(request)
        slug = self.kwargs["slug"]
        role = (
            WorkspaceMember.objects.filter(
                workspace__slug=slug,
                member=request.user,
                is_active=True,
            )
            .values_list("role", flat=True)
            .first()
        )
        if role is None:
            raise PermissionDenied()
        if self.scope == "project":
            role = (
                ProjectMember.objects.filter(
                    workspace__slug=slug,
                    project_id=self.kwargs["project_id"],
                    member=request.user,
                    is_active=True,
                )
                .values_list("role", flat=True)
                .first()
            )
            if role is None:
                raise PermissionDenied()
        self.member_role = role
        if not self.defer_write_permission:
            self.require_write_permission()

    def require_write_permission(self):
        if self.request.method not in SAFE_METHODS and self.member_role not in (15, 20):
            raise PermissionDenied()

    def pages(self):
        pages = Page.objects.filter(workspace__slug=self.kwargs["slug"])
        if self.scope == "project":
            pages = pages.filter(
                project_pages__project_id=self.kwargs["project_id"],
                project_pages__deleted_at__isnull=True,
                project_pages__project__deleted_at__isnull=True,
            )
            project = get_object_or_404(Project, pk=self.kwargs["project_id"], workspace__slug=self.kwargs["slug"])
            if self.member_role == 5 and not project.guest_view_all_features:
                pages = pages.filter(owned_by=self.request.user)
        else:
            # Workspace Pages are global; project Pages stay behind project membership.
            pages = pages.filter(is_global=True)
        return pages.filter(Q(access=Page.PUBLIC_ACCESS) | Q(owned_by=self.request.user)).distinct()

    def page(self, for_update=False):
        pages = self.pages()
        page = get_object_or_404(pages, pk=self.kwargs["page_id"])
        if for_update:
            # Lock the base row; DISTINCT queries cannot use FOR UPDATE.
            page = Page.objects.select_for_update().get(pk=page.pk)
        return page

    def require_owner_or_admin(self, page):
        if page.owned_by_id != self.request.user.id and self.member_role != 20:
            raise PermissionDenied("Only the owner or an admin can archive or delete a Page.")

    def list_pages(self, request, content_options=None):
        order = request.query_params.get("order_by", "-created_at")
        if order.lstrip("-") not in ("created_at", "updated_at", "name", "sort_order"):
            raise ValidationError({"order_by": "Unsupported ordering field."})
        pages = self.pages().order_by(order, "id")
        if content_options is not None:
            pages = defer_page_bodies(pages, content_options)
        serializer = PageSerializer if content_options is None else PageReadSerializer
        context = self.get_serializer_context() | {"page_content": content_options}
        # Keep archived Pages discoverable, including for the delete workflow.
        return self.paginate(
            request=request,
            queryset=pages,
            on_results=lambda rows: serializer(rows, many=True, context=context).data,
            default_per_page=20,
            max_per_page=100,
        )

    def create_page(self, request):
        serializer = PageCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        workspace = get_object_or_404(Workspace, slug=self.kwargs["slug"])
        with transaction.atomic():
            page = serializer.save(workspace=workspace, owned_by=request.user, is_global=self.scope == "workspace")
            if self.scope == "project":
                project = get_object_or_404(Project, pk=self.kwargs["project_id"], workspace=workspace)
                ProjectPage.objects.create(workspace=workspace, project=project, page=page)
        return Response(PageSerializer(page).data, status=201)

    def update_page(self, request):
        serializer = PageUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.page()  # Validate scope and visibility before consulting the shared lock.
        with page_write_lock(self.kwargs["page_id"]), transaction.atomic():
            page = self.page(for_update=True)
            if page.is_locked or page.archived_at:
                raise ValidationError("Locked or archived Pages cannot be updated.")
            if "description_html" in serializer.validated_data or "name" in serializer.validated_data:
                # Preserve the previous editor document synchronously before replacing it.
                PageVersion.objects.create(
                    workspace_id=page.workspace_id,
                    page=page,
                    owned_by=request.user,
                    description_html=page.description_html,
                    description_json=page.description_json,
                    description_binary=page.description_binary,
                )
                page.description_binary = None
                page.description_json = {}
            for field, value in serializer.validated_data.items():
                setattr(page, field, value)
            # Live reconstructs the collaborative document from the new HTML on next load.
            page.save()
        return Response(PageSerializer(page).data)

    def archive_page(self, archived):
        self.page()  # Validate scope and visibility before consulting the shared lock.
        with page_write_lock(self.kwargs["page_id"]), transaction.atomic():
            page = self.page(for_update=True)
            self.require_owner_or_admin(page)
            # Avoid silently changing/deleting descendants outside the requested resource.
            if Page.objects.filter(parent=page).exists():
                raise ValidationError("Archive or move child Pages individually first.")
            if not archived and page.parent_id and page.parent.archived_at:
                page.parent = None
            page.archived_at = timezone.localdate() if archived else None
            page.save()
        return Response(status=204)

    def delete_page(self):
        self.page()  # Validate scope and visibility before consulting the shared lock.
        with page_write_lock(self.kwargs["page_id"]), transaction.atomic():
            page = self.page(for_update=True)
            self.require_owner_or_admin(page)
            if page.archived_at is None:
                raise ValidationError("The Page must be archived before deletion.")
            if Page.objects.filter(parent=page).exists():
                raise ValidationError("Delete or move child Pages individually first.")
            page.delete()
        return Response(status=204)


LIST_PARAMETERS = [
    OpenApiParameter("cursor", str),
    OpenApiParameter("per_page", int),
    OpenApiParameter("order_by", str),
]


PAGE_LIST_RESPONSE = create_paginated_response(PageSerializer, "PaginatedPublicPageResponse", "Pages")


class WorkspacePageListEndpoint(PageEndpoint):
    @extend_schema(
        operation_id="list_workspace_pages",
        tags=["Pages"],
        parameters=LIST_PARAMETERS,
        responses={200: PAGE_LIST_RESPONSE},
    )
    def get(self, request, slug):
        return self.list_pages(request)

    @page_docs("workspace", "create", request=PageCreateSerializer, responses={201: PageSerializer})
    def post(self, request, slug):
        return self.create_page(request)


class WorkspacePageDetailEndpoint(PageEndpoint):
    @page_docs("workspace", "retrieve", responses={200: PageSerializer})
    def get(self, request, slug, page_id):
        return Response(PageSerializer(self.page()).data)

    @page_docs("workspace", "update", request=PageUpdateSerializer, responses={200: PageSerializer, 409: None})
    def put(self, request, slug, page_id):
        return self.update_page(request)

    @page_docs("workspace", "delete", responses={204: None, 409: None})
    def delete(self, request, slug, page_id):
        return self.delete_page()


class WorkspacePageArchiveEndpoint(PageEndpoint):
    @page_docs("workspace", "archive", request=None, responses={204: None, 409: None})
    def post(self, request, slug, page_id):
        return self.archive_page(True)

    @page_docs("workspace", "unarchive", responses={204: None, 409: None})
    def delete(self, request, slug, page_id):
        return self.archive_page(False)


class ProjectPageListEndpoint(PageEndpoint):
    scope = "project"

    @extend_schema(
        operation_id="list_project_pages",
        tags=["Pages"],
        parameters=LIST_PARAMETERS + CONTENT_PARAMETERS,
        responses={200: PAGE_LIST_RESPONSE},
    )
    def get(self, request, slug, project_id):
        options = PageContentOptions.from_query(request.query_params, default_full=True)
        return self.list_pages(request, content_options=options)

    @page_docs("project", "create", request=PageCreateSerializer, responses={201: PageSerializer})
    def post(self, request, slug, project_id):
        return self.create_page(request)


class ProjectPageDetailEndpoint(PageEndpoint):
    scope = "project"

    @page_docs("project", "retrieve", parameters=CONTENT_PARAMETERS, responses={200: PageSerializer})
    def get(self, request, slug, project_id, page_id):
        options = PageContentOptions.from_query(request.query_params, default_full=True)
        context = self.get_serializer_context() | {"page_content": options}
        page = get_object_or_404(defer_page_bodies(self.pages(), options), pk=page_id)
        return Response(PageReadSerializer(page, context=context).data)

    @page_docs("project", "update", request=PageUpdateSerializer, responses={200: PageSerializer, 409: None})
    def put(self, request, slug, project_id, page_id):
        return self.update_page(request)

    @page_docs("project", "delete", responses={204: None, 409: None})
    def delete(self, request, slug, project_id, page_id):
        return self.delete_page()


class ProjectPageArchiveEndpoint(PageEndpoint):
    scope = "project"

    @page_docs("project", "archive", request=None, responses={204: None, 409: None})
    def post(self, request, slug, project_id, page_id):
        return self.archive_page(True)

    @page_docs("project", "unarchive", responses={204: None, 409: None})
    def delete(self, request, slug, project_id, page_id):
        return self.archive_page(False)
