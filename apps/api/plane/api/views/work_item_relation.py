# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from plane.api.serializers.issue import IssueRelationRemoveSerializer
from plane.api.views.work_item_page import WorkItemEndpoint
from plane.db.models import Issue, IssueRelation


class WorkItemRelationRemoveEndpoint(WorkItemEndpoint):
    serializer_class = IssueRelationRemoveSerializer

    @extend_schema(
        operation_id="delete_work_item_relation",
        tags=["Work Item Relations"],
        request=IssueRelationRemoveSerializer,
        responses={204: None},
    )
    def post(self, request, slug, project_id, issue_id):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        related_id = serializer.validated_data["related_issue"]
        with transaction.atomic():
            # A stable lock order also serializes simultaneous reverse removals.
            list(
                Issue.objects.select_for_update(of=("self",))
                .filter(workspace__slug=slug, pk__in=[issue_id, related_id], project__deleted_at__isnull=True)
                .order_by("id")
            )
            self.issue()
            get_object_or_404(Issue, pk=related_id, workspace__slug=slug, project__deleted_at__isnull=True)
            self.require_write_permission()
            # v1.4.2 normally stores one canonical edge and derives its reverse.
            # Delete both stored directions too, so old paired rows leave no half-edge.
            IssueRelation.objects.filter(workspace__slug=slug).filter(
                Q(issue_id=issue_id, related_issue_id=related_id) | Q(issue_id=related_id, related_issue_id=issue_id)
            ).delete()
        return Response(status=204)
