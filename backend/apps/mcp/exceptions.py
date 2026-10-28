from __future__ import annotations

"""Custom DRF exception handler for MCP views.

Registered per-view via MCPAPIView.get_exception_handler() so it only affects
/mcp/v1/* routes and leaves all other endpoints untouched.
"""

from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, Throttled
from rest_framework.views import exception_handler as drf_exception_handler


def mcp_exception_handler(exc, context):
    """Post-process DRF exceptions to enforce MCP error-body contracts.

    Contracts:
      - 401 → {"error": "invalid_token"}
      - 403 → {"error": "scope_denied"}
      - 429 → {"error": "rate_limited", "retry_after": <int>}
    """
    response = drf_exception_handler(exc, context)

    if response is None:
        return response

    if isinstance(exc, Throttled):
        response.data = {
            "error": "rate_limited",
            "retry_after": int(exc.wait or 0),
        }
        return response

    if isinstance(exc, AuthenticationFailed):
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            response.data = detail
        return response

    if isinstance(exc, PermissionDenied):
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            response.data = detail
        return response

    return response
