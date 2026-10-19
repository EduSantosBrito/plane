# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

from django.urls import path
from plane.api.views.page import (
    WorkspacePageListEndpoint,
    WorkspacePageDetailEndpoint,
    WorkspacePageArchiveEndpoint,
    ProjectPageListEndpoint,
    ProjectPageDetailEndpoint,
    ProjectPageArchiveEndpoint,
)

urlpatterns = [
    path("workspaces/<str:slug>/pages/", WorkspacePageListEndpoint.as_view()),
    path("workspaces/<str:slug>/pages/<uuid:page_id>/", WorkspacePageDetailEndpoint.as_view()),
    path("workspaces/<str:slug>/pages/<uuid:page_id>/archive/", WorkspacePageArchiveEndpoint.as_view()),
    path("workspaces/<str:slug>/projects/<uuid:project_id>/pages/", ProjectPageListEndpoint.as_view()),
    path("workspaces/<str:slug>/projects/<uuid:project_id>/pages/<uuid:page_id>/", ProjectPageDetailEndpoint.as_view()),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/pages/<uuid:page_id>/archive/",
        ProjectPageArchiveEndpoint.as_view(),
    ),
]
