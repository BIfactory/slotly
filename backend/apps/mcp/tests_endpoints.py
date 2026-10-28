from __future__ import annotations

from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.accounts.models import User

from .models import APIToken, MCPCall
from .tools import ToolDefinition, _REGISTRY


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class ToolsListViewTests(APITestCase):
    """Tests for GET /mcp/v1/tools/list."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email="list@example.com", password="pass123")
        self.token, self.raw = APIToken.create_for_user(self.user, "T3-list", ["list_teams"])

    def test_tools_list_requires_auth(self):
        """GET without auth header → 401 {"error": "invalid_token"}."""
        response = self.client.get("/mcp/v1/tools/list")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"error": "invalid_token"})

    def test_tools_list_returns_four_tools(self):
        """GET with valid token → 200, four tools with required keys."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        response = self.client.get("/mcp/v1/tools/list")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        tools = data["tools"]
        self.assertEqual(len(tools), 4)
        for tool in tools:
            self.assertIn("name", tool)
            self.assertIn("description", tool)
            self.assertIn("inputSchema", tool)

    def test_tools_list_names(self):
        """GET with valid token → four exact tool names."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        response = self.client.get("/mcp/v1/tools/list")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        names = {t["name"] for t in data["tools"]}
        self.assertEqual(names, {"slotly.list_teams", "slotly.list_members", "slotly.find_slots", "slotly.check_time"})


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class ToolsCallViewTests(APITestCase):
    """Tests for POST /mcp/v1/tools/call."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email="call@example.com", password="pass123")
        self.token, self.raw = APIToken.create_for_user(self.user, "T3-call", ["list_teams"])

    def test_tools_call_requires_auth(self):
        """POST without auth header → 401."""
        response = self.client.post("/mcp/v1/tools/call", {"name": "slotly.list_teams", "params": {}}, format="json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"error": "invalid_token"})

    def test_tools_call_unknown_tool(self):
        """POST with unknown tool name → 404 {"error": "unknown_tool", "name": ...}."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        response = self.client.post(
            "/mcp/v1/tools/call",
            {"name": "slotly.nonexistent", "params": {}},
            format="json",
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "unknown_tool", "name": "slotly.nonexistent"})

    def test_tools_call_missing_name(self):
        """POST with empty body → 400 {"error": "invalid_request", "detail": "name is required"}."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        response = self.client.post("/mcp/v1/tools/call", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "invalid_request", "detail": "name is required"})

    def test_tools_call_scope_denied(self):
        """Token without the required scope → 403 {"error": "scope_denied"}, MCPCall audit row written."""
        # Token only has find_slots, not list_teams.
        _, raw = APIToken.create_for_user(self.user, "limited", ["find_slots"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        response = self.client.post(
            "/mcp/v1/tools/call",
            {"name": "slotly.list_teams", "params": {}},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"error": "scope_denied"})

        # Audit row must exist.
        call = MCPCall.objects.filter(capability="slotly.list_teams").first()
        self.assertIsNotNone(call)
        self.assertEqual(call.status, "error")
        self.assertEqual(call.error_code, "scope_denied")

    def test_tools_call_dummy_success(self):
        """Token with list_teams scope → 200, correct result, MCPCall audit row written."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        response = self.client.post(
            "/mcp/v1/tools/call",
            {"name": "slotly.list_teams", "params": {}},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"], {"teams": []})

        # Audit row must exist.
        call = MCPCall.objects.filter(capability="slotly.list_teams", status="ok").first()
        self.assertIsNotNone(call)
        self.assertGreaterEqual(call.duration_ms, 0)

    def test_tools_call_server_error_recorded(self):
        """Handler raising RuntimeError → 500 {"error": "server_error"}, MCPCall with server_error."""
        def _boom(user, params):
            raise RuntimeError("intentional failure")

        # Build a patched version of _REGISTRY with a broken list_teams handler.
        broken_tool = ToolDefinition(
            name="slotly.list_teams",
            description="boom",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=_boom,
        )
        patched_registry = dict(_REGISTRY)
        patched_registry["slotly.list_teams"] = broken_tool

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        with patch.dict("apps.mcp.tools._REGISTRY", patched_registry, clear=True):
            response = self.client.post(
                "/mcp/v1/tools/call",
                {"name": "slotly.list_teams", "params": {}},
                format="json",
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"error": "server_error"})

        # Audit row must exist.
        call = MCPCall.objects.filter(capability="slotly.list_teams", error_code="server_error").first()
        self.assertIsNotNone(call)
        self.assertEqual(call.status, "error")
