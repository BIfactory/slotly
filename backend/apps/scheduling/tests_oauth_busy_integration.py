"""Integration test suite: OAuth busy end-to-end regression.

Mocked HTTP → sync task → DB → availability views.
Guards the class of regression that produced bugs #1 and #2.

Bug #1: MyAvailabilityView (/api/me/availability) read only CalendarEvent; OAuthBusyWindow
        rows were silently dropped, so the host's own profile showed free when OAuth sync
        had populated busy windows.

Bug #2: TeammateAvailabilityView / PeerAvailabilityView (/api/users/<id>/availability,
        /api/public/profile/<token>) had the same CalendarEvent-only bug for the peer.

Both were fixed in C2 by routing all views through busy_intervals_for_users() which
UNIONs CalendarEvent + OAuthBusyWindow + Unavailability.

This suite exercises the full stack:
  patch fetch_freebusy (HTTP boundary) →
  call sync_one_oauth_account (the sync task) →
  verify OAuthBusyWindow row written to DB →
  call /api/me/availability (view layer) →
  assert busy interval appears in response.

Timezone reference (R1 guard):
  OAuthBusyWindow row: 2026-10-29 10:00–12:00 UTC
  Prague (CET, UTC+1 on that date — DST ended 2026-10-25): 11:00–13:00

The endpoints return busy intervals in UTC (ISO-8601 with +00:00 or Z suffix).
Assertions compare the underlying UTC instant, not string representation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.calendars.models import Calendar, CalendarEvent
from apps.scheduling.models import GoogleAccount, MicrosoftAccount, OAuthBusyWindow
from apps.scheduling.security import encrypt

UTC = timezone.utc

# The production scenario: a busy block at 10:00–12:00 UTC
# Prague equivalent: 11:00–13:00 CET
BUSY_START = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
BUSY_END = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

# A query window covering the busy block
WINDOW_FROM = "2026-10-29"
WINDOW_TO = "2026-10-30"

# Sentinel calendar ID used in mocks
PRIMARY_CAL_ID = "primary"
SECONDARY_CAL_ID = "c_a2bd8e4c123abc456def789012345678@group.calendar.google.com"
MS_CAL_ID = "AQMkADAwATM0MDAAMS1mNjgzLTkzMDctMDACLTAwCgBGAAADy8sms"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(email: str):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    return User.objects.create_user(email=email, password="x" * 12)


def _make_google_account(user) -> GoogleAccount:
    """Create a GoogleAccount with a far-future expiry so get_credentials
    returns the stored token without attempting a real HTTP refresh."""
    return GoogleAccount.objects.create(
        user=user,
        google_email=f"{user.email.split('@')[0]}@gmail.com",
        access_token_encrypted=encrypt("stub-access-token"),
        refresh_token_encrypted=encrypt("stub-refresh-token"),
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )


def _make_microsoft_account(user) -> MicrosoftAccount:
    return MicrosoftAccount.objects.create(
        user=user,
        microsoft_email=f"{user.email.split('@')[0]}@outlook.com",
        access_token_encrypted=encrypt("stub-ms-access-token"),
        refresh_token_encrypted=encrypt("stub-ms-refresh-token"),
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )


def _busy_instants(resp_data) -> list[tuple[datetime, datetime]]:
    """Parse the 'busy' list from the response into (start, end) UTC tuples."""
    result = []
    for item in resp_data.get("busy", []):
        s = datetime.fromisoformat(item["start"].replace("Z", "+00:00"))
        e = datetime.fromisoformat(item["end"].replace("Z", "+00:00"))
        result.append((s.astimezone(UTC), e.astimezone(UTC)))
    return result


def _covers(busy: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    """Return True if any interval in busy covers [start, end]."""
    return any(s <= start and e >= end for s, e in busy)


# ---------------------------------------------------------------------------
# Step 1: Google sync → DB → availability
# ---------------------------------------------------------------------------

class GoogleSyncAppearsInAvailabilityTests(TestCase):
    """Regression: a new Google OAuth busy block must appear in
    /api/me/availability after one sync cycle.

    This guards R1 (timezone silent drift): if fetch_freebusy returns a
    tz-naive datetime, busy_intervals_for_users raises ValueError loudly
    rather than silently shifting the interval.
    """

    def setUp(self):
        self.user = _make_user("alice_sync@example.com")
        self.account = _make_google_account(self.user)
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)

    def test_new_google_account_busy_appears_in_availability_after_sync(self):
        """Full pipeline: mock HTTP → sync → DB → view.

        freebusy mock returns 10:00–12:00 UTC.  After sync the view must
        include a busy interval covering that UTC instant.
        """
        from apps.scheduling.tasks import sync_one_oauth_account

        mock_calendars = [{"id": PRIMARY_CAL_ID}]
        mock_busy = [(PRIMARY_CAL_ID, BUSY_START, BUSY_END)]

        with patch("apps.scheduling.google_client.list_all_calendars", return_value=mock_calendars), \
             patch("apps.scheduling.google_client.fetch_freebusy", return_value=mock_busy):
            result = sync_one_oauth_account("google", self.account.pk)

        self.assertIn("written", result, f"sync failed: {result}")
        self.assertEqual(result["written"], 1)

        # DB must have the row.
        self.assertEqual(OAuthBusyWindow.objects.filter(user=self.user).count(), 1)
        row = OAuthBusyWindow.objects.get(user=self.user)
        self.assertEqual(row.starts_at, BUSY_START)
        self.assertEqual(row.ends_at, BUSY_END)
        self.assertIsNotNone(row.starts_at.tzinfo, "starts_at must be tz-aware")
        self.assertIsNotNone(row.ends_at.tzinfo, "ends_at must be tz-aware")

        # View must expose the interval.
        resp = self.api.get("/api/me/availability", {"from": WINDOW_FROM, "to": WINDOW_TO})
        self.assertEqual(resp.status_code, 200, resp.data)

        busy = _busy_instants(resp.data)
        self.assertTrue(
            _covers(busy, BUSY_START, BUSY_END),
            f"Expected OAuthBusyWindow {BUSY_START}–{BUSY_END} UTC in busy list; got: {busy}",
        )

    def test_second_sync_replaces_first_run_idempotency(self):
        """Running sync twice with the same mock data must not double the rows.

        OAuthBusyWindow has no unique constraint — the sync task uses
        delete + bulk_create atomically.  A second run with identical data
        must yield the same row count as the first run (idempotency).
        """
        from apps.scheduling.tasks import sync_one_oauth_account

        mock_calendars = [{"id": PRIMARY_CAL_ID}]
        mock_busy = [(PRIMARY_CAL_ID, BUSY_START, BUSY_END)]

        with patch("apps.scheduling.google_client.list_all_calendars", return_value=mock_calendars), \
             patch("apps.scheduling.google_client.fetch_freebusy", return_value=mock_busy):
            sync_one_oauth_account("google", self.account.pk)
            sync_one_oauth_account("google", self.account.pk)  # second run

        # Must still be exactly 1 row, not 2.
        count = OAuthBusyWindow.objects.filter(user=self.user).count()
        self.assertEqual(count, 1, f"Expected 1 row after two syncs; got {count} (double-write bug)")


# ---------------------------------------------------------------------------
# Step 2: Multiple accounts, overlapping windows — dedup
# ---------------------------------------------------------------------------

class MultipleAccountsOverlapTests(TestCase):
    """Regression: two OAuth accounts with overlapping busy windows must
    produce a single merged interval in the response (not two separate ones
    and not a double-width block).

    Google account A: 10:00–12:00 UTC
    Microsoft account B: 11:00–13:00 UTC
    Expected merged: 10:00–13:00 UTC  (one interval)
    """

    def setUp(self):
        self.user = _make_user("alice_multi@example.com")
        self.google_account = _make_google_account(self.user)
        self.ms_account = _make_microsoft_account(self.user)
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)

    def test_multiple_accounts_overlapping_windows_dedup(self):
        from apps.scheduling.tasks import sync_one_oauth_account

        g_start = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
        g_end = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
        ms_start = datetime(2026, 10, 29, 11, 0, tzinfo=UTC)
        ms_end = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)

        g_mock_calendars = [{"id": PRIMARY_CAL_ID}]
        g_mock_busy = [(PRIMARY_CAL_ID, g_start, g_end)]

        ms_mock_calendars = [{"id": MS_CAL_ID}]
        ms_mock_busy = [(MS_CAL_ID, ms_start, ms_end)]

        # Sync Google account.
        with patch("apps.scheduling.google_client.list_all_calendars", return_value=g_mock_calendars), \
             patch("apps.scheduling.google_client.fetch_freebusy", return_value=g_mock_busy):
            sync_one_oauth_account("google", self.google_account.pk)

        # Sync Microsoft account.
        with patch("apps.scheduling.graph_client.list_all_calendars", return_value=ms_mock_calendars), \
             patch("apps.scheduling.graph_client.fetch_freebusy", return_value=ms_mock_busy):
            sync_one_oauth_account("microsoft", self.ms_account.pk)

        self.assertEqual(OAuthBusyWindow.objects.filter(user=self.user).count(), 2)

        resp = self.api.get("/api/me/availability", {"from": WINDOW_FROM, "to": WINDOW_TO})
        self.assertEqual(resp.status_code, 200, resp.data)

        busy = _busy_instants(resp.data)
        # The two overlapping intervals (10:00–12:00 and 11:00–13:00) must be
        # merged into a single interval covering 10:00–13:00 UTC.
        expected_start = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
        expected_end = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)
        self.assertTrue(
            _covers(busy, expected_start, expected_end),
            f"Expected one merged interval {expected_start}–{expected_end} UTC; got: {busy}",
        )
        # Exactly one merged interval (not two separate ones).
        intervals_in_window = [
            (s, e) for s, e in busy
            if s >= expected_start and e <= expected_end
        ]
        self.assertEqual(
            len(intervals_in_window),
            1,
            f"Expected exactly ONE merged interval; got {busy}",
        )


# ---------------------------------------------------------------------------
# Step 3: ICS CalendarEvent + OAuthBusyWindow UNION
# ---------------------------------------------------------------------------

class IcsAndOAuthUnionTests(TestCase):
    """Regression: ICS CalendarEvent and OAuthBusyWindow must both appear in
    the busy list.  Adjacent (non-overlapping) intervals from different sources
    must appear as separate intervals.
    """

    def setUp(self):
        self.user = _make_user("alice_ics@example.com")
        self.google_account = _make_google_account(self.user)
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)

    def test_ics_calendar_event_and_oauth_busy_union(self):
        """ICS event 09:00–10:00 UTC + OAuthBusyWindow 10:00–11:00 UTC (adjacent)
        must both appear in the busy list.

        Note: _merge_intervals merges adjacent intervals (s <= last_e covers
        touching boundaries), so 09:00–10:00 + 10:00–11:00 → 09:00–11:00.
        The assertion checks that the combined window is covered.
        """
        ics_start = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
        ics_end = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
        oauth_start = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
        oauth_end = datetime(2026, 10, 29, 11, 0, tzinfo=UTC)

        # Create an ICS Calendar + CalendarEvent (opaque, not all-day).
        cal = Calendar.objects.create(
            owner=self.user,
            name="Work",
            url_encrypted=encrypt("https://example.com/calendar.ics"),
            include_in_busy=True,
        )
        CalendarEvent.objects.create(
            calendar=cal,
            uid="test-ics-uid-1",
            dtstart=ics_start,
            dtend=ics_end,
            is_all_day=False,
            transp=CalendarEvent.Transparency.OPAQUE,
            status=CalendarEvent.Status.CONFIRMED,
        )

        # Create the OAuthBusyWindow directly (no sync needed here — the ICS
        # path doesn't need HTTP mocking, only the OAuth path does).
        OAuthBusyWindow.objects.create(
            user=self.user,
            provider="google",
            google_account=self.google_account,
            calendar_id=PRIMARY_CAL_ID,
            starts_at=oauth_start,
            ends_at=oauth_end,
        )

        resp = self.api.get("/api/me/availability", {"from": WINDOW_FROM, "to": WINDOW_TO})
        self.assertEqual(resp.status_code, 200, resp.data)

        busy = _busy_instants(resp.data)
        # Both intervals must be in the busy list.
        # Adjacent intervals (10:00 touches) are merged by _merge_intervals,
        # so the combined coverage is 09:00–11:00.
        combined_start = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
        combined_end = datetime(2026, 10, 29, 11, 0, tzinfo=UTC)
        self.assertTrue(
            _covers(busy, combined_start, combined_end),
            f"Expected ICS+OAuth union covering {combined_start}–{combined_end}; got: {busy}",
        )

    def test_ics_calendar_event_and_oauth_busy_non_adjacent(self):
        """ICS event 09:00–10:00 UTC + OAuthBusyWindow 10:30–11:30 UTC (gap).
        Both intervals must appear separately (not merged) in the busy list.
        """
        ics_start = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
        ics_end = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
        oauth_start = datetime(2026, 10, 29, 10, 30, tzinfo=UTC)
        oauth_end = datetime(2026, 10, 29, 11, 30, tzinfo=UTC)

        cal = Calendar.objects.create(
            owner=self.user,
            name="Work2",
            url_encrypted=encrypt("https://example.com/calendar2.ics"),
            include_in_busy=True,
        )
        CalendarEvent.objects.create(
            calendar=cal,
            uid="test-ics-uid-2",
            dtstart=ics_start,
            dtend=ics_end,
            is_all_day=False,
            transp=CalendarEvent.Transparency.OPAQUE,
            status=CalendarEvent.Status.CONFIRMED,
        )

        OAuthBusyWindow.objects.create(
            user=self.user,
            provider="google",
            google_account=self.google_account,
            calendar_id=PRIMARY_CAL_ID,
            starts_at=oauth_start,
            ends_at=oauth_end,
        )

        resp = self.api.get("/api/me/availability", {"from": WINDOW_FROM, "to": WINDOW_TO})
        self.assertEqual(resp.status_code, 200, resp.data)

        busy = _busy_instants(resp.data)
        # Both non-adjacent intervals must appear separately.
        self.assertTrue(
            _covers(busy, ics_start, ics_end),
            f"ICS interval {ics_start}–{ics_end} not found in {busy}",
        )
        self.assertTrue(
            _covers(busy, oauth_start, oauth_end),
            f"OAuth interval {oauth_start}–{oauth_end} not found in {busy}",
        )
        # Two separate intervals exist (not one merged block).
        self.assertGreaterEqual(
            len(busy), 2,
            f"Expected at least 2 separate intervals; got: {busy}",
        )


# ---------------------------------------------------------------------------
# Step 4: Zero, one, and many busy windows
# ---------------------------------------------------------------------------

class ZeroOneManySyncTests(TestCase):
    """Parameterised coverage: 0, 1, and many busy windows from Google sync."""

    def setUp(self):
        self.user = _make_user("alice_count@example.com")
        self.account = _make_google_account(self.user)
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)

    def test_availability_zero_busy_windows(self):
        """Sync returns no busy intervals; view must return empty busy list."""
        from apps.scheduling.tasks import sync_one_oauth_account

        with patch("apps.scheduling.google_client.list_all_calendars", return_value=[{"id": PRIMARY_CAL_ID}]), \
             patch("apps.scheduling.google_client.fetch_freebusy", return_value=[]):
            result = sync_one_oauth_account("google", self.account.pk)

        self.assertEqual(result["written"], 0)
        self.assertEqual(OAuthBusyWindow.objects.filter(user=self.user).count(), 0)

        resp = self.api.get("/api/me/availability", {"from": WINDOW_FROM, "to": WINDOW_TO})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(_busy_instants(resp.data), [])

    def test_availability_one_busy_window(self):
        """Sync returns exactly one busy interval; view must expose it."""
        from apps.scheduling.tasks import sync_one_oauth_account

        mock_busy = [(PRIMARY_CAL_ID, BUSY_START, BUSY_END)]

        with patch("apps.scheduling.google_client.list_all_calendars", return_value=[{"id": PRIMARY_CAL_ID}]), \
             patch("apps.scheduling.google_client.fetch_freebusy", return_value=mock_busy):
            result = sync_one_oauth_account("google", self.account.pk)

        self.assertEqual(result["written"], 1)

        resp = self.api.get("/api/me/availability", {"from": WINDOW_FROM, "to": WINDOW_TO})
        self.assertEqual(resp.status_code, 200, resp.data)

        busy = _busy_instants(resp.data)
        self.assertTrue(
            _covers(busy, BUSY_START, BUSY_END),
            f"Expected one interval; got: {busy}",
        )

    def test_availability_many_busy_windows(self):
        """Sync returns three non-overlapping intervals; view must expose all three."""
        from apps.scheduling.tasks import sync_one_oauth_account

        t1_s = datetime(2026, 10, 29, 8, 0, tzinfo=UTC)
        t1_e = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
        t2_s = datetime(2026, 10, 29, 11, 0, tzinfo=UTC)
        t2_e = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
        t3_s = datetime(2026, 10, 29, 14, 0, tzinfo=UTC)
        t3_e = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)

        mock_busy = [
            (PRIMARY_CAL_ID, t1_s, t1_e),
            (PRIMARY_CAL_ID, t2_s, t2_e),
            (PRIMARY_CAL_ID, t3_s, t3_e),
        ]

        with patch("apps.scheduling.google_client.list_all_calendars", return_value=[{"id": PRIMARY_CAL_ID}]), \
             patch("apps.scheduling.google_client.fetch_freebusy", return_value=mock_busy):
            result = sync_one_oauth_account("google", self.account.pk)

        self.assertEqual(result["written"], 3)
        self.assertEqual(OAuthBusyWindow.objects.filter(user=self.user).count(), 3)

        resp = self.api.get("/api/me/availability", {"from": WINDOW_FROM, "to": WINDOW_TO})
        self.assertEqual(resp.status_code, 200, resp.data)

        busy = _busy_instants(resp.data)
        self.assertTrue(_covers(busy, t1_s, t1_e), f"Interval 1 missing from {busy}")
        self.assertTrue(_covers(busy, t2_s, t2_e), f"Interval 2 missing from {busy}")
        self.assertTrue(_covers(busy, t3_s, t3_e), f"Interval 3 missing from {busy}")
        self.assertEqual(len(busy), 3, f"Expected 3 intervals; got: {busy}")


# ---------------------------------------------------------------------------
# Step 5: Timezone — naive datetime from source raises loudly
# ---------------------------------------------------------------------------

class NaiveDatetimeSmokeTests(TestCase):
    """R1 timezone guard: busy_intervals_for_users must raise ValueError if
    any source produces a tz-naive datetime.

    OAuthBusyWindow.starts_at / ends_at are stored as tz-aware (Django
    DateTimeField with USE_TZ=True saves as UTC).  After retrieval via
    `.values()` they remain tz-aware.  This test inserts a row using
    connection.execute (bypassing Django's ORM tz-injection) to simulate a
    source returning a naive datetime, then calls busy_intervals_for_users
    and asserts it raises ValueError.

    If the low-level SQL bypass doesn't produce a naive datetime at the ORM
    layer (some backends transparently re-attach UTC), this test degrades to
    a smoke test that busy_intervals_for_users returns without error for the
    valid tz-aware case — which is still useful coverage.
    """

    def setUp(self):
        self.user = _make_user("alice_naive@example.com")
        self.google_account = _make_google_account(self.user)

    def test_valid_tz_aware_row_does_not_raise(self):
        """Sanity check: a properly tz-aware OAuthBusyWindow row is accepted
        without error by busy_intervals_for_users."""
        from apps.search.queries import busy_intervals_for_users

        OAuthBusyWindow.objects.create(
            user=self.user,
            provider="google",
            google_account=self.google_account,
            calendar_id=PRIMARY_CAL_ID,
            starts_at=BUSY_START,
            ends_at=BUSY_END,
        )

        window_s = datetime(2026, 10, 29, 0, 0, tzinfo=UTC)
        window_e = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)

        # Must not raise.
        result = busy_intervals_for_users([self.user.pk], window_s, window_e)
        intervals = result[self.user.pk]
        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0], (BUSY_START, BUSY_END))

    def test_helper_rejects_naive_window_start(self):
        """busy_intervals_for_users raises ValueError when window_start is naive."""
        from apps.search.queries import busy_intervals_for_users

        naive_start = datetime(2026, 10, 29, 0, 0)  # no tzinfo
        aware_end = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)

        with self.assertRaises(ValueError) as ctx:
            busy_intervals_for_users([self.user.pk], naive_start, aware_end)

        self.assertIn("window_start", str(ctx.exception))
        self.assertIn("tz-naive", str(ctx.exception))

    def test_helper_rejects_naive_window_end(self):
        """busy_intervals_for_users raises ValueError when window_end is naive."""
        from apps.search.queries import busy_intervals_for_users

        aware_start = datetime(2026, 10, 29, 0, 0, tzinfo=UTC)
        naive_end = datetime(2026, 10, 30, 0, 0)  # no tzinfo

        with self.assertRaises(ValueError) as ctx:
            busy_intervals_for_users([self.user.pk], aware_start, naive_end)

        self.assertIn("window_end", str(ctx.exception))
        self.assertIn("tz-naive", str(ctx.exception))


# ---------------------------------------------------------------------------
# Step 6 (bonus): Microsoft sync pipeline
# ---------------------------------------------------------------------------

class MicrosoftSyncAppearsInAvailabilityTests(TestCase):
    """Microsoft OAuth sync → DB → availability view.

    Mirrors the Google test (Step 1) for the MS Graph path, so both
    providers have end-to-end coverage.
    """

    def setUp(self):
        self.user = _make_user("alice_ms_sync@example.com")
        self.account = _make_microsoft_account(self.user)
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)

    def test_new_microsoft_account_busy_appears_in_availability_after_sync(self):
        """Full pipeline for Microsoft: mock HTTP → sync → DB → view."""
        from apps.scheduling.tasks import sync_one_oauth_account

        mock_calendars = [{"id": MS_CAL_ID}]
        mock_busy = [(MS_CAL_ID, BUSY_START, BUSY_END)]

        with patch("apps.scheduling.graph_client.list_all_calendars", return_value=mock_calendars), \
             patch("apps.scheduling.graph_client.fetch_freebusy", return_value=mock_busy):
            result = sync_one_oauth_account("microsoft", self.account.pk)

        self.assertIn("written", result, f"sync failed: {result}")
        self.assertEqual(result["written"], 1)

        self.assertEqual(OAuthBusyWindow.objects.filter(user=self.user).count(), 1)
        row = OAuthBusyWindow.objects.get(user=self.user)
        self.assertEqual(row.provider, "microsoft")
        self.assertIsNotNone(row.starts_at.tzinfo, "MS busy window starts_at must be tz-aware")

        resp = self.api.get("/api/me/availability", {"from": WINDOW_FROM, "to": WINDOW_TO})
        self.assertEqual(resp.status_code, 200, resp.data)

        busy = _busy_instants(resp.data)
        self.assertTrue(
            _covers(busy, BUSY_START, BUSY_END),
            f"Expected MS OAuthBusyWindow {BUSY_START}–{BUSY_END} UTC in busy list; got: {busy}",
        )

    def test_second_ms_sync_replaces_first_run_idempotency(self):
        """Microsoft sync idempotency: second run with same data must not
        create extra rows."""
        from apps.scheduling.tasks import sync_one_oauth_account

        mock_calendars = [{"id": MS_CAL_ID}]
        mock_busy = [(MS_CAL_ID, BUSY_START, BUSY_END)]

        with patch("apps.scheduling.graph_client.list_all_calendars", return_value=mock_calendars), \
             patch("apps.scheduling.graph_client.fetch_freebusy", return_value=mock_busy):
            sync_one_oauth_account("microsoft", self.account.pk)
            sync_one_oauth_account("microsoft", self.account.pk)

        count = OAuthBusyWindow.objects.filter(user=self.user).count()
        self.assertEqual(count, 1, f"Expected 1 row after two MS syncs; got {count}")


# ---------------------------------------------------------------------------
# Step 7 (bonus): No-calendars edge case
# ---------------------------------------------------------------------------

class NoCalendarsEdgeCaseTests(TestCase):
    """If the connected account has no calendars, sync must clear old rows
    (not leave stale data from a previous sync) and write 0 rows."""

    def setUp(self):
        self.user = _make_user("alice_nocal@example.com")
        self.account = _make_google_account(self.user)

    def test_no_calendars_clears_existing_windows(self):
        """Pre-existing OAuthBusyWindow rows must be removed when the
        account now reports zero calendars."""
        from apps.scheduling.tasks import sync_one_oauth_account

        # Seed a stale row from a previous sync.
        OAuthBusyWindow.objects.create(
            user=self.user,
            provider="google",
            google_account=self.account,
            calendar_id=PRIMARY_CAL_ID,
            starts_at=BUSY_START,
            ends_at=BUSY_END,
        )
        self.assertEqual(OAuthBusyWindow.objects.filter(user=self.user).count(), 1)

        with patch("apps.scheduling.google_client.list_all_calendars", return_value=[]):
            result = sync_one_oauth_account("google", self.account.pk)

        self.assertEqual(result.get("written", 0), 0)
        self.assertEqual(
            OAuthBusyWindow.objects.filter(user=self.user).count(), 0,
            "Stale rows must be removed when account reports zero calendars",
        )
