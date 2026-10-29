from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import APIToken, MCPCall

User = get_user_model()


def make_user(n: int):
    return User.objects.create_user(
        email=f"user{n}@example.com",
        password="pass",
        first_name=f"User{n}",
    )


class TokenListCreateTests(TestCase):
    def setUp(self):
        self.alice = make_user(1)
        self.bob = make_user(2)
        self.client.force_login(self.alice)

    def test_list_tokens_empty(self):
        res = self.client.get("/api/mcp/tokens")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])

    def test_list_tokens_mine_only(self):
        APIToken.create_for_user(self.alice, "alice-token", ["list_teams"])
        APIToken.create_for_user(self.bob, "bob-token", ["find_slots"])
        res = self.client.get("/api/mcp/tokens")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "alice-token")

    def test_list_tokens_hides_token_hash(self):
        APIToken.create_for_user(self.alice, "my-token", ["list_teams"])
        res = self.client.get("/api/mcp/tokens")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        # No key in any item should be "token_hash"
        for item in data:
            self.assertNotIn("token_hash", item)
        # Also confirm "token" field not present in list response
        for item in data:
            self.assertNotIn("token", item)

    def test_create_token_happy(self):
        res = self.client.post(
            "/api/mcp/tokens",
            {"name": "My PAT", "scopes": ["list_teams", "find_slots"]},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()
        # Must have all list-shape keys
        for key in ("id", "name", "prefix", "scopes", "created_at", "last_used_at", "revoked_at", "is_active"):
            self.assertIn(key, data)
        # Raw token in create response
        self.assertIn("token", data)
        self.assertTrue(data["token"].startswith("sltly_"))
        self.assertEqual(data["name"], "My PAT")
        self.assertEqual(data["scopes"], ["list_teams", "find_slots"])
        self.assertTrue(data["is_active"])

    def test_create_token_name_required(self):
        res = self.client.post(
            "/api/mcp/tokens",
            {"name": "", "scopes": ["list_teams"]},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)

    def test_create_token_name_blank(self):
        res = self.client.post(
            "/api/mcp/tokens",
            {"name": "   ", "scopes": ["list_teams"]},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)

    def test_create_token_invalid_scope(self):
        res = self.client.post(
            "/api/mcp/tokens",
            {"name": "Bad scope", "scopes": ["write_booking"]},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)

    def test_create_token_empty_scopes(self):
        res = self.client.post(
            "/api/mcp/tokens",
            {"name": "My PAT", "scopes": []},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)

    def test_create_token_raw_shown_once(self):
        # POST returns the raw token
        res = self.client.post(
            "/api/mcp/tokens",
            {"name": "Once", "scopes": ["check_time"]},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 201)
        create_data = res.json()
        self.assertIn("token", create_data)

        # Subsequent GET on list does NOT include "token" key in any item
        list_res = self.client.get("/api/mcp/tokens")
        self.assertEqual(list_res.status_code, 200)
        for item in list_res.json():
            self.assertNotIn("token", item)


class TokenRevokeTests(TestCase):
    def setUp(self):
        self.alice = make_user(10)
        self.bob = make_user(11)
        self.client.force_login(self.alice)

    def test_revoke_token(self):
        token, _ = APIToken.create_for_user(self.alice, "to-revoke", ["list_teams"])
        res = self.client.delete(f"/api/mcp/tokens/{token.pk}")
        self.assertEqual(res.status_code, 204)

        # Refresh: appears in list with is_active=False and revoked_at != null
        list_res = self.client.get("/api/mcp/tokens")
        data = list_res.json()
        found = next((x for x in data if x["id"] == token.pk), None)
        self.assertIsNotNone(found)
        self.assertFalse(found["is_active"])
        self.assertIsNotNone(found["revoked_at"])

    def test_revoke_token_idempotent(self):
        token, _ = APIToken.create_for_user(self.alice, "idempotent", ["list_teams"])
        res1 = self.client.delete(f"/api/mcp/tokens/{token.pk}")
        self.assertEqual(res1.status_code, 204)
        res2 = self.client.delete(f"/api/mcp/tokens/{token.pk}")
        self.assertEqual(res2.status_code, 204)

    def test_revoke_other_users_token_is_404(self):
        bob_token, _ = APIToken.create_for_user(self.bob, "bob-token", ["find_slots"])
        res = self.client.delete(f"/api/mcp/tokens/{bob_token.pk}")
        self.assertEqual(res.status_code, 404)


class TokenPurgeTests(TestCase):
    def setUp(self):
        self.alice = make_user(30)
        self.bob = make_user(31)
        self.client.force_login(self.alice)

    def test_purge_requires_revoked_token(self):
        token, _ = APIToken.create_for_user(self.alice, "active", ["list_teams"])
        res = self.client.delete(f"/api/mcp/tokens/{token.pk}/purge")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"], "token_still_active")
        # Row still exists.
        self.assertTrue(APIToken.objects.filter(pk=token.pk).exists())

    def test_purge_deletes_revoked_token_and_calls(self):
        token, _ = APIToken.create_for_user(self.alice, "to-purge", ["list_teams"])
        # Create a call row so we can verify CASCADE.
        MCPCall.objects.create(token=token, capability="list_teams", status="ok", duration_ms=12)
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])

        res = self.client.delete(f"/api/mcp/tokens/{token.pk}/purge")
        self.assertEqual(res.status_code, 204)
        self.assertFalse(APIToken.objects.filter(pk=token.pk).exists())
        self.assertFalse(MCPCall.objects.filter(token_id=token.pk).exists())

    def test_purge_other_users_token_is_404(self):
        bob_token, _ = APIToken.create_for_user(self.bob, "bob-token", ["find_slots"])
        bob_token.revoked_at = timezone.now()
        bob_token.save(update_fields=["revoked_at"])
        res = self.client.delete(f"/api/mcp/tokens/{bob_token.pk}/purge")
        self.assertEqual(res.status_code, 404)
        # Still exists — belongs to bob, untouched.
        self.assertTrue(APIToken.objects.filter(pk=bob_token.pk).exists())

    def test_purge_unknown_token_is_404(self):
        res = self.client.delete("/api/mcp/tokens/999999/purge")
        self.assertEqual(res.status_code, 404)


class TokenCallsTests(TestCase):
    def setUp(self):
        self.alice = make_user(20)
        self.bob = make_user(21)
        self.client.force_login(self.alice)
        self.alice_token, _ = APIToken.create_for_user(self.alice, "alice-calls", ["list_teams"])

    def test_calls_endpoint_empty(self):
        res = self.client.get(f"/api/mcp/tokens/{self.alice_token.pk}/calls")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])

    def test_calls_endpoint_only_fifty(self):
        # Create 60 calls with explicit created_at (1 second apart) so ordering is deterministic.
        # duration_ms == i, so the newest row (i=59) has the highest duration_ms.
        now = timezone.now()
        MCPCall.objects.bulk_create([
            MCPCall(
                token=self.alice_token,
                capability="slotly.list_teams",
                status="ok",
                duration_ms=i,
                error_code="",
                created_at=now + timedelta(seconds=i),
            )
            for i in range(60)
        ])
        res = self.client.get(f"/api/mcp/tokens/{self.alice_token.pk}/calls")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        # Cap: exactly 50 rows returned
        self.assertEqual(len(data), 50)
        # Ordering: newest first — row with duration_ms=59 must be first, duration_ms=10 last
        self.assertGreater(data[0]["duration_ms"], data[-1]["duration_ms"])
        self.assertGreaterEqual(data[0]["created_at"], data[-1]["created_at"])

    def test_calls_endpoint_only_mine(self):
        bob_token, _ = APIToken.create_for_user(self.bob, "bob-calls", ["find_slots"])
        res = self.client.get(f"/api/mcp/tokens/{bob_token.pk}/calls")
        self.assertEqual(res.status_code, 404)


class TokenUnauthenticatedTests(TestCase):
    def setUp(self):
        self.user = make_user(30)
        self.token, _ = APIToken.create_for_user(self.user, "unauth-test", ["list_teams"])

    def test_unauthenticated_403_or_401(self):
        # Without a session, DRF IsAuthenticated returns 403
        urls = [
            "/api/mcp/tokens",
            f"/api/mcp/tokens/{self.token.pk}",
            f"/api/mcp/tokens/{self.token.pk}/calls",
        ]
        for url in urls:
            res_get = self.client.get(url)
            self.assertIn(res_get.status_code, (401, 403), msg=f"GET {url}")
        res_post = self.client.post("/api/mcp/tokens", {}, content_type="application/json")
        self.assertIn(res_post.status_code, (401, 403))
        res_delete = self.client.delete(f"/api/mcp/tokens/{self.token.pk}")
        self.assertIn(res_delete.status_code, (401, 403))


class ScopeChoicesTests(TestCase):
    def setUp(self):
        self.user = make_user(40)
        self.client.force_login(self.user)

    def test_scopes_endpoint_returns_six_entries(self):
        res = self.client.get("/api/mcp/scopes")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("scopes", data)
        self.assertEqual(len(data["scopes"]), 6)

    def test_scopes_endpoint_shape(self):
        res = self.client.get("/api/mcp/scopes")
        self.assertEqual(res.status_code, 200)
        for entry in res.json()["scopes"]:
            self.assertIn("name", entry)
            self.assertIn("label", entry)
            self.assertIn("group", entry)

    def test_scopes_endpoint_requires_auth(self):
        self.client.logout()
        res = self.client.get("/api/mcp/scopes")
        self.assertIn(res.status_code, (401, 403))

    def test_scopes_endpoint_write_group_contains_create_booking_write_event(self):
        res = self.client.get("/api/mcp/scopes")
        self.assertEqual(res.status_code, 200)
        write_names = {e["name"] for e in res.json()["scopes"] if e["group"] == "write"}
        self.assertIn("create_booking", write_names)
        self.assertIn("write_event", write_names)


class CsrfEnforcementRegressionTests(TestCase):
    """Prevent a future refactor from stripping 'CSRF' out of the 403 body —
    the frontend's lib/mcp-tokens.ts::request() peek-and-retry recovery for
    stale csrftoken cookies greps the body text for the substring 'CSRF'
    to decide whether to run the clear-cookie + re-fetch-config + retry
    dance. If DRF ever changes the detail format (e.g. a translation drops
    the string), the client would silently stop recovering and the user
    would hit the same login-loop → CSRF error our live incident hit on
    2026-10-28.

    Also pins the end-to-end expectation that:
      - POST /api/mcp/tokens requires CSRF when SessionAuthentication is used
      - Missing / wrong X-CSRFToken returns 403 (not 401, not 500)
    """

    def setUp(self):
        from django.test import Client

        self.alice = make_user(50)
        # Enforce CSRF checks like production. Default Django Client disables
        # CSRF enforcement; without this flag the real DRF behavior is masked.
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.alice)

    def test_post_with_missing_csrf_header_returns_403_with_detectable_body(self):
        res = self.client.post(
            "/api/mcp/tokens",
            data='{"name": "x", "scopes": ["list_teams"]}',
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 403, res.content[:200])
        # Frontend retry logic depends on this substring; see
        # frontend/lib/mcp-tokens.ts::request() peek.includes("CSRF").
        self.assertIn("CSRF", res.content.decode())

    def test_post_with_wrong_csrf_header_returns_403_with_detectable_body(self):
        # Seed a csrftoken cookie first — mirrors the browser having a cookie
        # from an earlier (now-stale) session.
        self.client.get("/_allauth/browser/v1/config")
        res = self.client.post(
            "/api/mcp/tokens",
            data='{"name": "x", "scopes": ["list_teams"]}',
            content_type="application/json",
            HTTP_X_CSRFTOKEN="definitely-not-the-right-token",
        )
        self.assertEqual(res.status_code, 403, res.content[:200])
        self.assertIn("CSRF", res.content.decode())

    def test_post_with_matching_csrf_header_succeeds(self):
        self.client.get("/_allauth/browser/v1/config")
        csrf = self.client.cookies["csrftoken"].value
        res = self.client.post(
            "/api/mcp/tokens",
            data='{"name": "ok", "scopes": ["list_teams"]}',
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        )
        self.assertEqual(res.status_code, 201, res.content[:200])
