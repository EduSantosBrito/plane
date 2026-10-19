# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

from rest_framework import serializers

from plane.db.models import Page
from plane.utils.content_validator import validate_html_content


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
