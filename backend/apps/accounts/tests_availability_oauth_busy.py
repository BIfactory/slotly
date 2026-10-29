"""TDD tests for Task C2 — OAuth busy intervals visible in profile & peer endpoints.

These tests deliberately fail against the pre-patch code (RED phase) because
MyAvailabilityView and TeammateAvailabilityView read only CalendarEvent for busy
data and never UNION OAuthBusyWindow.

After patching the views to call busy_intervals_for_users they should pass (GREEN).

Timezone fixture
-----------------
OAuthBusyWindow row: 2026-10-29 10:00–12:00 UTC
= Prague (CET, UTC+1 on that date — DST ended 2026-10-25): 11:00–13:00

The endpoint returns busy intervals in their stored UTC representation (ISO-8601
with +00:00 suffix). Tests assert the *time instant* (compare as UTC) so they
are independent of string serialisation format.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.scheduling.models import GoogleAccount, OAuthBusyWindow
from apps.teams.models import Membership, Team

User = get_user_model()

UTC = timezone.utc

# The production scenario: secondary-calendar block at 10:00-12:00 UTC
BUSY_START = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
BUSY_END = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

# Window that covers the busy block
WINDOW_FROM = "2026-10-29"
WINDOW_TO = "2026-10-30"


def _make_user(email: str) -> User:
    return User.objects.create_user(email=email, password="x" * 12)


def _make_google_account(user: User) -> GoogleAccount:
    return GoogleAccount.objects.create(
        user=user,
        google_email=f"{user.email.split('@')[0]}@gmail.com",
        access_token_encrypted="x",
        refresh_token_encrypted="x",
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    )


def _make_oauth_busy(user: User, g: GoogleAccount, *, calendar_id: str = "primary") -> OAuthBusyWindow:
    return OAuthBusyWindow.objects.create(
        user=user,
        provider="google",
        google_account=g,
        calendar_id=calendar_id,
        starts_at=BUSY_START,
        ends_at=BUSY_END,
    )


def _busy_instants(resp_data) -> list[tuple[datetime, datetime]]:
    """Parse the 'busy' list from the response into (start, end) UTC tuples."""
    result = []
    for item in resp_data.get("busy", []):
        s = datetime.fromisoformat(item["start"].replace("Z", "+00:00"))
        e = datetime.fromisoformat(item["end"].replace("Z", "+00:00"))
        result.append((s.astimezone(UTC), e.astimezone(UTC)))
    return result


class MyAvailabilityOAuthBusyTests(TestCase):
    """GET /api/me/availability must include OAuthBusyWindow rows."""

    def setUp(self):
        self.user = _make_user("alice@example.com")
        self.g = _make_google_account(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_oauth_busy_appears_in_profile_availability_in_prague_tz(self):
        """OAuthBusyWindow 10:00-12:00 UTC must appear in /api/me/availability busy list.

        Equivalent Prague time: 11:00-13:00 CET (DST ended 2026-10-25).
        The assertion checks the time instant (UTC), not the string.
        """
        _make_oauth_busy(self.user, self.g, calendar_id="primary")

        resp = self.client.get(
            "/api/me/availability",
            {"from": WINDOW_FROM, "to": WINDOW_TO},
        )
        self.assertEqual(resp.status_code, 200, resp.data)

        busy = _busy_instants(resp.data)
        # The busy block must cover the expected UTC instant.
        self.assertTrue(
            any(s <= BUSY_START and e >= BUSY_END for s, e in busy),
            f"Expected OAuthBusyWindow {BUSY_START}–{BUSY_END} UTC in busy list; got: {busy}",
        )

    def test_oauth_busy_secondary_calendar_appears_in_profile_availability(self):
        """OAuthBusyWindow from a secondary calendar (c_<hash> id) must also appear.

        This validates that no calendar_id filter silently drops secondary-cal rows.
        """
        secondary_cal_id = "c_a2bd8e4c123abc456def789012345678@group.calendar.google.com"
        _make_oauth_busy(self.user, self.g, calendar_id=secondary_cal_id)

        resp = self.client.get(
            "/api/me/availability",
            {"from": WINDOW_FROM, "to": WINDOW_TO},
        )
        self.assertEqual(resp.status_code, 200, resp.data)

        busy = _busy_instants(resp.data)
        self.assertTrue(
            any(s <= BUSY_START and e >= BUSY_END for s, e in busy),
            f"Expected secondary-calendar OAuthBusyWindow {BUSY_START}–{BUSY_END} UTC; got: {busy}",
        )

    def test_no_oauth_busy_yields_empty_busy_list(self):
        """Without OAuthBusyWindow rows the endpoint must return an empty busy list."""
        resp = self.client.get(
            "/api/me/availability",
            {"from": WINDOW_FROM, "to": WINDOW_TO},
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        busy = _busy_instants(resp.data)
        self.assertEqual(busy, [])


class TeammateAvailabilityOAuthBusyTests(TestCase):
    """GET /api/users/<id>/availability must include the peer's OAuthBusyWindow rows.

    Note: the brief targets MyAvailabilityView + PeerAvailabilityView. This view
    (TeammateAvailabilityView) has the same CalendarEvent-only bug and is patched
    in the same pass. These tests serve as the RED proof and GREEN validation.
    """

    def setUp(self):
        self.alice = _make_user("alice2@example.com")
        self.bob = _make_user("bob2@example.com")
        self.team = Team.objects.create(name="T2", created_by=self.alice)
        Membership.objects.create(team=self.team, user=self.alice)
        Membership.objects.create(team=self.team, user=self.bob)

        self.g_bob = _make_google_account(self.bob)

        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)

    def test_oauth_busy_appears_in_peer_availability_in_prague_tz(self):
        """Bob's OAuthBusyWindow must appear when Alice fetches /api/users/<bob>/availability.

        This is bug #2 from the production dump: /people/<id> shows Bob as free
        even though sync_oauth_busy populated his OAuthBusyWindow correctly.
        """
        _make_oauth_busy(self.bob, self.g_bob, calendar_id="primary")

        resp = self.client.get(
            f"/api/users/{self.bob.pk}/availability",
            {"from": WINDOW_FROM, "to": WINDOW_TO},
        )
        self.assertEqual(resp.status_code, 200, resp.data)

        busy = _busy_instants(resp.data)
        self.assertTrue(
            any(s <= BUSY_START and e >= BUSY_END for s, e in busy),
            f"Expected Bob's OAuthBusyWindow {BUSY_START}–{BUSY_END} UTC; got: {busy}",
        )

    def test_oauth_busy_secondary_calendar_appears_in_peer_availability(self):
        """Secondary-calendar OAuthBusyWindow rows must not be silently dropped."""
        secondary_cal_id = "c_a2bd8e4c123abc456def789012345678@group.calendar.google.com"
        _make_oauth_busy(self.bob, self.g_bob, calendar_id=secondary_cal_id)

        resp = self.client.get(
            f"/api/users/{self.bob.pk}/availability",
            {"from": WINDOW_FROM, "to": WINDOW_TO},
        )
        self.assertEqual(resp.status_code, 200, resp.data)

        busy = _busy_instants(resp.data)
        self.assertTrue(
            any(s <= BUSY_START and e >= BUSY_END for s, e in busy),
            f"Expected secondary-calendar OAuthBusyWindow; got: {busy}",
        )


class PublicProfileOAuthBusyTests(TestCase):
    """GET /api/public/profile/<token> must include OAuthBusyWindow rows.

    This is the most user-visible manifestation of the CalendarEvent-only bug:
    a visitor on /u/<token> sees the host as free even when sync_oauth_busy has
    correctly populated OAuthBusyWindow.
    """

    def setUp(self):
        self.host = _make_user("host_public@example.com")
        # Enable public profile so PublicProfileView returns 200.
        self.host.share_enabled = True
        self.host.save(update_fields=["share_enabled"])

        self.g = _make_google_account(self.host)

        # Anonymous client — PublicProfileView has no auth requirement.
        self.client = APIClient()

    def test_oauth_busy_appears_in_public_profile(self):
        """OAuthBusyWindow 10:00-12:00 UTC must appear in /api/public/profile/<token> busy list.

        Before the fix, PublicProfileView read only CalendarEvent + Unavailability,
        so OAuth-synced busy windows were invisible to anonymous visitors.
        """
        _make_oauth_busy(self.host, self.g, calendar_id="primary")

        resp = self.client.get(
            f"/api/public/profile/{self.host.share_token}",
            {"from": WINDOW_FROM, "to": WINDOW_TO},
        )
        self.assertEqual(resp.status_code, 200, resp.data)

        busy = _busy_instants(resp.data)
        self.assertTrue(
            any(s <= BUSY_START and e >= BUSY_END for s, e in busy),
            f"Expected OAuthBusyWindow {BUSY_START}–{BUSY_END} UTC in public profile busy list; got: {busy}",
        )

    def test_oauth_busy_secondary_calendar_appears_in_public_profile(self):
        """Secondary-calendar OAuthBusyWindow must also surface on the public profile."""
        secondary_cal_id = "c_a2bd8e4c123abc456def789012345678@group.calendar.google.com"
        _make_oauth_busy(self.host, self.g, calendar_id=secondary_cal_id)

        resp = self.client.get(
            f"/api/public/profile/{self.host.share_token}",
            {"from": WINDOW_FROM, "to": WINDOW_TO},
        )
        self.assertEqual(resp.status_code, 200, resp.data)

        busy = _busy_instants(resp.data)
        self.assertTrue(
            any(s <= BUSY_START and e >= BUSY_END for s, e in busy),
            f"Expected secondary-calendar OAuthBusyWindow in public profile; got: {busy}",
        )

    def test_no_oauth_busy_yields_empty_busy_list_on_public_profile(self):
        """Without OAuthBusyWindow rows the public profile must return an empty busy list."""
        resp = self.client.get(
            f"/api/public/profile/{self.host.share_token}",
            {"from": WINDOW_FROM, "to": WINDOW_TO},
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        busy = _busy_instants(resp.data)
        self.assertEqual(busy, [])
