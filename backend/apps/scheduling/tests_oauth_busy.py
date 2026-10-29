"""Tests for OAuth-driven busy intervals (OAuthBusyWindow model + sync tasks + search integration)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch, PropertyMock

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


# ---------------------------------------------------------------------------
# PR #10 must-fix regression tests
# ---------------------------------------------------------------------------

class ConcurrentSyncIdempotencyTests(TestCase):
    """PR #10 Fix 1: select_for_update() prevents double-insert on concurrent runs."""

    def setUp(self):
        self.user = User.objects.create_user(email="fix1@x.com", password="x" * 12)
        self.g = GoogleAccount.objects.create(
            user=self.user,
            google_email="fix1@gmail.com",
            access_token_encrypted="x",
            refresh_token_encrypted="x",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    @patch("apps.scheduling.google_client.list_all_calendars")
    @patch("apps.scheduling.google_client.fetch_freebusy")
    def test_concurrent_runs_do_not_double_insert(self, mock_freebusy, mock_calendars):
        """Simulate two concurrent task runs by calling sync_one_oauth_account
        twice back-to-back with identical freebusy results.  The atomic
        delete+create path must be idempotent: the final OAuthBusyWindow count
        must equal the single-run count, not 2x.

        (True thread-level concurrency requires a real multi-process DB and is
        a follow-up. This exercises the delete-then-create cycle twice, which
        is the dominant failure mode from a race: worker A deletes, worker B
        deletes-nothing, both create → double rows.)
        """
        now = datetime.now(timezone.utc)
        mock_calendars.return_value = [{"id": "primary"}]
        mock_freebusy.return_value = [
            ("primary", now, now + timedelta(hours=1)),
            ("primary", now + timedelta(hours=2), now + timedelta(hours=3)),
        ]

        sync_one_oauth_account("google", self.g.pk)
        # Run again — select_for_update serialises; result must not be 2x.
        sync_one_oauth_account("google", self.g.pk)

        self.assertEqual(
            OAuthBusyWindow.objects.filter(google_account=self.g).count(),
            2,
            "Two sequential runs of sync must produce 2 rows, not 4 (double-insert guard)",
        )


class GraphClientTimezonePreferHeaderTests(TestCase):
    """PR #10 Fix 2: fetch_freebusy must send Prefer: outlook.timezone="UTC"."""

    def setUp(self):
        self.user = User.objects.create_user(email="fix2@x.com", password="x" * 12)
        self.ms = MicrosoftAccount.objects.create(
            user=self.user,
            microsoft_email="fix2@outlook.com",
            access_token_encrypted="x",
            refresh_token_encrypted="x",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    def test_fetch_freebusy_sends_utc_timezone_preference(self):
        """Regression: MS Graph must normalize times to UTC via Prefer header.
        Without it, non-UTC calendar events are silently stored with wrong
        UTC values (e.g. a 9am Prague event stored as 9am UTC instead of 8am UTC).
        """
        from unittest.mock import Mock
        from apps.scheduling.graph_client import _Credentials, fetch_freebusy

        now = datetime.now(timezone.utc)
        start = now
        end = now + timedelta(days=7)

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"value": []}

        mock_client = Mock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        fake_creds = _Credentials(
            access_token="test-token",
            expires_at=now + timedelta(hours=1),
        )

        with patch("apps.scheduling.graph_client.get_credentials", return_value=fake_creds):
            with patch("apps.scheduling.graph_client._bearer", return_value=mock_client):
                fetch_freebusy(self.ms, ["primary"], start, end)

        self.assertTrue(
            mock_client.get.called,
            "graph_client.fetch_freebusy must call client.get",
        )
        call_kwargs = mock_client.get.call_args
        # headers is passed as a keyword argument
        headers = call_kwargs.kwargs.get("headers") or {}
        self.assertIn(
            "Prefer",
            headers,
            "fetch_freebusy must pass a Prefer header to the Graph calendarView request",
        )
        self.assertEqual(
            headers["Prefer"],
            'outlook.timezone="UTC"',
            'Prefer header must be exactly: outlook.timezone="UTC"',
        )


# ---------------------------------------------------------------------------
# _host_availability_for_reschedule must see OAuthBusyWindow
# ---------------------------------------------------------------------------

class HostAvailabilityForRescheduleOAuthBusyTests(TestCase):
    """_host_availability_for_reschedule must include OAuthBusyWindow rows.

    Previously the helper read only CalendarEvent + Unavailability, which meant
    a visitor trying to reschedule saw the host as free during OAuth-synced blocks.
    """

    def setUp(self):
        self.host = User.objects.create_user(email="reschedule_host@x.com", password="x" * 12)
        self.g = GoogleAccount.objects.create(
            user=self.host,
            google_email="reschedule_host@gmail.com",
            access_token_encrypted="x",
            refresh_token_encrypted="x",
            expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
        )

    def _call(self):
        from apps.scheduling.views import _host_availability_for_reschedule
        return _host_availability_for_reschedule(self.host)

    def test_oauth_busy_appears_in_reschedule_availability(self):
        """OAuthBusyWindow row must appear in the busy list returned by the helper."""
        now = datetime.now(timezone.utc)
        busy_start = now + timedelta(hours=2)
        busy_end = now + timedelta(hours=3)

        OAuthBusyWindow.objects.create(
            user=self.host,
            provider="google",
            google_account=self.g,
            calendar_id="primary",
            starts_at=busy_start,
            ends_at=busy_end,
        )

        result = self._call()
        busy = [
            (
                datetime.fromisoformat(item["start"].replace("Z", "+00:00")).astimezone(timezone.utc),
                datetime.fromisoformat(item["end"].replace("Z", "+00:00")).astimezone(timezone.utc),
            )
            for item in result["busy"]
        ]

        self.assertTrue(
            any(s <= busy_start and e >= busy_end for s, e in busy),
            f"Expected OAuthBusyWindow {busy_start}–{busy_end} UTC in reschedule busy list; got: {busy}",
        )

    def test_no_oauth_busy_yields_empty_busy_list_in_reschedule(self):
        """Without OAuthBusyWindow rows the reschedule helper must return an empty busy list."""
        result = self._call()
        self.assertEqual(result["busy"], [])


# ---------------------------------------------------------------------------
# C4: dump_oauth_busy --all  +  structured sync metrics logging
# ---------------------------------------------------------------------------

class DumpOAuthBusyAllFlagTests(TestCase):
    """dump_oauth_busy --all iterates every user in email order, printing headers."""

    def setUp(self):
        self.alice = User.objects.create_user(email="alice@x.com", password="x" * 12)
        self.bob = User.objects.create_user(email="bob@x.com", password="x" * 12)

    def test_all_flag_prints_both_user_headers(self):
        from io import StringIO
        from django.core.management import call_command

        out = StringIO()
        call_command("dump_oauth_busy", "--all", "--date", "2026-10-29", stdout=out)
        output = out.getvalue()

        self.assertIn("=== alice@x.com ===", output)
        self.assertIn("=== bob@x.com ===", output)

    def test_all_flag_preserves_email_order(self):
        """Users must appear in ascending email order."""
        from io import StringIO
        from django.core.management import call_command

        out = StringIO()
        call_command("dump_oauth_busy", "--all", "--date", "2026-10-29", stdout=out)
        output = out.getvalue()

        alice_pos = output.index("=== alice@x.com ===")
        bob_pos = output.index("=== bob@x.com ===")
        self.assertLess(alice_pos, bob_pos)

    def test_email_flag_still_works(self):
        """Single-user --email behavior is byte-identical to before."""
        from io import StringIO
        from django.core.management import call_command

        out = StringIO()
        call_command("dump_oauth_busy", "--email", "alice@x.com", "--date", "2026-10-29", stdout=out)
        output = out.getvalue()

        self.assertIn("alice@x.com", output)
        self.assertNotIn("=== alice@x.com ===", output)
        self.assertNotIn("bob@x.com", output)

    def test_all_and_email_are_mutually_exclusive(self):
        """Passing both --all and --email must raise CommandError (argparse mutual exclusion)."""
        from io import StringIO
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command(
                "dump_oauth_busy",
                "--all", "--email", "alice@x.com", "--date", "2026-10-29",
                stdout=StringIO(), stderr=StringIO(),
            )

    def test_neither_flag_raises_command_error(self):
        """Omitting both --all and --email must raise CommandError (argparse required group)."""
        from io import StringIO
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command(
                "dump_oauth_busy", "--date", "2026-10-29",
                stdout=StringIO(), stderr=StringIO(),
            )


class SyncMetricsLoggingTests(TestCase):
    """sync_one_oauth_account emits structured key=value log lines at completion."""

    def setUp(self):
        self.user = User.objects.create_user(email="log@x.com", password="x" * 12)
        self.g = GoogleAccount.objects.create(
            user=self.user,
            google_email="log@gmail.com",
            access_token_encrypted="x",
            refresh_token_encrypted="x",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        self.ms = MicrosoftAccount.objects.create(
            user=self.user,
            microsoft_email="log@outlook.com",
            access_token_encrypted="x",
            refresh_token_encrypted="x",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    @patch("apps.scheduling.google_client.list_all_calendars")
    @patch("apps.scheduling.google_client.fetch_freebusy")
    def test_google_success_emits_log_line(self, mock_freebusy, mock_calendars):
        """Successful Google sync emits event=oauth_busy_sync with required fields."""
        import logging as _logging

        mock_calendars.return_value = [{"id": "primary"}]
        mock_freebusy.return_value = [_make_busy("primary")]

        with self.assertLogs("apps.scheduling.tasks", level=_logging.INFO) as cm:
            sync_one_oauth_account("google", self.g.pk)

        combined = " ".join(cm.output)
        self.assertIn("event=oauth_busy_sync", combined)
        self.assertIn("provider=google", combined)
        self.assertIn(f"account_id={self.g.pk}", combined)
        self.assertIn("windows_written=1", combined)
        self.assertIn("duration_ms=", combined)

    @patch("apps.scheduling.google_client.list_all_calendars")
    def test_google_auth_error_emits_log_line(self, mock_calendars):
        """Google auth error emits event=oauth_busy_sync with error field."""
        import logging as _logging
        from apps.scheduling.google_client import GoogleOAuthError

        mock_calendars.side_effect = GoogleOAuthError("token revoked")

        with self.assertLogs("apps.scheduling.tasks", level=_logging.WARNING) as cm:
            sync_one_oauth_account("google", self.g.pk)

        combined = " ".join(cm.output)
        self.assertIn("event=oauth_busy_sync", combined)
        self.assertIn("provider=google", combined)
        self.assertIn("error=", combined)

    @patch("apps.scheduling.graph_client.list_all_calendars")
    @patch("apps.scheduling.graph_client.fetch_freebusy")
    def test_microsoft_success_emits_log_line(self, mock_freebusy, mock_calendars):
        """Successful Microsoft sync emits event=oauth_busy_sync with required fields."""
        import logging as _logging

        mock_calendars.return_value = [{"id": "cal-1"}]
        mock_freebusy.return_value = [_make_busy("cal-1")]

        with self.assertLogs("apps.scheduling.tasks", level=_logging.INFO) as cm:
            sync_one_oauth_account("microsoft", self.ms.pk)

        combined = " ".join(cm.output)
        self.assertIn("event=oauth_busy_sync", combined)
        self.assertIn("provider=microsoft", combined)
        self.assertIn(f"account_id={self.ms.pk}", combined)
        self.assertIn("windows_written=1", combined)
        self.assertIn("duration_ms=", combined)

    @patch("apps.scheduling.graph_client.list_all_calendars")
    def test_microsoft_auth_error_emits_log_line(self, mock_calendars):
        """Microsoft auth error emits event=oauth_busy_sync with error field."""
        import logging as _logging
        from apps.scheduling.graph_client import MicrosoftOAuthError

        mock_calendars.side_effect = MicrosoftOAuthError("token revoked")

        with self.assertLogs("apps.scheduling.tasks", level=_logging.WARNING) as cm:
            sync_one_oauth_account("microsoft", self.ms.pk)

        combined = " ".join(cm.output)
        self.assertIn("event=oauth_busy_sync", combined)
        self.assertIn("provider=microsoft", combined)
        self.assertIn("error=", combined)


# ---------------------------------------------------------------------------
# _assert_tz_aware guard — OAuthBusyWindow row path
# ---------------------------------------------------------------------------

class AssertTzAwareOAuthBusyWindowTests(TestCase):
    """busy_intervals_for_users raises ValueError when OAuthBusyWindow.starts_at
    is tz-naive.  The DB schema requires tz-aware values, but the guard must
    catch any source (test fixture, migration, direct ORM write) that strips
    timezone info before the intervals reach the merge step.
    """

    def setUp(self):
        self.user = User.objects.create_user(email="tz_guard@x.com", password="x" * 12)

    def test_naive_starts_at_raises_value_error(self):
        """_assert_tz_aware must raise ValueError containing 'OAuthBusyWindow.starts_at'
        when the DB returns a tz-naive datetime for that column.
        """
        from apps.search.queries import busy_intervals_for_users

        now = datetime.now(timezone.utc)
        window_start = now
        window_end = now + timedelta(hours=4)

        # Fake a DB row where starts_at has no tzinfo (simulates a source stripping tz).
        naive_start = datetime(2026, 10, 29, 10, 0, 0)  # no tzinfo
        aware_end = now + timedelta(hours=1)

        fake_rows = [{"user_id": self.user.pk, "starts_at": naive_start, "ends_at": aware_end}]

        # Patch OAuthBusyWindow.objects.filter(...).values(...) to return our fake rows.
        with patch(
            "apps.search.queries.OAuthBusyWindow.objects"
        ) as mock_manager:
            # CalendarEvent and Unavailability must return empty so we reach OAuthBusyWindow.
            with patch("apps.search.queries.CalendarEvent.objects") as mock_cal, \
                 patch("apps.search.queries.Unavailability.objects") as mock_unavail:
                mock_cal.filter.return_value.exclude.return_value.values.return_value = []
                mock_unavail.filter.return_value.values.return_value = []
                mock_manager.filter.return_value.values.return_value = fake_rows

                with self.assertRaises(ValueError) as ctx:
                    busy_intervals_for_users([self.user.pk], window_start, window_end)

        self.assertIn("OAuthBusyWindow.starts_at", str(ctx.exception))
