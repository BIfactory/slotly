from __future__ import annotations

import logging
import time

from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.mcp import idempotency
from apps.scheduling import graph_client as _graph
from apps.scheduling.google_client import GoogleOAuthError

MicrosoftOAuthError = _graph.MicrosoftOAuthError
from apps.mcp.auth import MCPTokenAuthentication, has_scope
from apps.mcp.models import APIToken, MCPCall
from apps.mcp.throttling import MCPTokenThrottle
from apps.mcp.tools import get_tool, list_tools
from apps.mcp.views_base import MCPAPIView

logger = logging.getLogger(__name__)


class ToolsListView(MCPAPIView):
    authentication_classes = [MCPTokenAuthentication]
    permission_classes = [IsAuthenticated]
    # No scope requirement — listing tools is zero-risk and clients need it
    # to discover what their token can call. The response unconditionally
    # advertises ALL tools; whether the token can CALL a given tool is
    # enforced at call-time by require_scope().
    throttle_classes = [MCPTokenThrottle]

    def get(self, request):
        return Response({
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.input_schema,
                    "annotations": {"sensitivity": t.sensitivity},
                }
                for t in list_tools()
            ],
        })

    # MCP clients vary on the HTTP method used to list tools — the spec
    # (2024-11-05) is JSON-RPC over HTTP, which many clients implement as
    # POST with an empty body. Accept POST too so Personal Hub, Claude
    # Desktop and any JSON-RPC-style client probe can succeed without
    # client-side method rewriting.
    def post(self, request):
        return self.get(request)


class ToolsCallView(MCPAPIView):
    authentication_classes = [MCPTokenAuthentication]
    permission_classes = [IsAuthenticated]
    throttle_classes = [MCPTokenThrottle]

    def post(self, request):
        name = request.data.get("name")
        params = request.data.get("params") or {}
        if not isinstance(name, str) or not name:
            return Response({"error": "invalid_request", "detail": "name is required"}, status=400)
        if not isinstance(params, dict):
            return Response({"error": "invalid_request", "detail": "params must be an object"}, status=400)

        # Validate idempotency_key if present.
        raw_idem_key = request.data.get("idempotency_key")
        if raw_idem_key is not None:
            if not isinstance(raw_idem_key, str) or not (1 <= len(raw_idem_key) <= 128):
                return Response(
                    {"error": "invalid_request", "detail": "idempotency_key must be a string of length 1-128"},
                    status=400,
                )

        tool = get_tool(name)
        if tool is None:
            return Response({"error": "unknown_tool", "name": name}, status=404)

        # Scope check — do this here (not via permission_classes) so we can
        # read the tool name from the body.
        token = request.auth  # APIToken
        if not has_scope(token, tool.name):
            # Audit the attempt as scope_denied before refusing.
            _record_call(token, tool.name, status_="error", duration_ms=0, error_code="scope_denied")
            return Response({"error": "scope_denied"}, status=403)

        # Idempotency enforcement — only for write_external tools.
        is_write = tool.sensitivity == "write_external"
        idem_key = raw_idem_key if (is_write and raw_idem_key is not None) else None

        if idem_key is not None:
            cached = idempotency.lookup(token, idem_key)
            if cached is not None:
                _record_call(token, tool.name, status_="ok", duration_ms=0, error_code="idempotency_hit")
                return Response({"result": cached})

        started = time.perf_counter()
        try:
            result = tool.handler(request.user, params)
        except ValidationError as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            _record_call(token, tool.name, status_="error", duration_ms=duration_ms, error_code="validation_error")
            return Response({"error": "validation_error", "detail": _as_detail(exc)}, status=400)
        except PermissionDenied as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            _record_call(token, tool.name, status_="error", duration_ms=duration_ms, error_code="forbidden")
            return Response({"error": "forbidden", "detail": _as_detail(exc)}, status=403)
        except (GoogleOAuthError, MicrosoftOAuthError) as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            _record_call(token, tool.name, status_="error", duration_ms=duration_ms, error_code="upstream_oauth_failed")
            return Response(
                {"error": "upstream_oauth_failed", "detail": str(exc)},
                status=503,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("MCP tool %s failed", tool.name)
            duration_ms = int((time.perf_counter() - started) * 1000)
            _record_call(token, tool.name, status_="error", duration_ms=duration_ms, error_code="server_error")
            return Response({"error": "server_error"}, status=500)

        duration_ms = int((time.perf_counter() - started) * 1000)
        _record_call(token, tool.name, status_="ok", duration_ms=duration_ms, error_code="")

        # Store idempotency record after successful write dispatch.
        if idem_key is not None:
            booking_uuid = None
            if isinstance(result, dict):
                raw = result.get("booking_uuid")
                # Only pass through non-empty strings; other types (int, None,
                # empty string) would silently fail UUIDField coercion at the DB
                # layer and raise DataError — not caught by store()'s IntegrityError
                # guard. Losing the convenience field is a non-fatal degradation.
                if isinstance(raw, str) and raw:
                    booking_uuid = raw
            try:
                idempotency.store(
                    token,
                    idem_key,
                    tool.name,
                    result,
                    booking_uuid=booking_uuid,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "idempotency.store failed for key=%s capability=%s",
                    idem_key,
                    tool.name,
                )

        return Response({"result": result})


def _record_call(token, capability, *, status_, duration_ms, error_code):
    try:
        MCPCall.objects.create(
            token=token,
            capability=capability,
            status=status_,
            duration_ms=duration_ms,
            error_code=error_code,
        )
    except Exception:  # noqa: BLE001
        # Audit failures never block the response.
        logger.exception("Failed to record MCPCall")


def _as_detail(exc):
    """Convert DRF validation error payloads to a plain-serializable dict."""
    detail = getattr(exc, "detail", None)
    if detail is None:
        return str(exc)
    return detail
