from __future__ import annotations

from django.urls import path

from .user_views import TokenListCreateView, TokenRevokeView, TokenCallsView

urlpatterns = [
    path("mcp/tokens", TokenListCreateView.as_view(), name="mcp-token-list"),
    path("mcp/tokens/<int:pk>", TokenRevokeView.as_view(), name="mcp-token-revoke"),
    path("mcp/tokens/<int:pk>/calls", TokenCallsView.as_view(), name="mcp-token-calls"),
]
