"""Tests for OAuth-driven busy intervals (OAuthBusyWindow model + sync tasks + search integration)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

from django.test import TestCase

from apps.accounts.models import User
from apps.scheduling.models import GoogleAccount, MicrosoftAccount, OAuthBusyWindow
from apps.scheduling.tasks import sync_one_oauth_account


class OAuthBusyWindowSchemaTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(email="a@x.com", password="x" * 12)
        self.g = GoogleAccount.objects.create(
            user=self.alice,
            google_email="a@gmail.com",
            access_token_encrypted="x",
            refresh_token_encrypted="x",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    def test_create_and_query_by_user(self):
        now = datetime.now(timezone.utc)
        OAuthBusyWindow.objects.create(
            user=self.alice,
            provider="google",
            google_account=self.g,
            calendar_id="primary",
            starts_at=now,
            ends_at=now + timedelta(hours=1),
        )
        self.assertEqual(OAuthBusyWindow.objects.filter(user=self.alice).count(), 1)

    def test_cascade_delete_with_account(self):
        now = datetime.now(timezone.utc)
        OAuthBusyWindow.objects.create(
            user=self.alice,
            provider="google",
            google_account=self.g,
            calendar_id="primary",
            starts_at=now,
            ends_at=now + timedelta(hours=1),
        )
        self.g.delete()
        self.assertEqual(OAuthBusyWindow.objects.count(), 0)

    def test_no_title_field(self):
        """Privacy baseline — model MUST NOT have a title or description field."""
        field_names = {f.name for f in OAuthBusyWindow._meta.get_fields()}
        for forbidden in ("title", "summary", "description", "location", "attendees"):
            self.assertNotIn(
                forbidden,
                field_names,
                f"{forbidden} field would leak privacy — see PRD §5.2 and F15-R9",
            )

    def test_microsoft_cascade_delete(self):
        """OAuthBusyWindow rows for a MicrosoftAccount are deleted when the account is deleted."""
        ms = MicrosoftAccount.objects.create(
            user=self.alice,
            microsoft_email="a@outlook.com",
            access_token_encrypted="x",
            refresh_token_encrypted="x",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        now = datetime.now(timezone.utc)
        OAuthBusyWindow.objects.create(
            user=self.alice,
            provider="microsoft",
            microsoft_account=ms,
            calendar_id="primary",
            starts_at=now,
            ends_at=now + timedelta(hours=1),
        )
        ms.delete()
        self.assertEqual(OAuthBusyWindow.objects.count(), 0)


# ---------------------------------------------------------------------------
# Sync task tests (Task 2)
# ---------------------------------------------------------------------------

def _make_busy(cal_id="primary", hours_from_now=2, duration=1):
    """Helper: return a (cal_id, starts_at, ends_at) tuple."""
    now = datetime.now(timezone.utc)
    return (cal_id, now + timedelta(hours=hours_from_now), now + timedelta(hours=hours_from_now + duration))


class SyncOneOAuthAccountGoogleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="bob@x.com", password="x" * 12)
        self.g = GoogleAccount.objects.create(
            user=self.user,
            google_email="bob@gmail.com",
            access_token_encrypted="x",
            refresh_token_encrypted="x",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    @patch("apps.scheduling.google_client.list_all_calendars")
    @patch("apps.scheduling.google_client.fetch_freebusy")
    def test_sync_writes_busy_windows(self, mock_freebusy, mock_calendars):
        """Successful sync writes OAuthBusyWindow rows for the account."""
        busy_rows = [_make_busy("primary"), _make_busy("work@example.com")]
        mock_calendars.return_value = [{"id": "primary"}, {"id": "work@example.com"}]
        mock_freebusy.return_value = busy_rows

        result = sync_one_oauth_account("google", self.g.pk)

        self.assertEqual(result["written"], 2)
        self.assertEqual(OAuthBusyWindow.objects.filter(google_account=self.g).count(), 2)

    @patch("apps.scheduling.google_client.list_all_calendars")
    @patch("apps.scheduling.google_client.fetch_freebusy")
    def test_sync_replaces_old_rows(self, mock_freebusy, mock_calendars):
        """Sync atomically replaces existing rows for this account (idempotent)."""
        now = datetime.now(timezone.utc)
        # Pre-existing stale row
        OAuthBusyWindow.objects.create(
            user=self.user, provider="google", google_account=self.g,
            calendar_id="stale", starts_at=now, ends_at=now + timedelta(hours=1),
        )

        mock_calendars.return_value = [{"id": "primary"}]
        mock_freebusy.return_value = [_make_busy("primary")]

        sync_one_oauth_account("google", self.g.pk)

        windows = list(OAuthBusyWindow.objects.filter(google_account=self.g))
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].calendar_id, "primary")

    @patch("apps.scheduling.google_client.list_all_calendars")
    def test_broken_oauth_token_skips_preserves_rows(self, mock_calendars):
        """Broken OAuth token: task skips account, keeps existing OAuthBusyWindow rows."""
        from apps.scheduling.google_client import GoogleOAuthError

        now = datetime.now(timezone.utc)
        OAuthBusyWindow.objects.create(
            user=self.user, provider="google", google_account=self.g,
            calendar_id="primary", starts_at=now, ends_at=now + timedelta(hours=1),
        )

        mock_calendars.side_effect = GoogleOAuthError("token revoked")

        result = sync_one_oauth_account("google", self.g.pk)

        self.assertIn("error", result)
        # Existing rows must survive
        self.assertEqual(OAuthBusyWindow.objects.filter(google_account=self.g).count(), 1)

    @patch("apps.scheduling.google_client.list_all_calendars")
    @patch("apps.scheduling.google_client.fetch_freebusy")
    def test_new_calendar_mid_cycle_picked_up(self, mock_freebusy, mock_calendars):
        """Re-listing calendarList on each poll picks up newly added calendars.
        This test covers the 'new calendar mid-cycle' review focus case.
        """
        # First poll: one calendar
        mock_calendars.return_value = [{"id": "primary"}]
        mock_freebusy.return_value = [_make_busy("primary")]
        sync_one_oauth_account("google", self.g.pk)
        self.assertEqual(OAuthBusyWindow.objects.filter(google_account=self.g).count(), 1)

        # Second poll: user added a new calendar on Google side
        mock_calendars.return_value = [{"id": "primary"}, {"id": "new-cal@group.calendar.google.com"}]
        mock_freebusy.return_value = [
            _make_busy("primary"),
            _make_busy("new-cal@group.calendar.google.com"),
        ]
        sync_one_oauth_account("google", self.g.pk)

        windows = list(OAuthBusyWindow.objects.filter(google_account=self.g).order_by("calendar_id"))
        self.assertEqual(len(windows), 2)
        cal_ids = {w.calendar_id for w in windows}
        self.assertIn("new-cal@group.calendar.google.com", cal_ids)

    def test_skips_nonexistent_account(self):
        """sync_one_oauth_account returns skipped dict if account was deleted."""
        result = sync_one_oauth_account("google", 99999)
        self.assertTrue(result.get("skipped"))


# ---------------------------------------------------------------------------
# Multi-account cascade + per-account isolation tests (Task 3)
# ---------------------------------------------------------------------------

class MultiAccountIsolationTests(TestCase):
    """Review Focus: multi-account per user (post-PR #8)."""

    def setUp(self):
        self.user = User.objects.create_user(email="dave@x.com", password="x" * 12)
        self.g1 = GoogleAccount.objects.create(
            user=self.user,
            google_email="dave@gmail.com",
            access_token_encrypted="x",
            refresh_token_encrypted="x",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        self.g2 = GoogleAccount.objects.create(
            user=self.user,
            google_email="dave@work.com",
            access_token_encrypted="x",
            refresh_token_encrypted="x",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    def _create_window(self, account, calendar_id="primary"):
        now = datetime.now(timezone.utc)
        return OAuthBusyWindow.objects.create(
            user=self.user,
            provider="google",
            google_account=account,
            calendar_id=calendar_id,
            starts_at=now,
            ends_at=now + timedelta(hours=1),
        )

    def test_delete_account_a_keeps_account_b_rows(self):
        """Deleting g1 must not affect g2's OAuthBusyWindow rows."""
        self._create_window(self.g1, "primary")
        self._create_window(self.g1, "other@gmail.com")
        self._create_window(self.g2, "primary")  # must survive

        self.g1.delete()

        remaining = list(OAuthBusyWindow.objects.all())
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].google_account_id, self.g2.pk)

    @patch("apps.scheduling.google_client.list_all_calendars")
    @patch("apps.scheduling.google_client.fetch_freebusy")
    def test_idempotency_run_twice_same_count(self, mock_freebusy, mock_calendars):
        """Running sync_one_oauth_account twice for the same account
        produces the same OAuthBusyWindow count (atomic delete+insert).
        """
        mock_calendars.return_value = [{"id": "primary"}]
        mock_freebusy.return_value = [_make_busy("primary"), _make_busy("primary", hours_from_now=4)]

        sync_one_oauth_account("google", self.g1.pk)
        count_after_first = OAuthBusyWindow.objects.filter(google_account=self.g1).count()

        sync_one_oauth_account("google", self.g1.pk)
        count_after_second = OAuthBusyWindow.objects.filter(google_account=self.g1).count()

        self.assertEqual(count_after_first, count_after_second)
        self.assertEqual(count_after_second, 2)

    @patch("apps.scheduling.google_client.list_all_calendars")
    @patch("apps.scheduling.google_client.fetch_freebusy")
    def test_two_accounts_synced_independently(self, mock_freebusy, mock_calendars):
        """Each account's sync writes only to its own rows; other account unaffected."""
        mock_calendars.return_value = [{"id": "primary"}]
        mock_freebusy.side_effect = [
            [_make_busy("primary")],       # g1's call
            [_make_busy("primary"), _make_busy("primary", hours_from_now=5)],  # g2's call
        ]

        sync_one_oauth_account("google", self.g1.pk)
        sync_one_oauth_account("google", self.g2.pk)

        self.assertEqual(OAuthBusyWindow.objects.filter(google_account=self.g1).count(), 1)
        self.assertEqual(OAuthBusyWindow.objects.filter(google_account=self.g2).count(), 2)


class SyncOneOAuthAccountMicrosoftTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="carol@x.com", password="x" * 12)
        self.ms = MicrosoftAccount.objects.create(
            user=self.user,
            microsoft_email="carol@outlook.com",
            access_token_encrypted="x",
            refresh_token_encrypted="x",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    @patch("apps.scheduling.graph_client.list_all_calendars")
    @patch("apps.scheduling.graph_client.fetch_freebusy")
    def test_sync_writes_busy_windows(self, mock_freebusy, mock_calendars):
        """Microsoft sync writes OAuthBusyWindow rows for the account."""
        mock_calendars.return_value = [{"id": "cal-abc-123"}]
        mock_freebusy.return_value = [_make_busy("cal-abc-123")]

        result = sync_one_oauth_account("microsoft", self.ms.pk)

        self.assertEqual(result["written"], 1)
        self.assertEqual(OAuthBusyWindow.objects.filter(microsoft_account=self.ms).count(), 1)

    @patch("apps.scheduling.graph_client.list_all_calendars")
    def test_broken_oauth_token_preserves_rows(self, mock_calendars):
        """Microsoft broken token: task skips, keeps existing rows."""
        from apps.scheduling.graph_client import MicrosoftOAuthError

        now = datetime.now(timezone.utc)
        OAuthBusyWindow.objects.create(
            user=self.user, provider="microsoft", microsoft_account=self.ms,
            calendar_id="primary", starts_at=now, ends_at=now + timedelta(hours=1),
        )

        mock_calendars.side_effect = MicrosoftOAuthError("token revoked")

        result = sync_one_oauth_account("microsoft", self.ms.pk)

        self.assertIn("error", result)
        self.assertEqual(OAuthBusyWindow.objects.filter(microsoft_account=self.ms).count(), 1)


# ---------------------------------------------------------------------------
# Search engine integration tests (Task 4)
# ---------------------------------------------------------------------------

class OAuthBusyWindowSearchIntegrationTests(TestCase):
    """Verify OAuthBusyWindow rows are included in SearchView busy computation."""

    def setUp(self):
        from apps.teams.models import Membership, Team

        self.user = User.objects.create_user(email="eve@x.com", password="x" * 12)
        self.team = Team.objects.create(name="T", created_by=self.user)
        Membership.objects.create(team=self.team, user=self.user)

        self.g = GoogleAccount.objects.create(
            user=self.user,
            google_email="eve@gmail.com",
            access_token_encrypted="x",
            refresh_token_encrypted="x",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    def _post_check(self, start, end):
        from django.test import Client as DjangoClient
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(user=self.user)
        return client.post("/api/search/check-time", {
            "team_id": self.team.pk,
            "member_ids": [self.user.pk],
            "start": start.isoformat(),
            "end": end.isoformat(),
        }, format="json")

    def test_oauth_busy_window_makes_slot_busy(self):
        """OAuthBusyWindow row covering a time window makes that window report busy."""
        now = datetime.now(timezone.utc)
        slot_start = now + timedelta(hours=2)
        slot_end = now + timedelta(hours=3)

        OAuthBusyWindow.objects.create(
            user=self.user,
            provider="google",
            google_account=self.g,
            calendar_id="primary",
            starts_at=slot_start,
            ends_at=slot_end,
        )

        resp = self._post_check(slot_start, slot_end)
        self.assertEqual(resp.status_code, 200)
        people = resp.data["people"]
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]["status"], "busy")

    def test_no_oauth_window_means_slot_free(self):
        """Without OAuthBusyWindow rows, the slot is free."""
        now = datetime.now(timezone.utc)
        slot_start = now + timedelta(hours=2)
        slot_end = now + timedelta(hours=3)

        resp = self._post_check(slot_start, slot_end)
        self.assertEqual(resp.status_code, 200)
        people = resp.data["people"]
        self.assertEqual(people[0]["status"], "free")
