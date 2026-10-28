from __future__ import annotations

import hashlib

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework.permissions import BasePermission

from .models import APIToken


class MCPTokenAuthentication(BaseAuthentication):
    """Authenticates requests using an `Authorization: Bearer sltly_*` header.

    - No header / missing Bearer prefix → return None (other auth classes may
      handle, or DRF emits 401 via WWW-Authenticate challenge).
    - Header present but raw token doesn't start with ``sltly_`` → 401
      {"error": "invalid_token"}.
    - Token not in DB or revoked → 401 {"error": "invalid_token"}.
    - Valid → update last_used_at (single-field UPDATE), return (user, token).
    """

    def authenticate(self, request):
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header:
            return None

        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None

        raw = parts[1]

        if not raw.startswith("sltly_"):
            raise AuthenticationFailed({"error": "invalid_token"})

        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        token = APIToken.objects.filter(token_hash=token_hash).first()

        if token is None:
            raise AuthenticationFailed({"error": "invalid_token"})

        if token.revoked_at is not None:
            raise AuthenticationFailed({"error": "invalid_token"})

        # Single-field update to avoid a full row write.
        APIToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())

        return (token.user, token)

    def authenticate_header(self, request):
        return "Bearer realm=\"mcp\""


def has_scope(token: APIToken, capability: str) -> bool:
    """Check whether *token* covers *capability*.

    *capability* arrives in ``slotly.*`` form (e.g. ``"slotly.list_teams"``).
    The ``slotly.`` prefix is stripped before comparing to token.scopes per
    ledger ruling R1.
    """
    short = capability.removeprefix("slotly.")
    if "*" in token.scopes:
        return True
    return short in token.scopes


class MCPScope(BasePermission):
    """DRF permission that checks a specific MCP capability on request.auth."""

    capability: str = ""

    def has_permission(self, request, view):
        token = getattr(request, "auth", None)
        if not isinstance(token, APIToken):
            return False
        if not has_scope(token, self.capability):
            raise PermissionDenied({"error": "scope_denied"})
        return True


def require_scope(cap: str) -> type[MCPScope]:
    """Factory returning a DRF permission class bound to *cap*.

    Usage::

        permission_classes = [IsAuthenticated, require_scope("slotly.list_teams")]
    """
    return type("Scoped", (MCPScope,), {"capability": cap})
