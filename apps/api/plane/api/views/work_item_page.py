# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from plane.api.page_content import CONTENT_PARAMETERS, PageContentOptions, defer_page_bodies
from plane.api.serializers.page import IssuePageCreateSerializer, IssuePageSerializer
from plane.api.views.page import LIST_PARAMETERS, PageEndpoint
from plane.db.models import Issue, IssuePage, Page
from plane.utils.openapi import create_paginated_response


class WorkItemEndpoint(PageEndpoint):
    scope = "project"
    # Resolve resource visibility before denying a write to a visible resource.
    defer_write_permission = True

    def issue(self, for_update=False):
        issues = Issue.objects.filter(
            workspace__slug=self.kwargs["slug"],
            project_id=self.kwargs["project_id"],
            project__deleted_at__isnull=True,
        )
        if for_update:
            issues = issues.select_for_update(of=("self",))
        return get_object_or_404(issues, pk=self.kwargs["issue_id"])


class WorkItemPageEndpoint(WorkItemEndpoint):
    serializer_class = IssuePageSerializer

    def get_serializer_context(self):
        return super().get_serializer_context() | {
            "page_content": getattr(self, "content_options", PageContentOptions())
        }

    def pages(self):
        # Explicit attachments stay within a project, like the project Page API.
        # Global Pages are excluded even if a ProjectPage link also exists.
        return super().pages().filter(is_global=False, archived_at__isnull=True)

    def associations(self):
        issue = self.issue()
        associations = (
            IssuePage.objects.filter(
                issue=issue,
                project_id=self.kwargs["project_id"],
                workspace_id=issue.workspace_id,
                page__in=self.pages(),
            )
            .select_related("page")
            .order_by("-created_at", "id")
        )
        return defer_page_bodies(associations, getattr(self, "content_options", PageContentOptions()), "page__")


class WorkItemPageListEndpoint(WorkItemPageEndpoint):
    @extend_schema(
        operation_id="list_work_item_pages",
        tags=["Pages"],
        parameters=LIST_PARAMETERS[:2] + CONTENT_PARAMETERS,
        responses={200: create_paginated_response(IssuePageSerializer, "PaginatedWorkItemPageResponse", "Pages")},
    )
    def get(self, request, slug, project_id, issue_id):
        self.content_options = PageContentOptions.from_query(request.query_params)
        # The shared paginator does not reject zero/negative page sizes itself.
        if self.get_per_page(request, 20, 100) < 1:
            raise ValidationError({"per_page": "Must be positive."})
        return self.paginate(
            request=request,
            queryset=self.associations(),
            on_results=lambda rows: self.get_serializer(rows, many=True).data,
            default_per_page=20,
            max_per_page=100,
        )

    @extend_schema(
        operation_id="attach_page_to_work_item",
        tags=["Pages"],
        request=IssuePageCreateSerializer,
        parameters=CONTENT_PARAMETERS,
        responses={200: IssuePageSerializer, 201: IssuePageSerializer},
    )
    def post(self, request, slug, project_id, issue_id):
        self.content_options = PageContentOptions.from_query(request.query_params)
        serializer = IssuePageCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            issue = self.issue(for_update=True)
            page = get_object_or_404(self.pages(), pk=serializer.validated_data["page_id"])
            # Serialize with archive/delete operations before checking visibility again.
            Page.objects.select_for_update(of=("self",)).get(pk=page.pk)
            page = get_object_or_404(self.pages(), pk=page.pk)
            self.require_write_permission()
            association, created = IssuePage.objects.get_or_create(
                issue=issue,
                page=page,
                defaults={"project_id": issue.project_id, "created_by": request.user},
            )
            # Render inside the transaction: an unknown section must not leave a new pair.
            data = self.get_serializer(association).data
        return Response(data, status=201 if created else 200)


class WorkItemPageDetailEndpoint(WorkItemPageEndpoint):
    @extend_schema(
        operation_id="retrieve_work_item_page",
        tags=["Pages"],
        parameters=CONTENT_PARAMETERS,
        responses={200: IssuePageSerializer},
    )
    def get(self, request, slug, project_id, issue_id, work_item_page_id):
        self.content_options = PageContentOptions.from_query(request.query_params)
        association = get_object_or_404(self.associations(), pk=work_item_page_id)
        return Response(self.get_serializer(association).data)

    @extend_schema(operation_id="detach_page_from_work_item", tags=["Pages"], responses={204: None})
    def delete(self, request, slug, project_id, issue_id, work_item_page_id):
        with transaction.atomic():
            self.issue(for_update=True)
            association = get_object_or_404(self.associations(), pk=work_item_page_id)
            self.require_write_permission()
            IssuePage.objects.filter(pk=association.pk).delete()
        return Response(status=204)
