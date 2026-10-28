from __future__ import annotations

from django.urls import path

from .user_views import TokenListCreateView, TokenRevokeView, TokenCallsView, ScopeChoicesView

urlpatterns = [
    path("mcp/tokens", TokenListCreateView.as_view(), name="mcp-token-list"),
    path("mcp/tokens/<int:pk>", TokenRevokeView.as_view(), name="mcp-token-revoke"),
    path("mcp/tokens/<int:pk>/calls", TokenCallsView.as_view(), name="mcp-token-calls"),
    path("mcp/scopes", ScopeChoicesView.as_view(), name="mcp-scope-choices"),
]
