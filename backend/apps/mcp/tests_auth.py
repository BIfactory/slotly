from __future__ import annotations

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from apps.accounts.models import User

from .auth import MCPTokenAuthentication, require_scope
from .models import APIToken
from .throttling import MCPTokenThrottle
from .views_base import MCPAPIView


# ---------------------------------------------------------------------------
# Minimal probe view used by authentication / scope tests
# ---------------------------------------------------------------------------

@api_view(["GET"])
@authentication_classes([MCPTokenAuthentication])
@permission_classes([IsAuthenticated, require_scope("slotly.list_teams")])
def _probe(request):
    return Response({"ok": True, "user_id": request.user.id, "token_id": request.auth.id})


# Minimal probe view for rate-limit tests (no scope requirement).
@api_view(["GET"])
@authentication_classes([MCPTokenAuthentication])
@permission_classes([IsAuthenticated])
def _probe_noscope(request):
    return Response({"ok": True})


factory = APIRequestFactory()


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class MCPTokenAuthTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="auth@example.com", password="pass123")
        self.token, self.raw = APIToken.create_for_user(self.user, "T1", ["list_teams"])

    # --- Test 1: valid token authenticates ---

    def test_valid_token_authenticates(self):
        request = factory.get("/", HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        response = _probe(request)
        self.assertEqual(response.status_code, 200)
        response.accepted_renderer = response.accepted_media_type = None
        response.renderer_context = {}
        response.accepted_renderer = __import__("rest_framework.renderers", fromlist=["JSONRenderer"]).JSONRenderer()
        response.accepted_media_type = "application/json"
        response.renderer_context = {"request": request, "view": None}
        response.render()
        data = response.data
        self.assertTrue(data["ok"])
        self.assertEqual(data["user_id"], self.user.id)
        self.assertEqual(data["token_id"], self.token.id)

    # --- Test 2: missing header rejected with 401 and correct body shape ---

    def test_missing_header_rejected(self):
        # Use an MCPAPIView-based view so the MCP exception handler is active
        # and normalises NotAuthenticated → {"error": "invalid_token"}.
        from rest_framework.permissions import IsAuthenticated
        from rest_framework.response import Response as DRFResponse

        class _MCPProbe(MCPAPIView):
            authentication_classes = [MCPTokenAuthentication]
            permission_classes = [IsAuthenticated]

            def get(self, request):
                return DRFResponse({"ok": True})

        view = _MCPProbe.as_view()
        request = factory.get("/")
        response = view(request)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data, {"error": "invalid_token"})

    # --- Test 3: malformed header rejected ---

    def test_malformed_header_rejected(self):
        request = factory.get("/", HTTP_AUTHORIZATION="foo bar")
        response = _probe(request)
        self.assertEqual(response.status_code, 401)

    # --- Test 4: revoked token rejected ---

    def test_revoked_token_rejected(self):
        self.token.revoked_at = timezone.now()
        self.token.save(update_fields=["revoked_at"])
        request = factory.get("/", HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        response = _probe(request)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data, {"error": "invalid_token"})

    # --- Test 5: unknown token rejected ---

    def test_unknown_token_rejected(self):
        request = factory.get("/", HTTP_AUTHORIZATION="Bearer sltly_notarealtoken")
        response = _probe(request)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data, {"error": "invalid_token"})

    # --- Test 6: scope denied ---

    def test_scope_denied(self):
        _token, raw = APIToken.create_for_user(self.user, "Limited", ["find_slots"])
        request = factory.get("/", HTTP_AUTHORIZATION=f"Bearer {raw}")
        response = _probe(request)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data, {"error": "scope_denied"})

    # --- Test 7: wildcard scope allows any ---

    def test_wildcard_scope_allows_any(self):
        _token, raw = APIToken.create_for_user(self.user, "Wildcard", ["*"])
        request = factory.get("/", HTTP_AUTHORIZATION=f"Bearer {raw}")
        response = _probe(request)
        self.assertEqual(response.status_code, 200)

    # --- Test 8: last_used_at updated on success ---

    def test_last_used_at_updated_on_success(self):
        self.assertIsNone(self.token.last_used_at)
        request = factory.get("/", HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        _probe(request)
        self.token.refresh_from_db()
        self.assertIsNotNone(self.token.last_used_at)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class MCPRateLimitTests(TestCase):
    """Tests for per-token rate limiting via MCPTokenThrottle."""

    def setUp(self):
        self.user = User.objects.create_user(email="rl@example.com", password="pass123")

    def _get_throttled_view(self):
        """Return a minimal view class with our throttle attached."""

        class _RateLimitedView(MCPAPIView):
            authentication_classes = [MCPTokenAuthentication]
            permission_classes = [IsAuthenticated]
            throttle_classes = [MCPTokenThrottle]

            def get(self, request):
                return Response({"ok": True})

        return _RateLimitedView.as_view()

    # --- Test 9: minute rate limit ---

    def test_rate_limit_minute(self):
        _token, raw = APIToken.create_for_user(self.user, "RL minute", ["list_teams"])
        view = self._get_throttled_view()
        last_response = None
        for i in range(61):
            request = factory.get("/", HTTP_AUTHORIZATION=f"Bearer {raw}")
            last_response = view(request)

        self.assertEqual(last_response.status_code, 429)
        # The MCPAPIView exception handler rewrites the body.
        data = last_response.data
        self.assertEqual(data.get("error"), "rate_limited")
        retry_after = data.get("retry_after")
        self.assertIsInstance(retry_after, int)
        self.assertGreater(retry_after, 0)
        self.assertLessEqual(retry_after, 60)

    # --- Test 10: hour rate limit ---

    def test_rate_limit_hour(self):
        _token, raw = APIToken.create_for_user(self.user, "RL hour", ["list_teams"])
        view = self._get_throttled_view()

        # Override the minute limit so we can exceed the hour limit first.
        # We patch MCPTokenThrottle.LIMITS to remove the minute limit.
        import unittest.mock as mock

        no_minute_limits = (("1h", 1000, 3600),)
        last_response = None
        with mock.patch.object(MCPTokenThrottle, "LIMITS", no_minute_limits):
            for i in range(1001):
                request = factory.get("/", HTTP_AUTHORIZATION=f"Bearer {raw}")
                last_response = view(request)

        self.assertEqual(last_response.status_code, 429)
        data = last_response.data
        self.assertEqual(data.get("error"), "rate_limited")
        retry_after = data.get("retry_after")
        self.assertIsInstance(retry_after, int)
        self.assertGreater(retry_after, 0)
        self.assertLessEqual(retry_after, 3600)
