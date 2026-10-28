"""Tests for the idempotency infrastructure (Task 2).

Uses a trivial write_external test tool injected via patch.dict on _REGISTRY
so tests don't depend on T3/T4 handlers.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import User

from .idempotency import TTL_HOURS, lookup, store
from .models import APIToken, IdempotencyRecord, MCPCall
from .tasks import purge_old_idempotency
from .tools import ToolDefinition, _REGISTRY


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_write_tool(name="slotly.test_write", handler=None):
    """Return a ToolDefinition with sensitivity='write_external'."""
    if handler is None:
        def handler(user, params):
            return {"status": "created", "booking_uuid": None}
    return ToolDefinition(
        name=name,
        description="Test write tool",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
        sensitivity="write_external",
    )


# ---------------------------------------------------------------------------
# Unit tests for idempotency.lookup / idempotency.store
# ---------------------------------------------------------------------------

@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class IdempotencyLookupTests(APITestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email="idem_lookup@example.com", password="pass")
        self.token, _ = APIToken.create_for_user(self.user, "idem-lookup", ["list_teams"])

    # 1
    def test_lookup_miss_returns_none(self):
        """lookup with no row in DB returns None."""
        result = lookup(self.token, "nonexistent-key")
        self.assertIsNone(result)

    # 2
    def test_lookup_hit_returns_cached(self):
        """lookup with existing in-TTL row returns response_json."""
        payload = {"status": "created", "id": 42}
        IdempotencyRecord.objects.create(
            token=self.token,
            key="hit-key",
            capability="slotly.test_write",
            response_json=payload,
        )
        result = lookup(self.token, "hit-key")
        self.assertEqual(result, payload)

    # 3
    def test_lookup_expired_returns_none(self):
        """lookup with row older than TTL_HOURS returns None."""
        payload = {"status": "created"}
        rec = IdempotencyRecord.objects.create(
            token=self.token,
            key="expired-key",
            capability="slotly.test_write",
            response_json=payload,
        )
        # Manually set created_at older than TTL.
        IdempotencyRecord.objects.filter(pk=rec.pk).update(
            created_at=timezone.now() - timedelta(hours=TTL_HOURS + 1)
        )
        result = lookup(self.token, "expired-key")
        self.assertIsNone(result)

    # 4
    def test_store_persists_response(self):
        """store() creates an IdempotencyRecord row with correct fields."""
        import uuid
        buid = str(uuid.uuid4())
        payload = {"status": "created", "booking_uuid": buid}
        store(self.token, "store-key", "slotly.test_write", payload, booking_uuid=buid)
        rec = IdempotencyRecord.objects.get(token=self.token, key="store-key")
        self.assertEqual(rec.response_json, payload)
        self.assertEqual(rec.capability, "slotly.test_write")

    # 5
    def test_store_race_second_write_swallowed(self):
        """Calling store() twice with the same (token, key) does not raise;
        only one row is persisted."""
        payload = {"status": "created"}
        store(self.token, "race-key", "slotly.test_write", payload)
        # Second call — IntegrityError should be swallowed.
        store(self.token, "race-key", "slotly.test_write", {"status": "duplicate"})
        count = IdempotencyRecord.objects.filter(token=self.token, key="race-key").count()
        self.assertEqual(count, 1)


# ---------------------------------------------------------------------------
# Integration tests — tools/list and tools/call endpoints
# ---------------------------------------------------------------------------

@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class ToolsListSensitivityTests(APITestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email="idem_list@example.com", password="pass")
        self.token, self.raw = APIToken.create_for_user(self.user, "idem-list", ["list_teams"])

    # 6
    def test_tools_list_includes_sensitivity(self):
        """GET /mcp/v1/tools/list — every tool has annotations.sensitivity in allowed values."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        response = self.client.get("/mcp/v1/tools/list")
        self.assertEqual(response.status_code, 200)
        tools = response.json()["tools"]
        self.assertGreater(len(tools), 0)
        for t in tools:
            self.assertIn("annotations", t)
            self.assertIn(t["annotations"]["sensitivity"], {"read_only", "write_external"})


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class ToolsCallIdempotencyTests(APITestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email="idem_call@example.com", password="pass")
        self.token, self.raw = APIToken.create_for_user(self.user, "idem-call", ["list_teams"])

        # A counter so we can verify how many times the handler was called.
        self._call_count = 0

        def _write_handler(user, params):
            self._call_count += 1
            return {"status": "created", "seq": self._call_count}

        self._write_tool = _make_write_tool(handler=_write_handler)
        self._patched_registry = dict(_REGISTRY)
        self._patched_registry[self._write_tool.name] = self._write_tool

    def _post(self, body):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        # Inject write tool scope into the token for the test.
        self.token.scopes = list(self.token.scopes) + [self._write_tool.name.split(".")[-1]]
        self.token.save()
        with patch.dict("apps.mcp.tools._REGISTRY", self._patched_registry):
            # Also patch has_scope to allow the test tool.
            with patch("apps.mcp.views.has_scope", return_value=True):
                return self.client.post("/mcp/v1/tools/call", body, format="json")

    # 7
    def test_write_tool_without_idempotency_key_executes(self):
        """POST write tool without idempotency_key → handler called, no IdempotencyRecord."""
        resp = self._post({"name": self._write_tool.name, "params": {}})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._call_count, 1)
        self.assertEqual(IdempotencyRecord.objects.count(), 0)

    # 8
    def test_write_tool_with_idempotency_key_stores(self):
        """POST write tool with idempotency_key → handler called once, IdempotencyRecord created."""
        resp = self._post({"name": self._write_tool.name, "params": {}, "idempotency_key": "key-8"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._call_count, 1)
        self.assertEqual(IdempotencyRecord.objects.filter(token=self.token, key="key-8").count(), 1)

    # 9
    def test_write_tool_second_call_returns_cached(self):
        """Two POSTs with the same idempotency_key → handler called once, responses identical."""
        body = {"name": self._write_tool.name, "params": {}, "idempotency_key": "key-9"}
        r1 = self._post(body)
        r2 = self._post(body)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(self._call_count, 1)
        self.assertEqual(r1.json()["result"], r2.json()["result"])

    # 10
    def test_write_tool_second_call_audit_logged_as_hit(self):
        """Second call with same idempotency_key creates MCPCall(error_code='idempotency_hit')."""
        body = {"name": self._write_tool.name, "params": {}, "idempotency_key": "key-10"}
        self._post(body)
        self._post(body)
        hit_calls = MCPCall.objects.filter(
            capability=self._write_tool.name,
            error_code="idempotency_hit",
            status="ok",
        )
        self.assertEqual(hit_calls.count(), 1)

    # 11
    def test_read_tool_ignores_idempotency_key(self):
        """POST read tool with idempotency_key → handler called every time, no IdempotencyRecord."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        # list_teams is read_only and in scope.
        for _ in range(2):
            resp = self.client.post(
                "/mcp/v1/tools/call",
                {"name": "slotly.list_teams", "params": {}, "idempotency_key": "read-key"},
                format="json",
            )
            self.assertEqual(resp.status_code, 200)
        self.assertEqual(IdempotencyRecord.objects.count(), 0)

    # 12
    def test_handler_failure_not_cached(self):
        """Handler that raises ValidationError → no IdempotencyRecord; retry re-dispatches."""
        from rest_framework.exceptions import ValidationError

        call_count = {"n": 0}

        def _failing_handler(user, params):
            call_count["n"] += 1
            raise ValidationError("always fails")

        failing_tool = _make_write_tool(name="slotly.test_failing", handler=_failing_handler)
        registry = dict(_REGISTRY)
        registry[failing_tool.name] = failing_tool

        body = {"name": failing_tool.name, "params": {}, "idempotency_key": "fail-key"}
        with patch.dict("apps.mcp.tools._REGISTRY", registry):
            with patch("apps.mcp.views.has_scope", return_value=True):
                self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")
                r1 = self.client.post("/mcp/v1/tools/call", body, format="json")
                r2 = self.client.post("/mcp/v1/tools/call", body, format="json")

        self.assertEqual(r1.status_code, 400)
        self.assertEqual(r2.status_code, 400)
        self.assertEqual(call_count["n"], 2)  # handler called each time (no cache)
        self.assertEqual(IdempotencyRecord.objects.count(), 0)

    # 13
    def test_idempotency_key_invalid_shape(self):
        """Empty key or >128 chars → 400 invalid_request."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")

        # Empty string
        resp = self.client.post(
            "/mcp/v1/tools/call",
            {"name": "slotly.list_teams", "params": {}, "idempotency_key": ""},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "invalid_request")

        # 129-character string
        resp = self.client.post(
            "/mcp/v1/tools/call",
            {"name": "slotly.list_teams", "params": {}, "idempotency_key": "x" * 129},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "invalid_request")

    # 14
    def test_idempotency_key_scoped_per_token(self):
        """Two tokens using the same key each have their own cache; no cross-token hit."""
        user2 = User.objects.create_user(email="idem_call2@example.com", password="pass")
        token2, raw2 = APIToken.create_for_user(user2, "idem-call2", ["list_teams"])

        payload1 = {"status": "created", "seq": 1}
        payload2 = {"status": "created", "seq": 999}

        store(self.token, "shared-key", "slotly.test_write", payload1)
        store(token2, "shared-key", "slotly.test_write", payload2)

        hit1 = lookup(self.token, "shared-key")
        hit2 = lookup(token2, "shared-key")

        self.assertEqual(hit1, payload1)
        self.assertEqual(hit2, payload2)
        self.assertNotEqual(hit1, hit2)


# ---------------------------------------------------------------------------
# Celery task test
# ---------------------------------------------------------------------------

@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class PurgeTaskTests(APITestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email="idem_purge@example.com", password="pass")
        self.token, _ = APIToken.create_for_user(self.user, "idem-purge", ["list_teams"])

    # 15
    def test_purge_task_removes_old_rows(self):
        """purge_old_idempotency() deletes rows older than 48h; leaves newer ones."""
        now = timezone.now()

        # Row at 1h — should survive.
        r1 = IdempotencyRecord.objects.create(
            token=self.token, key="1h", capability="slotly.test_write", response_json={}
        )
        IdempotencyRecord.objects.filter(pk=r1.pk).update(created_at=now - timedelta(hours=1))

        # Row at 25h — within 48h window, should survive.
        r2 = IdempotencyRecord.objects.create(
            token=self.token, key="25h", capability="slotly.test_write", response_json={}
        )
        IdempotencyRecord.objects.filter(pk=r2.pk).update(created_at=now - timedelta(hours=25))

        # Row at 49h — should be deleted.
        r3 = IdempotencyRecord.objects.create(
            token=self.token, key="49h", capability="slotly.test_write", response_json={}
        )
        IdempotencyRecord.objects.filter(pk=r3.pk).update(created_at=now - timedelta(hours=49))

        deleted_count = purge_old_idempotency()

        self.assertEqual(deleted_count, 1)
        self.assertTrue(IdempotencyRecord.objects.filter(pk=r1.pk).exists())
        self.assertTrue(IdempotencyRecord.objects.filter(pk=r2.pk).exists())
        self.assertFalse(IdempotencyRecord.objects.filter(pk=r3.pk).exists())
