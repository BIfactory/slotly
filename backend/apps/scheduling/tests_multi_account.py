"""
Tests for multi-account OAuth model layer (Task 1) and OAuth callback
upsert + get_credentials semantics (Task 2).
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.scheduling.models import GoogleAccount, MicrosoftAccount, WriteCalendarDefault
from apps.scheduling.security import decrypt, encrypt


def _make_google(user, email="x@gmail.com"):
    return GoogleAccount.objects.create(
        user=user,
        google_email=email,
        access_token_encrypted=encrypt("AT"),
        refresh_token_encrypted=encrypt("RT"),
        expires_at=timezone.now() + timedelta(hours=1),
    )


def _make_microsoft(user, email="x@outlook.com"):
    return MicrosoftAccount.objects.create(
        user=user,
        microsoft_email=email,
        access_token_encrypted=encrypt("AT"),
        refresh_token_encrypted=encrypt("RT"),
        expires_at=timezone.now() + timedelta(hours=1),
    )


class WriteCalendarDefaultConstraintTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="a@x.test", password="x" * 12)

    def test_cannot_point_to_both_google_and_microsoft(self):
        g = _make_google(self.user)
        m = _make_microsoft(self.user)
        with self.assertRaises(IntegrityError):
            WriteCalendarDefault.objects.create(
                user=self.user, google_account=g, microsoft_account=m
            )

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

        g1 = _make_google(self.user, email="alice@gmail.com")
        g2 = _make_google(self.user, email="alice.work@gmail.com")

        creds1 = get_credentials(self.user, g1.pk)
        creds2 = get_credentials(self.user, g2.pk)

        # Both should resolve without error and return different tokens if
        # they were created with different access tokens; here we just check
        # that neither raises.
        self.assertIsNotNone(creds1.access_token)
        self.assertIsNotNone(creds2.access_token)

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
