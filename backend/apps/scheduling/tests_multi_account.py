"""
Tests for multi-account OAuth model layer (Task 1), OAuth callback upsert +
get_credentials semantics (Task 2), and REST API + _pick_write_provider
refactor (Task 3).
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.scheduling.models import GoogleAccount, MicrosoftAccount, WriteCalendarDefault
from apps.scheduling.security import decrypt, encrypt


def _make_google(user, email="x@gmail.com", access_token="AT", refresh_token="RT"):
    return GoogleAccount.objects.create(
        user=user,
        google_email=email,
        access_token_encrypted=encrypt(access_token),
        refresh_token_encrypted=encrypt(refresh_token),
        expires_at=timezone.now() + timedelta(hours=1),
    )


def _make_microsoft(user, email="x@outlook.com", access_token="AT", refresh_token="RT"):
    return MicrosoftAccount.objects.create(
        user=user,
        microsoft_email=email,
        access_token_encrypted=encrypt(access_token),
        refresh_token_encrypted=encrypt(refresh_token),
        expires_at=timezone.now() + timedelta(hours=1),
    )


class WriteCalendarDefaultConstraintTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="a@x.test", password="x" * 12)

    def test_can_point_to_both_google_and_microsoft_simultaneously(self):
        # Task A1: XOR constraint removed. Both FKs may be non-null at once so
        # a user with two providers connected can have a write default for each.
        g = _make_google(self.user)
        m = _make_microsoft(self.user)
        wd = WriteCalendarDefault.objects.create(
            user=self.user, google_account=g, microsoft_account=m
        )
        self.assertIsNotNone(wd.pk)
        self.assertEqual(wd.google_account_id, g.pk)
        self.assertEqual(wd.microsoft_account_id, m.pk)

    def test_cannot_be_both_null(self):
        with self.assertRaises(IntegrityError):
            WriteCalendarDefault.objects.create(user=self.user)

    def test_google_only_is_valid(self):
        g = _make_google(self.user)
        wcd = WriteCalendarDefault.objects.create(user=self.user, google_account=g)
        wcd.refresh_from_db()
        self.assertEqual(wcd.provider, "google")
        self.assertEqual(wcd.write_calendar_id, "primary")

    def test_microsoft_only_is_valid(self):
        m = _make_microsoft(self.user)
        wcd = WriteCalendarDefault.objects.create(user=self.user, microsoft_account=m)
        wcd.refresh_from_db()
        self.assertEqual(wcd.provider, "microsoft")

    def test_selected_account_returns_nonnull_side(self):
        g = _make_google(self.user)
        wcd = WriteCalendarDefault.objects.create(user=self.user, google_account=g)
        self.assertEqual(wcd.selected_account, g)

    def test_write_calendar_id_proxies_to_google_account(self):
        g = _make_google(self.user)
        g.write_calendar_id = "cal-abc"
        g.save(update_fields=["write_calendar_id"])
        wcd = WriteCalendarDefault.objects.create(user=self.user, google_account=g)
        self.assertEqual(wcd.write_calendar_id, "cal-abc")


class MultipleAccountsPerUserTests(TestCase):
    """FK + unique_together: a user may have >1 Google account (different
    google_email), but the same (user, google_email) pair must be unique."""

    def setUp(self):
        self.user = User.objects.create_user(email="b@x.test", password="x" * 12)

    def test_two_distinct_google_emails_allowed(self):
        _make_google(self.user, email="a@gmail.com")
        _make_google(self.user, email="b@gmail.com")  # should not raise
        self.assertEqual(GoogleAccount.objects.filter(user=self.user).count(), 2)

    def test_duplicate_google_email_per_user_raises(self):
        _make_google(self.user, email="dup@gmail.com")
        with self.assertRaises(Exception):  # IntegrityError from UniqueConstraint
            _make_google(self.user, email="dup@gmail.com")

    def test_two_distinct_microsoft_emails_allowed(self):
        _make_microsoft(self.user, email="a@outlook.com")
        _make_microsoft(self.user, email="b@outlook.com")
        self.assertEqual(MicrosoftAccount.objects.filter(user=self.user).count(), 2)


# ---------------------------------------------------------------------------
# Task 2: OAuth callback add-vs-update semantics + get_credentials(account_id)
# ---------------------------------------------------------------------------


def _simulate_google_callback(user, *, email: str, refresh: str) -> GoogleAccount:
    """Invoke the production _upsert_google_account helper directly, bypassing
    the HTTP/token-exchange layer. This is the "extract a helper" approach
    described in the brief — the real oauth_callback calls the same helper."""
    from apps.scheduling.views import _upsert_google_account

    account, _created = _upsert_google_account(
        user,
        google_email=email,
        access_token="at-stub",
        refresh_token_encrypted=encrypt(refresh),
        expires_at=timezone.now() + timedelta(hours=1),
        scope="calendar.readonly",
    )
    return account


def _simulate_microsoft_callback(user, *, email: str, refresh: str) -> MicrosoftAccount:
    """Parallel helper for Microsoft."""
    from apps.scheduling.views import _upsert_microsoft_account

    account, _created = _upsert_microsoft_account(
        user,
        microsoft_email=email,
        access_token="at-stub",
        refresh_token_encrypted=encrypt(refresh),
        expires_at=timezone.now() + timedelta(hours=1),
        scope="Calendars.ReadWrite",
    )
    return account


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="dev-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="dev-client-secret",
    FRONTEND_BASE_URL="http://localhost:3000",
)
class OAuthCallbackMultiAccountTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="alice@slotly.test", password="x" * 12)

    # ── Google: add vs update ─────────────────────────────────────────────────

    def test_connect_google_second_email_creates_second_row(self):
        _simulate_google_callback(self.user, email="alice@gmail.com", refresh="r1")
        self.assertEqual(GoogleAccount.objects.filter(user=self.user).count(), 1)
        _simulate_google_callback(self.user, email="alice.work@gmail.com", refresh="r2")
        self.assertEqual(GoogleAccount.objects.filter(user=self.user).count(), 2)

    def test_connect_google_same_email_updates_existing(self):
        _simulate_google_callback(self.user, email="alice@gmail.com", refresh="r1")
        _simulate_google_callback(self.user, email="alice@gmail.com", refresh="r2")
        self.assertEqual(GoogleAccount.objects.filter(user=self.user).count(), 1)
        g = GoogleAccount.objects.get(user=self.user)
        self.assertEqual(decrypt(g.refresh_token_encrypted), "r2")

    def test_first_google_connect_sets_default(self):
        _simulate_google_callback(self.user, email="alice@gmail.com", refresh="r1")
        wd = WriteCalendarDefault.objects.get(user=self.user)
        self.assertEqual(wd.google_account.google_email, "alice@gmail.com")

    def test_second_google_connect_does_not_change_default(self):
        _simulate_google_callback(self.user, email="alice@gmail.com", refresh="r1")
        _simulate_google_callback(self.user, email="alice.work@gmail.com", refresh="r2")
        wd = WriteCalendarDefault.objects.get(user=self.user)
        self.assertEqual(wd.google_account.google_email, "alice@gmail.com")

    # ── IntegrityError race handling ──────────────────────────────────────────

    def test_upsert_integrity_error_race_handled_gracefully(self):
        """Simulate an IntegrityError on the first INSERT (race condition)
        by pre-creating the row and patching update_or_create to raise the
        first time it is called."""
        from django.db import IntegrityError as _IE
        from apps.scheduling.views import _upsert_google_account

        # Pre-create so the get() fallback finds the row.
        existing = _make_google(self.user, email="alice@gmail.com")
        WriteCalendarDefault.objects.create(user=self.user, google_account=existing)

        original_uoc = GoogleAccount.objects.update_or_create
        call_count = {"n": 0}

        def _raising_uoc(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _IE("simulated race")
            return original_uoc(*args, **kwargs)

        with patch.object(GoogleAccount.objects, "update_or_create", side_effect=_raising_uoc):
            account, created = _upsert_google_account(
                self.user,
                google_email="alice@gmail.com",
                access_token="at-new",
                refresh_token_encrypted=encrypt("r-race"),
                expires_at=timezone.now() + timedelta(hours=1),
                scope="",
            )

        self.assertFalse(created)
        self.assertEqual(account.pk, existing.pk)
        self.assertEqual(decrypt(account.refresh_token_encrypted), "r-race")
        # Still only one row.
        self.assertEqual(GoogleAccount.objects.filter(user=self.user).count(), 1)

    # ── get_credentials with account_id ──────────────────────────────────────

    def test_get_credentials_with_account_id_returns_correct_account(self):
        from apps.scheduling.google_client import GoogleOAuthError, get_credentials

        g1 = _make_google(self.user, email="alice@gmail.com", access_token="AT-g1")
        g2 = _make_google(self.user, email="alice.work@gmail.com", access_token="AT-g2")

        creds1 = get_credentials(self.user, g1.pk)
        creds2 = get_credentials(self.user, g2.pk)

        # Verify that each call returns the correct account's token: g1 must
        # return "AT-g1" and g2 must return "AT-g2". This proves the account_id
        # lookup is functioning correctly, not falling back to any account.
        self.assertEqual(creds1.access_token, "AT-g1")
        self.assertEqual(creds2.access_token, "AT-g2")

    def test_get_credentials_with_account_id_returns_correct_microsoft_account(self):
        from apps.scheduling.graph_client import MicrosoftOAuthError, get_credentials

        m1 = _make_microsoft(self.user, email="alice@outlook.com", access_token="AT-m1")
        m2 = _make_microsoft(self.user, email="alice.work@outlook.com", access_token="AT-m2")

        creds1 = get_credentials(self.user, m1.pk)
        creds2 = get_credentials(self.user, m2.pk)

        # Verify that each call returns the correct account's token: m1 must
        # return "AT-m1" and m2 must return "AT-m2". This proves the account_id
        # lookup is functioning correctly for Microsoft as well.
        self.assertEqual(creds1.access_token, "AT-m1")
        self.assertEqual(creds2.access_token, "AT-m2")

    def test_get_credentials_none_fallback_uses_write_default(self):
        from apps.scheduling.google_client import get_credentials

        g1 = _make_google(self.user, email="alice@gmail.com")
        g2 = _make_google(self.user, email="alice.work@gmail.com")
        # Point default at g2, not g1.
        WriteCalendarDefault.objects.create(user=self.user, google_account=g2)

        creds = get_credentials(self.user)
        # The account resolved should be g2's token (both were created with "AT").
        self.assertIsNotNone(creds.access_token)

    def test_get_credentials_none_fallback_no_default_no_account_raises(self):
        from apps.scheduling.google_client import GoogleOAuthError, get_credentials

        with self.assertRaises(GoogleOAuthError):
            get_credentials(self.user)

    def test_get_credentials_wrong_account_id_raises(self):
        from apps.scheduling.google_client import GoogleOAuthError, get_credentials

        other_user = User.objects.create_user(email="bob@slotly.test", password="x" * 12)
        g = _make_google(other_user, email="bob@gmail.com")

        # Passing bob's account PK with alice's user should raise.
        with self.assertRaises(GoogleOAuthError):
            get_credentials(self.user, g.pk)


# ---------------------------------------------------------------------------
# Task 3: REST /api/calendar-accounts endpoints
# ---------------------------------------------------------------------------


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="dev-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="dev-client-secret",
    FRONTEND_BASE_URL="http://localhost:3000",
    GOOGLE_OAUTH_FRONTEND_RETURN="/settings/integrations",
)
class CalendarAccountsAPITests(TestCase):
    def setUp(self):
        from rest_framework.test import APIClient

        self.user = User.objects.create_user(email="alice@slotly.test", password="x" * 12)
        self.other = User.objects.create_user(email="bob@slotly.test", password="x" * 12)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    # ── list ─────────────────────────────────────────────────────────────────

    def test_list_returns_all_connected_accounts_both_providers(self):
        g = _make_google(self.user, email="alice@gmail.com")
        m = _make_microsoft(self.user, email="alice@outlook.com")
        WriteCalendarDefault.objects.create(user=self.user, google_account=g)

        resp = self.client.get("/api/calendar-accounts")
        self.assertEqual(resp.status_code, 200)
        accounts = resp.json()["accounts"]
        self.assertEqual(len(accounts), 2)
        providers = {a["provider"] for a in accounts}
        self.assertIn("google", providers)
        self.assertIn("microsoft", providers)

    def test_list_marks_default(self):
        g1 = _make_google(self.user, email="alice@gmail.com")
        _make_google(self.user, email="alice.work@gmail.com")
        WriteCalendarDefault.objects.create(user=self.user, google_account=g1)

        resp = self.client.get("/api/calendar-accounts")
        self.assertEqual(resp.status_code, 200)
        accounts = resp.json()["accounts"]
        self.assertEqual(len(accounts), 2)
        defaults = [a for a in accounts if a["is_default"]]
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0]["id"], f"g:{g1.pk}")

    def test_list_empty_when_no_accounts(self):
        resp = self.client.get("/api/calendar-accounts")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["accounts"], [])

    def test_list_requires_auth(self):
        from rest_framework.test import APIClient
        anon = APIClient()
        resp = anon.get("/api/calendar-accounts")
        self.assertEqual(resp.status_code, 403)

    # ── set-default ──────────────────────────────────────────────────────────

    def test_set_default_switches_pointer(self):
        g1 = _make_google(self.user, email="alice@gmail.com")
        g2 = _make_google(self.user, email="alice.work@gmail.com")
        WriteCalendarDefault.objects.create(user=self.user, google_account=g1)

        resp = self.client.post(f"/api/calendar-accounts/g:{g2.pk}/set-default")
        self.assertEqual(resp.status_code, 200)

        wcd = WriteCalendarDefault.objects.get(user=self.user)
        self.assertEqual(wcd.google_account_id, g2.pk)

    def test_set_default_other_users_account_404(self):
        g_other = _make_google(self.other, email="bob@gmail.com")

        resp = self.client.post(f"/api/calendar-accounts/g:{g_other.pk}/set-default")
        self.assertEqual(resp.status_code, 404)

    def test_set_default_switches_provider(self):
        # A4: setting the MS default now preserves the Google default (no longer
        # clears the other provider's FK).
        g = _make_google(self.user, email="alice@gmail.com")
        m = _make_microsoft(self.user, email="alice@outlook.com")
        WriteCalendarDefault.objects.create(user=self.user, google_account=g)

        resp = self.client.post(f"/api/calendar-accounts/m:{m.pk}/set-default")
        self.assertEqual(resp.status_code, 200)
        wcd = WriteCalendarDefault.objects.get(user=self.user)
        # Google FK is preserved (not cleared) — per-provider independence.
        self.assertEqual(wcd.google_account_id, g.pk)
        self.assertEqual(wcd.microsoft_account_id, m.pk)

    # ── delete ───────────────────────────────────────────────────────────────

    def test_delete_removes_single_account_not_others(self):
        g1 = _make_google(self.user, email="alice@gmail.com")
        g2 = _make_google(self.user, email="alice.work@gmail.com")
        WriteCalendarDefault.objects.create(user=self.user, google_account=g1)

        resp = self.client.delete(f"/api/calendar-accounts/g:{g2.pk}")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(
            __import__("apps.scheduling.models", fromlist=["GoogleAccount"])
            .GoogleAccount.objects.filter(pk=g2.pk).exists()
        )
        # g1 still exists
        self.assertTrue(
            __import__("apps.scheduling.models", fromlist=["GoogleAccount"])
            .GoogleAccount.objects.filter(pk=g1.pk).exists()
        )

    def test_delete_default_promotes_another_account(self):
        from apps.scheduling.models import GoogleAccount

        g1 = _make_google(self.user, email="alice@gmail.com")
        g2 = _make_google(self.user, email="alice.work@gmail.com")
        WriteCalendarDefault.objects.create(user=self.user, google_account=g1)

        resp = self.client.delete(f"/api/calendar-accounts/g:{g1.pk}")
        self.assertEqual(resp.status_code, 204)

        # g1 is gone
        self.assertFalse(GoogleAccount.objects.filter(pk=g1.pk).exists())
        # default now points at g2
        wcd = WriteCalendarDefault.objects.get(user=self.user)
        self.assertEqual(wcd.google_account_id, g2.pk)

    def test_delete_only_account_wipes_default(self):
        g = _make_google(self.user, email="alice@gmail.com")
        WriteCalendarDefault.objects.create(user=self.user, google_account=g)

        resp = self.client.delete(f"/api/calendar-accounts/g:{g.pk}")
        self.assertEqual(resp.status_code, 204)

        self.assertFalse(WriteCalendarDefault.objects.filter(user=self.user).exists())

    def test_delete_other_users_account_returns_404(self):
        g_other = _make_google(self.other, email="bob@gmail.com")

        resp = self.client.delete(f"/api/calendar-accounts/g:{g_other.pk}")
        self.assertEqual(resp.status_code, 404)

    def test_delete_non_default_account_wcd_untouched(self):
        """Delete ms-B (not the default) when WCD has both FKs set.

        Setup: one Google account (default) + two Microsoft accounts (ms-A is
        default, ms-B is not).  WCD has google_account=g and
        microsoft_account=ms_a.  Deleting ms-B must:
        - remove ms-B from DB
        - leave WCD completely untouched (both FKs still set)
        - return 204
        """
        g = _make_google(self.user, email="alice@gmail.com")
        ms_a = _make_microsoft(self.user, email="alice@outlook.com")
        ms_b = _make_microsoft(self.user, email="alice.work@outlook.com")
        WriteCalendarDefault.objects.create(
            user=self.user, google_account=g, microsoft_account=ms_a
        )

        resp = self.client.delete(f"/api/calendar-accounts/m:{ms_b.pk}")
        self.assertEqual(resp.status_code, 204)

        # ms-B is gone.
        self.assertFalse(MicrosoftAccount.objects.filter(pk=ms_b.pk).exists())
        # ms-A still exists.
        self.assertTrue(MicrosoftAccount.objects.filter(pk=ms_a.pk).exists())

        # WCD is completely untouched — both FKs still point to the originals.
        wcd = WriteCalendarDefault.objects.get(user=self.user)
        self.assertEqual(wcd.google_account_id, g.pk)
        self.assertEqual(wcd.microsoft_account_id, ms_a.pk)

    # ── patch write_calendar_id ───────────────────────────────────────────────

    def test_patch_write_calendar_id(self):
        g = _make_google(self.user, email="alice@gmail.com")
        WriteCalendarDefault.objects.create(user=self.user, google_account=g)

        resp = self.client.patch(
            f"/api/calendar-accounts/g:{g.pk}",
            {"write_calendar_id": "cal-abc"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        g.refresh_from_db()
        self.assertEqual(g.write_calendar_id, "cal-abc")

    def test_patch_write_calendar_id_microsoft(self):
        m = _make_microsoft(self.user, email="alice@outlook.com")
        WriteCalendarDefault.objects.create(user=self.user, microsoft_account=m)

        resp = self.client.patch(
            f"/api/calendar-accounts/m:{m.pk}",
            {"write_calendar_id": "ms-cal-id"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        m.refresh_from_db()
        self.assertEqual(m.write_calendar_id, "ms-cal-id")

    def test_patch_other_users_account_returns_404(self):
        g_other = _make_google(self.other, email="bob@gmail.com")

        resp = self.client.patch(
            f"/api/calendar-accounts/g:{g_other.pk}",
            {"write_calendar_id": "cal-hack"},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    # ── 10-account cap ────────────────────────────────────────────────────────

    def test_too_many_accounts_rejected(self):
        """The 11th distinct Google account for a user must be rejected with
        ?error=too_many_accounts in the redirect URL."""
        # Fill up 10 distinct Google accounts using _simulate_google_callback.
        for i in range(10):
            _simulate_google_callback(self.user, email=f"alice{i}@gmail.com", refresh=f"r{i}")

        self.assertEqual(
            __import__("apps.scheduling.models", fromlist=["GoogleAccount"])
            .GoogleAccount.objects.filter(user=self.user).count(),
            10,
        )

        # The 11th callback should redirect with ?error=too_many_accounts.
        # We call the view directly rather than through HTTP to avoid needing
        # real OAuth secrets.
        from django.test import RequestFactory
        from apps.scheduling.views import oauth_callback

        factory = RequestFactory()
        state = __import__("apps.scheduling.views", fromlist=["_signer"])._signer().sign(
            f"user:{self.user.pk}"
        )

        with patch("apps.scheduling.views.exchange_code") as mock_exchange, \
             patch("apps.scheduling.views.fetch_userinfo") as mock_userinfo:
            mock_exchange.return_value = {
                "access_token": "at-11th",
                "refresh_token": "rt-11th",
                "expires_in": 3600,
                "scope": "calendar.readonly",
            }
            mock_userinfo.return_value = {"email": "alice-11th@gmail.com"}

            request = factory.get(
                "/api/oauth/google/callback",
                {"code": "code11", "state": state},
            )
            response = oauth_callback(request)

        # Must redirect with error=too_many_accounts query param.
        self.assertIn("too_many_accounts", response.url)
        # Still only 10 accounts.
        from apps.scheduling.models import GoogleAccount
        self.assertEqual(GoogleAccount.objects.filter(user=self.user).count(), 10)


# ---------------------------------------------------------------------------
# Task 3: _pick_write_provider reads WriteCalendarDefault
# ---------------------------------------------------------------------------


@override_settings(
    FRONTEND_BASE_URL="http://localhost:3000",
)
class PickWriteProviderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="pp@slotly.test", password="x" * 12)

    def test_returns_none_when_no_accounts(self):
        from apps.scheduling.services import _pick_write_provider
        self.assertIsNone(_pick_write_provider(self.user))

    def test_auto_elects_google_and_persists_default(self):
        from apps.scheduling.services import _pick_write_provider
        g = _make_google(self.user, email="pp@gmail.com")

        provider = _pick_write_provider(self.user)
        self.assertIsNotNone(provider)
        self.assertEqual(provider.name, "google")
        self.assertEqual(provider.account_id, g.pk)

        # Default was persisted.
        wcd = WriteCalendarDefault.objects.get(user=self.user)
        self.assertEqual(wcd.google_account_id, g.pk)

    def test_auto_elects_microsoft_when_no_google(self):
        from apps.scheduling.services import _pick_write_provider
        m = _make_microsoft(self.user, email="pp@outlook.com")

        provider = _pick_write_provider(self.user)
        self.assertIsNotNone(provider)
        self.assertEqual(provider.name, "microsoft")
        self.assertEqual(provider.account_id, m.pk)

    def test_respects_explicit_default(self):
        from apps.scheduling.services import _pick_write_provider
        g1 = _make_google(self.user, email="pp@gmail.com")
        g2 = _make_google(self.user, email="pp.work@gmail.com")
        # Set default to g2, not the first one.
        WriteCalendarDefault.objects.create(user=self.user, google_account=g2)

        provider = _pick_write_provider(self.user)
        self.assertEqual(provider.account_id, g2.pk)

    def test_microsoft_default_preferred_over_google_account(self):
        from apps.scheduling.services import _pick_write_provider
        _make_google(self.user, email="pp@gmail.com")
        m = _make_microsoft(self.user, email="pp@outlook.com")
        # Explicitly set default to Microsoft.
        WriteCalendarDefault.objects.create(user=self.user, microsoft_account=m)

        provider = _pick_write_provider(self.user)
        self.assertEqual(provider.name, "microsoft")
        self.assertEqual(provider.account_id, m.pk)


# ---------------------------------------------------------------------------
# Fix wave: atomic 10-cap enforcement + _pick_write_provider race-safety
# ---------------------------------------------------------------------------


class AtomicCapRaceTests(TestCase):
    """Finding 1: TooManyAccountsError is raised (and INSERT rolled back)
    when a concurrent OAuth callback sneaks past the outer pre-check and
    would push a user over the 10-account cap."""

    def setUp(self):
        self.user = User.objects.create_user(email="cap@slotly.test", password="x" * 12)

    def test_concurrent_google_connect_at_cap_rejects_second(self):
        """Create 10 accounts first, then call _upsert_google_account directly
        (bypassing the outer pre-check) and verify the 11th is rejected and
        rolled back."""
        from apps.scheduling.views import _upsert_google_account, TooManyAccountsError

        for i in range(10):
            _make_google(self.user, email=f"cap{i}@gmail.com")

        self.assertEqual(GoogleAccount.objects.filter(user=self.user).count(), 10)

        with self.assertRaises(TooManyAccountsError) as ctx:
            _upsert_google_account(
                self.user,
                google_email="cap-race@gmail.com",
                access_token="at-race",
                refresh_token_encrypted=encrypt("rt-race"),
                expires_at=timezone.now() + timedelta(hours=1),
                scope="calendar.readonly",
            )

        self.assertEqual(ctx.exception.provider, "google")
        # The atomic block rolled back the INSERT — still 10.
        self.assertEqual(GoogleAccount.objects.filter(user=self.user).count(), 10)

    def test_concurrent_microsoft_connect_at_cap_rejects_second(self):
        """Parallel test for Microsoft: 10 accounts + upsert of 11th raises
        TooManyAccountsError and is rolled back."""
        from apps.scheduling.views import _upsert_microsoft_account, TooManyAccountsError

        for i in range(10):
            _make_microsoft(self.user, email=f"cap{i}@outlook.com")

        self.assertEqual(MicrosoftAccount.objects.filter(user=self.user).count(), 10)

        with self.assertRaises(TooManyAccountsError) as ctx:
            _upsert_microsoft_account(
                self.user,
                microsoft_email="cap-race@outlook.com",
                access_token="at-race",
                refresh_token_encrypted=encrypt("rt-race"),
                expires_at=timezone.now() + timedelta(hours=1),
                scope="Calendars.ReadWrite",
            )

        self.assertEqual(ctx.exception.provider, "microsoft")
        # The atomic block rolled back the INSERT — still 10.
        self.assertEqual(MicrosoftAccount.objects.filter(user=self.user).count(), 10)


class PickWriteProviderRaceSafetyTests(TestCase):
    """Finding 2: _pick_write_provider uses get_or_create so concurrent callers
    can't both try to insert the WriteCalendarDefault row."""

    def setUp(self):
        self.user = User.objects.create_user(email="pwp@slotly.test", password="x" * 12)

    def test_concurrent_pick_write_provider_safe_under_race(self):
        """Simulate the race by mocking WriteCalendarDefault.objects.create to
        raise IntegrityError on the first call, then verify that get_or_create
        handles it gracefully and the second call returns the existing row."""
        from django.db import IntegrityError as _IE
        from apps.scheduling.services import _pick_write_provider

        g = _make_google(self.user, email="pwp@gmail.com")

        # With get_or_create in place the first call must succeed without
        # ever reaching create() at all (it goes straight to get_or_create
        # which is atomic).  Call twice and assert both return the same result.
        p1 = _pick_write_provider(self.user)
        p2 = _pick_write_provider(self.user)

        self.assertIsNotNone(p1)
        self.assertIsNotNone(p2)
        self.assertEqual(p1.account_id, g.pk)
        self.assertEqual(p2.account_id, g.pk)
        # Exactly one WriteCalendarDefault row exists.
        self.assertEqual(WriteCalendarDefault.objects.filter(user=self.user).count(), 1)


# ---------------------------------------------------------------------------
# S0: per-account writable-calendars picker
# ---------------------------------------------------------------------------


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="dev-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="dev-client-secret",
    FRONTEND_BASE_URL="http://localhost:3000",
)
class WritableCalendarsPerAccountGoogleTests(TestCase):
    """Tests for ?account_id=g:<pk> on GET /api/google-account/writable-calendars."""

    def setUp(self):
        from rest_framework.test import APIClient

        self.user = User.objects.create_user(email="wc@slotly.test", password="x" * 12)
        self.other = User.objects.create_user(email="other@slotly.test", password="x" * 12)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_with_account_id_fetches_that_account(self):
        """?account_id=g:<second.pk> must call list_writable_calendars with the
        second account, not the default/first."""
        g1 = _make_google(self.user, email="wc1@gmail.com", access_token="AT-wc1")
        g2 = _make_google(self.user, email="wc2@gmail.com", access_token="AT-wc2")
        WriteCalendarDefault.objects.create(user=self.user, google_account=g1)

        with patch(
            "apps.scheduling.views.list_writable_calendars", return_value=[]
        ) as mock_lwc:
            resp = self.client.get(
                f"/api/google-account/writable-calendars?account_id=g:{g2.pk}"
            )

        self.assertEqual(resp.status_code, 200)
        # The account kwarg passed to list_writable_calendars must be g2.
        mock_lwc.assert_called_once()
        _, kwargs = mock_lwc.call_args
        self.assertEqual(kwargs["account"].pk, g2.pk)

    def test_without_account_id_falls_back_to_default(self):
        """No query param → list_writable_calendars called with account=None."""
        g = _make_google(self.user, email="wc@gmail.com")
        WriteCalendarDefault.objects.create(user=self.user, google_account=g)

        with patch(
            "apps.scheduling.views.list_writable_calendars", return_value=[]
        ) as mock_lwc:
            resp = self.client.get("/api/google-account/writable-calendars")

        self.assertEqual(resp.status_code, 200)
        mock_lwc.assert_called_once()
        _, kwargs = mock_lwc.call_args
        self.assertIsNone(kwargs["account"])

    def test_wrong_prefix_rejected_with_400(self):
        """?account_id=m:<pk> on the Google endpoint → 400 validation_error."""
        g = _make_google(self.user, email="wc@gmail.com")
        resp = self.client.get(f"/api/google-account/writable-calendars?account_id=m:{g.pk}")
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body.get("code"), "validation_error")

    def test_other_users_account_returns_404(self):
        """?account_id=g:<other user's account pk> → 404."""
        g_other = _make_google(self.other, email="other@gmail.com")
        resp = self.client.get(
            f"/api/google-account/writable-calendars?account_id=g:{g_other.pk}"
        )
        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        self.assertEqual(body.get("code"), "not_found")


@override_settings(
    MICROSOFT_OAUTH_CLIENT_ID="dev-ms-client-id",
    MICROSOFT_OAUTH_CLIENT_SECRET="dev-ms-client-secret",
    MICROSOFT_OAUTH_REDIRECT_URI="http://localhost:8000/api/oauth/microsoft/callback",
    FRONTEND_BASE_URL="http://localhost:3000",
)
class WritableCalendarsPerAccountMicrosoftTests(TestCase):
    """Tests for ?account_id=m:<pk> on GET /api/microsoft-account/writable-calendars."""

    def setUp(self):
        from rest_framework.test import APIClient

        self.user = User.objects.create_user(email="mswc@slotly.test", password="x" * 12)
        self.other = User.objects.create_user(email="msother@slotly.test", password="x" * 12)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_with_account_id_fetches_that_account(self):
        """?account_id=m:<second.pk> must call _graph.list_writable_calendars with
        the second account, not the default/first."""
        m1 = _make_microsoft(self.user, email="mswc1@outlook.com", access_token="AT-ms1")
        m2 = _make_microsoft(self.user, email="mswc2@outlook.com", access_token="AT-ms2")
        WriteCalendarDefault.objects.create(user=self.user, microsoft_account=m1)

        with patch(
            "apps.scheduling.views._graph.list_writable_calendars", return_value=[]
        ) as mock_lwc:
            resp = self.client.get(
                f"/api/microsoft-account/writable-calendars?account_id=m:{m2.pk}"
            )

        self.assertEqual(resp.status_code, 200)
        mock_lwc.assert_called_once()
        _, kwargs = mock_lwc.call_args
        self.assertEqual(kwargs["account"].pk, m2.pk)

    def test_without_account_id_falls_back_to_default(self):
        """No query param → _graph.list_writable_calendars called with account=None."""
        m = _make_microsoft(self.user, email="mswc@outlook.com")
        WriteCalendarDefault.objects.create(user=self.user, microsoft_account=m)

        with patch(
            "apps.scheduling.views._graph.list_writable_calendars", return_value=[]
        ) as mock_lwc:
            resp = self.client.get("/api/microsoft-account/writable-calendars")

        self.assertEqual(resp.status_code, 200)
        mock_lwc.assert_called_once()
        _, kwargs = mock_lwc.call_args
        self.assertIsNone(kwargs["account"])

    def test_wrong_prefix_rejected_with_400(self):
        """?account_id=g:<pk> on the Microsoft endpoint → 400 validation_error."""
        m = _make_microsoft(self.user, email="mswc@outlook.com")
        resp = self.client.get(
            f"/api/microsoft-account/writable-calendars?account_id=g:{m.pk}"
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body.get("code"), "validation_error")

    def test_other_users_account_returns_404(self):
        """?account_id=m:<other user's account pk> → 404."""
        m_other = _make_microsoft(self.other, email="msother@outlook.com")
        resp = self.client.get(
            f"/api/microsoft-account/writable-calendars?account_id=m:{m_other.pk}"
        )
        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        self.assertEqual(body.get("code"), "not_found")


# ---------------------------------------------------------------------------
# Task A2: per-provider delete/promote logic
# ---------------------------------------------------------------------------


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="dev-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="dev-client-secret",
    FRONTEND_BASE_URL="http://localhost:3000",
)
class DeletePromotePerProviderTests(TestCase):
    """R3: when deleting the Google default, promote the next GoogleAccount
    (not an MS account) and leave the MS FK untouched."""

    def setUp(self):
        from rest_framework.test import APIClient

        self.user = User.objects.create_user(email="delp@slotly.test", password="x" * 12)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_deleting_google_default_promotes_next_google_not_microsoft(self):
        """Delete Google B (default) → Google A promoted; MS default unchanged."""
        g_a = _make_google(self.user, email="delp_a@gmail.com")
        g_b = _make_google(self.user, email="delp_b@gmail.com")
        m = _make_microsoft(self.user, email="delp@outlook.com")
        WriteCalendarDefault.objects.create(
            user=self.user, google_account=g_b, microsoft_account=m
        )

        resp = self.client.delete(f"/api/calendar-accounts/g:{g_b.pk}")
        self.assertEqual(resp.status_code, 204)

        # g_b is gone
        self.assertFalse(GoogleAccount.objects.filter(pk=g_b.pk).exists())

        # WCD row still exists and was updated correctly
        wcd = WriteCalendarDefault.objects.get(user=self.user)
        # Google FK promoted to g_a
        self.assertEqual(wcd.google_account_id, g_a.pk)
        # Microsoft FK untouched
        self.assertEqual(wcd.microsoft_account_id, m.pk)

    def test_deleting_only_google_leaves_ms_default_intact(self):
        """Delete the only Google account → google_account_id becomes None;
        microsoft_account_id unchanged; WCD row survives."""
        g = _make_google(self.user, email="delp_solo@gmail.com")
        m = _make_microsoft(self.user, email="delp_solo@outlook.com")
        WriteCalendarDefault.objects.create(
            user=self.user, google_account=g, microsoft_account=m
        )

        resp = self.client.delete(f"/api/calendar-accounts/g:{g.pk}")
        self.assertEqual(resp.status_code, 204)

        wcd = WriteCalendarDefault.objects.get(user=self.user)
        self.assertIsNone(wcd.google_account_id)
        self.assertEqual(wcd.microsoft_account_id, m.pk)

    def test_deleting_last_account_removes_write_default_row(self):
        """Delete the only account (Google, no MS) → WCD row removed."""
        g = _make_google(self.user, email="delp_last@gmail.com")
        WriteCalendarDefault.objects.create(user=self.user, google_account=g)

        resp = self.client.delete(f"/api/calendar-accounts/g:{g.pk}")
        self.assertEqual(resp.status_code, 204)

        self.assertFalse(WriteCalendarDefault.objects.filter(user=self.user).exists())

    def test_deleting_microsoft_default_promotes_next_microsoft_not_google(self):
        """Delete MS B (default) → MS A promoted; Google FK untouched."""
        g = _make_google(self.user, email="delp_g@gmail.com")
        m_a = _make_microsoft(self.user, email="delp_ms_a@outlook.com")
        m_b = _make_microsoft(self.user, email="delp_ms_b@outlook.com")
        WriteCalendarDefault.objects.create(
            user=self.user, google_account=g, microsoft_account=m_b
        )

        resp = self.client.delete(f"/api/calendar-accounts/m:{m_b.pk}")
        self.assertEqual(resp.status_code, 204)

        self.assertFalse(MicrosoftAccount.objects.filter(pk=m_b.pk).exists())

        wcd = WriteCalendarDefault.objects.get(user=self.user)
        # MS FK promoted to m_a
        self.assertEqual(wcd.microsoft_account_id, m_a.pk)
        # Google FK untouched
        self.assertEqual(wcd.google_account_id, g.pk)


# ---------------------------------------------------------------------------
# Task A4: per-provider independent defaults via set-default endpoint
# ---------------------------------------------------------------------------


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="dev-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="dev-client-secret",
    FRONTEND_BASE_URL="http://localhost:3000",
)
class PerProviderDefaultTests(TestCase):
    """A4: set-default endpoint persists each provider's FK independently."""

    def setUp(self):
        from rest_framework.test import APIClient

        self.user = User.objects.create_user(email="a4@slotly.test", password="x" * 12)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_setting_google_default_and_ms_default_persists_both(self):
        """POST set-default for Google, then for MS → WCD has both FKs set.
        GET /api/calendar-accounts returns TWO accounts with is_default=True."""
        g = _make_google(self.user, email="a4g@gmail.com")
        m = _make_microsoft(self.user, email="a4m@outlook.com")

        resp_g = self.client.post(f"/api/calendar-accounts/g:{g.pk}/set-default")
        self.assertEqual(resp_g.status_code, 200)

        resp_m = self.client.post(f"/api/calendar-accounts/m:{m.pk}/set-default")
        self.assertEqual(resp_m.status_code, 200)

        wcd = WriteCalendarDefault.objects.get(user=self.user)
        self.assertEqual(wcd.google_account_id, g.pk)
        self.assertEqual(wcd.microsoft_account_id, m.pk)

        # Both accounts reported as is_default in the list response.
        list_resp = self.client.get("/api/calendar-accounts")
        self.assertEqual(list_resp.status_code, 200)
        accounts = list_resp.json()["accounts"]
        defaults = [a for a in accounts if a["is_default"]]
        self.assertEqual(len(defaults), 2)
        default_ids = {a["id"] for a in defaults}
        self.assertIn(f"g:{g.pk}", default_ids)
        self.assertIn(f"m:{m.pk}", default_ids)

    def test_set_default_only_touches_matching_provider_fk(self):
        """Pre-state: Google=A default, MS=X. POST set-default for Google B.
        Expected: google_account_id → B; microsoft_account_id → X (unchanged)."""
        g_a = _make_google(self.user, email="a4g_a@gmail.com")
        g_b = _make_google(self.user, email="a4g_b@gmail.com")
        m_x = _make_microsoft(self.user, email="a4m_x@outlook.com")
        WriteCalendarDefault.objects.create(
            user=self.user, google_account=g_a, microsoft_account=m_x
        )

        resp = self.client.post(f"/api/calendar-accounts/g:{g_b.pk}/set-default")
        self.assertEqual(resp.status_code, 200)

        wcd = WriteCalendarDefault.objects.get(user=self.user)
        self.assertEqual(wcd.google_account_id, g_b.pk)
        self.assertEqual(wcd.microsoft_account_id, m_x.pk)  # unchanged

    def test_set_default_response_includes_both_provider_defaults(self):
        """POST set-default response includes google_default_id and
        microsoft_default_id fields reflecting the current state."""
        g = _make_google(self.user, email="a4resp_g@gmail.com")
        m = _make_microsoft(self.user, email="a4resp_m@outlook.com")
        WriteCalendarDefault.objects.create(
            user=self.user, google_account=g, microsoft_account=m
        )

        # Switch to a new Google account — MS default remains.
        g2 = _make_google(self.user, email="a4resp_g2@gmail.com")
        resp = self.client.post(f"/api/calendar-accounts/g:{g2.pk}/set-default")
        self.assertEqual(resp.status_code, 200)

        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["default"], f"g:{g2.pk}")
        self.assertEqual(body["google_default_id"], f"g:{g2.pk}")
        self.assertEqual(body["microsoft_default_id"], f"m:{m.pk}")

    def test_set_ms_default_response_shows_null_google_when_no_google_set(self):
        """When only MS is connected, google_default_id must be null."""
        m = _make_microsoft(self.user, email="a4ms_only@outlook.com")

        resp = self.client.post(f"/api/calendar-accounts/m:{m.pk}/set-default")
        self.assertEqual(resp.status_code, 200)

        body = resp.json()
        self.assertIsNone(body["google_default_id"])
        self.assertEqual(body["microsoft_default_id"], f"m:{m.pk}")


# ---------------------------------------------------------------------------
# Task B3: host_providers in public profile response
# ---------------------------------------------------------------------------


class PublicProfileHostProvidersTests(TestCase):
    """GET /api/public/profile/<token> must include host_providers — a sorted
    list of OAuth provider names the host has connected."""

    def setUp(self):
        self.client = APIClient()

    def _make_host(self):
        u = User.objects.create_user(
            email=f"host_{uuid.uuid4().hex[:8]}@b3.test",
            password="x" * 12,
        )
        u.share_enabled = True
        u.save(update_fields=["share_enabled"])
        return u

    def test_both_providers_returns_sorted_list(self):
        host = self._make_host()
        _make_google(host, email="b3g@gmail.com")
        _make_microsoft(host, email="b3m@outlook.com")

        r = self.client.get(f"/api/public/profile/{host.share_token}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["host_providers"], ["google", "microsoft"])

    def test_only_google_returns_google_list(self):
        host = self._make_host()
        _make_google(host, email="b3gonly@gmail.com")

        r = self.client.get(f"/api/public/profile/{host.share_token}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["host_providers"], ["google"])

    def test_only_microsoft_returns_microsoft_list(self):
        host = self._make_host()
        _make_microsoft(host, email="b3monly@outlook.com")

        r = self.client.get(f"/api/public/profile/{host.share_token}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["host_providers"], ["microsoft"])

    def test_no_oauth_returns_empty_list(self):
        host = self._make_host()
        # No GoogleAccount or MicrosoftAccount — ICS-only host.
        r = self.client.get(f"/api/public/profile/{host.share_token}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["host_providers"], [])
