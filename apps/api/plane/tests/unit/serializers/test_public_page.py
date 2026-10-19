# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

import pytest
from plane.api.serializers.page import PageCreateSerializer, PageUpdateSerializer

pytestmark = pytest.mark.unit


def test_create_requires_content_and_name():
    serializer = PageCreateSerializer(data={})
    assert not serializer.is_valid()
    assert set(serializer.errors) == {"name", "description_html"}


def test_update_rejects_empty_and_ignores_privileged_fields():
    serializer = PageUpdateSerializer(data={"owned_by": "attacker", "access": 0}, partial=True)
    assert not serializer.is_valid()


def test_update_accepts_blank_content():
    serializer = PageUpdateSerializer(data={"description_html": ""}, partial=True)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == {"description_html": ""}


def test_html_validation():
    serializer = PageCreateSerializer(
        data={"name": "Unsafe", "description_html": "<script>alert(1)</script><p>Hello</p>"}
    )
    if serializer.is_valid():
        assert "<script" not in serializer.validated_data["description_html"]
