from __future__ import annotations

from django.urls import path

from .views import ToolsCallView, ToolsListView

urlpatterns = [
    path("tools/list", ToolsListView.as_view(), name="mcp-tools-list"),
    path("tools/call", ToolsCallView.as_view(), name="mcp-tools-call"),
]
