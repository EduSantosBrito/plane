# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
from types import SimpleNamespace

import pytest
from plane.utils.openapi.hooks import preprocess_filter_api_v1_paths

pytestmark = pytest.mark.unit


def test_pages_put_contract_is_included():
    callback = SimpleNamespace(cls=SimpleNamespace(schema_include_put=True))
    endpoint = ("/api/v1/workspaces/{slug}/pages/{page_id}/", "regex", "PUT", callback)
    assert preprocess_filter_api_v1_paths([endpoint]) == [endpoint]


def test_legacy_put_and_session_routes_stay_excluded():
    callback = SimpleNamespace(cls=SimpleNamespace())
    endpoints = [
        ("/api/v1/workspaces/{slug}/projects/", "regex", "PUT", callback),
        ("/api/workspaces/{slug}/pages/", "regex", "GET", callback),
    ]
    assert preprocess_filter_api_v1_paths(endpoints) == []
