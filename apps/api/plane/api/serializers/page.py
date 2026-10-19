# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

from rest_framework import serializers

from plane.db.models import IssuePage, Page
from plane.api.page_content import PageContentOptions, render_page_content
from plane.utils.content_validator import validate_html_content
from plane.utils.host import base_host


class PageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Page
        fields = [
            "id",
            "name",
            "description_html",
            "description_json",
            "description_stripped",
            "workspace",
            "owned_by",
            "access",
            "color",
            "is_locked",
            "archived_at",
            "view_props",
            "logo_props",
            "external_id",
            "external_source",
            "is_global",
            "parent",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
        ]
        read_only_fields = fields


class PageCreateSerializer(serializers.ModelSerializer):
    name = serializers.CharField(allow_blank=True)
    description_html = serializers.CharField(allow_blank=True, trim_whitespace=False)

    class Meta:
        model = Page
        fields = [
            "name",
            "description_html",
            "access",
            "color",
            "is_locked",
            "archived_at",
            "view_props",
            "logo_props",
            "external_id",
            "external_source",
        ]

    def validate_description_html(self, value):
        valid, message, sanitized = validate_html_content(value)
        if not valid:
            raise serializers.ValidationError(message)
        return sanitized if sanitized is not None else value


class PageUpdateSerializer(PageCreateSerializer):
    name = serializers.CharField(required=False, allow_blank=True)
    description_html = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)

    class Meta(PageCreateSerializer.Meta):
        fields = ["name", "description_html"]

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Provide name, description_html, or both.")
        return attrs


class IssuePageCreateSerializer(serializers.Serializer):
    page_id = serializers.UUIDField()


class PageMetadataSerializer(PageSerializer):
    url = serializers.SerializerMethodField()

    class Meta(PageSerializer.Meta):
        fields = [
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
        ]
        read_only_fields = fields

    def get_url(self, page):
        view = self.context["view"]
        return (
            f"{base_host(self.context['request']).rstrip('/')}/{view.kwargs['slug']}"
            f"/projects/{view.kwargs['project_id']}/pages/{page.id}"
        )


class PageReadSerializer(PageMetadataSerializer):
    def to_representation(self, page):
        options = self.context.get("page_content", PageContentOptions())
        if options.legacy:
            return PageSerializer(page).data
        result = super().to_representation(page)
        if options.mode != "metadata":
            result.update(render_page_content(page, options))
        return result


class IssuePageSerializer(serializers.ModelSerializer):
    page = PageReadSerializer(read_only=True)

    class Meta:
        model = IssuePage
        fields = ["id", "page", "issue", "project", "workspace", "created_at", "updated_at", "created_by"]
        read_only_fields = fields
