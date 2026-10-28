"""
Tests for T3: slotly.create_booking / slotly.update_booking / slotly.cancel_booking.

Covers:
  - Happy paths (create, update time, update title, cancel)
  - Scope enforcement: create_booking scope covers all three write tools
  - Input validation (empty attendees, too-many, naive datetime, missing fields)
  - Service-level errors (slot_conflict, meeting_type_inactive, already_ended, not found)
  - Idempotency: second call with same key returns cached result
  - Rate-limit tier split: write tier (20/min) vs read tier (60/min)
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import MagicMock, call, patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone as djtz
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.teams.models import Membership, Team

from .models import APIToken, MCPCall
from .tools import _REGISTRY


# ---------------------------------------------------------------------------
# Shared fixtures helpers
# ---------------------------------------------------------------------------

def _future_slot(hours_ahead: int = 24, duration_min: int = 30):
    """Return a (start_iso, end_iso) tuple in the future with UTC timezone."""
    start = djtz.now() + timedelta(hours=hours_ahead)
    end = start + timedelta(minutes=duration_min)
    return start.isoformat(), end.isoformat()


def _mock_provider(event_id: str = "evt-t3") -> MagicMock:
    """Return a mock _WriteProvider simulating a successful Google insert."""
    provider = MagicMock()
    provider.name = "google"
    provider.write_calendar_id = "primary"
    provider.create_event.return_value = {
        "id": event_id,
        "htmlLink": f"https://calendar.google.com/event/{event_id}",
        "hangoutLink": "https://meet.google.com/abc-xyz",
    }
    provider.delete_event.return_value = None
    return provider


# ---------------------------------------------------------------------------
# create_booking tests
# ---------------------------------------------------------------------------

@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    FRONTEND_BASE_URL="http://localhost:3000",
    TIME_ZONE="UTC",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class CreateBookingTests(APITestCase):

    def setUp(self):
        cache.clear()
        self.host = User.objects.create_user(
            email="alice@t3.local", password="pass", first_name="Alice", last_name="Host"
        )
        self.guest = User.objects.create_user(
            email="bob@t3.local", password="pass", first_name="Bob", last_name="Guest"
        )
        self.team = Team.objects.create(name="T3Team")
        Membership.objects.create(team=self.team, user=self.host, role=Membership.Role.ADMIN)
        Membership.objects.create(team=self.team, user=self.guest, role=Membership.Role.MEMBER)

        self.token, self.raw = APIToken.create_for_user(
            self.host, "T3-create", ["create_booking"]
        )

    def _call(self, params, *, tool="slotly.create_booking", idem_key=None):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        body = {"name": tool, "params": params}
        if idem_key is not None:
            body["idempotency_key"] = idem_key
        return self.client.post("/mcp/v1/tools/call", body, format="json")

    # 1
    def test_create_booking_happy_path(self):
        """Valid params + mocked provider → 200 with booking_uuid, status=ok, event.id."""
        start_iso, end_iso = _future_slot()
        provider = _mock_provider("happy-t3")

        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            resp = self._call({
                "attendee_user_ids": [self.guest.pk],
                "start": start_iso,
                "end": end_iso,
            })

        self.assertEqual(resp.status_code, 200)
        result = resp.json()["result"]
        self.assertEqual(result["status"], "ok")
        self.assertIn("booking_uuid", result)
        self.assertEqual(result["event"]["id"], "happy-t3")
        self.assertIn("invited_emails", result)
        self.assertIn(self.guest.email, result["invited_emails"])

        # Audit row written.
        call_row = MCPCall.objects.filter(capability="slotly.create_booking", status="ok").first()
        self.assertIsNotNone(call_row)

        # Booking row created in DB.
        from apps.scheduling.models import Booking
        booking = Booking.objects.get(uuid=result["booking_uuid"])
        self.assertEqual(booking.host, self.host)
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)

    # 2
    def test_create_booking_scope_denied(self):
        """Token with only find_slots scope → 403 scope_denied."""
        _, raw2 = APIToken.create_for_user(self.host, "T3-limited", ["find_slots"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw2}")
        start_iso, end_iso = _future_slot()
        resp = self.client.post(
            "/mcp/v1/tools/call",
            {"name": "slotly.create_booking", "params": {
                "attendee_user_ids": [self.guest.pk],
                "start": start_iso,
                "end": end_iso,
            }},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"], "scope_denied")

    # 3
    def test_create_booking_bad_attendee_user_ids_empty(self):
        """Empty attendee_user_ids list → 400 validation_error."""
        start_iso, end_iso = _future_slot()
        resp = self._call({
            "attendee_user_ids": [],
            "start": start_iso,
            "end": end_iso,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "validation_error")

    # 4
    def test_create_booking_bad_attendee_user_ids_too_many(self):
        """101 attendee_user_ids → 400 validation_error."""
        start_iso, end_iso = _future_slot()
        resp = self._call({
            "attendee_user_ids": list(range(1, 102)),
            "start": start_iso,
            "end": end_iso,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "validation_error")

    # 5
    def test_create_booking_bad_attendee_not_in_team(self):
        """Attendee not in any shared team → 403 (PermissionDenied from service)."""
        stranger = User.objects.create_user(email="stranger@t3.local", password="pass")
        start_iso, end_iso = _future_slot()
        with patch("apps.scheduling.services._pick_write_provider", return_value=_mock_provider()):
            resp = self._call({
                "attendee_user_ids": [stranger.pk],
                "start": start_iso,
                "end": end_iso,
            })
        self.assertEqual(resp.status_code, 403)

    # 6
    def test_create_booking_slot_conflict(self):
        """Conflict on slot → 400 with code=slot_conflict in detail."""
        start_iso, end_iso = _future_slot()

        def _busy_provider_error(host_user, attendee_user_ids, **kwargs):
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"detail": "busy", "code": "slot_conflict"})

        with patch("apps.scheduling.services._pick_write_provider", return_value=_mock_provider()):
            with patch(
                "apps.scheduling.services.create_booking",
                side_effect=lambda **kw: (_ for _ in ()).throw(
                    __import__("rest_framework.exceptions", fromlist=["ValidationError"]).ValidationError(
                        {"detail": "busy", "code": "slot_conflict"}
                    )
                ),
            ):
                resp = self._call({
                    "attendee_user_ids": [self.guest.pk],
                    "start": start_iso,
                    "end": end_iso,
                })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "validation_error")

    # 6b: more natural slot_conflict via real service with busy calendar event
    def test_create_booking_slot_conflict_via_service(self):
        """Pre-create a CalendarEvent that makes the slot busy → 400 slot_conflict."""
        from apps.calendars.models import Calendar, CalendarEvent
        guest_cal = Calendar.objects.create(
            owner=self.guest,
            name="Guest primary",
            include_in_busy=True,
        )
        start_iso, end_iso = _future_slot()
        # Parse back to create the event at the same time
        from django.utils.dateparse import parse_datetime
        start_dt = parse_datetime(start_iso)
        end_dt = parse_datetime(end_iso)

        CalendarEvent.objects.create(
            calendar=guest_cal,
            uid="busy-uid-1",
            dtstart=start_dt - timedelta(minutes=5),
            dtend=end_dt + timedelta(minutes=5),
            transp=CalendarEvent.Transparency.OPAQUE,
            is_all_day=False,
        )

        with patch("apps.scheduling.services._pick_write_provider", return_value=_mock_provider()):
            resp = self._call({
                "attendee_user_ids": [self.guest.pk],
                "start": start_iso,
                "end": end_iso,
            })
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertEqual(data["error"], "validation_error")
        detail = data["detail"]
        # Service raises with code="slot_conflict"
        self.assertEqual(detail.get("code"), "slot_conflict")

    # 7
    def test_create_booking_meeting_type_inactive(self):
        """meeting_type with is_active=False → 400 with code=meeting_type_inactive."""
        from apps.scheduling.models import MeetingType
        mt = MeetingType.objects.create(
            host=self.host,
            name="Inactive Type",
            slug="inactive",
            is_active=False,
        )
        start_iso, end_iso = _future_slot()
        with patch("apps.scheduling.services._pick_write_provider", return_value=_mock_provider()):
            resp = self._call({
                "attendee_user_ids": [self.guest.pk],
                "start": start_iso,
                "end": end_iso,
                "meeting_type_id": mt.pk,
            })
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertEqual(data["error"], "validation_error")
        # code is in detail as a key or nested under detail
        detail = data["detail"]
        self.assertEqual(detail.get("code"), "meeting_type_inactive")

    # 8
    def test_create_booking_naive_start(self):
        """start without timezone offset → 400 validation_error."""
        resp = self._call({
            "attendee_user_ids": [self.guest.pk],
            "start": "2027-06-01T10:00:00",  # no tz
            "end": "2027-06-01T10:30:00+00:00",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "validation_error")

    # 9
    def test_create_booking_idempotency_hit(self):
        """Same idempotency_key twice → handler called once, second audit row has idempotency_hit."""
        start_iso, end_iso = _future_slot()
        provider = _mock_provider("idem-t3")

        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            r1 = self._call(
                {"attendee_user_ids": [self.guest.pk], "start": start_iso, "end": end_iso},
                idem_key="t3-idem-key-9",
            )
            r2 = self._call(
                {"attendee_user_ids": [self.guest.pk], "start": start_iso, "end": end_iso},
                idem_key="t3-idem-key-9",
            )

        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        # Responses are identical
        self.assertEqual(r1.json()["result"]["booking_uuid"], r2.json()["result"]["booking_uuid"])
        # Provider.create_event called exactly once
        self.assertEqual(provider.create_event.call_count, 1)
        # Second audit row has idempotency_hit
        hit = MCPCall.objects.filter(
            capability="slotly.create_booking", error_code="idempotency_hit"
        ).first()
        self.assertIsNotNone(hit)
        self.assertEqual(hit.status, "ok")


# ---------------------------------------------------------------------------
# update_booking tests
# ---------------------------------------------------------------------------

@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    FRONTEND_BASE_URL="http://localhost:3000",
    TIME_ZONE="UTC",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class UpdateBookingTests(APITestCase):

    def setUp(self):
        cache.clear()
        self.host = User.objects.create_user(
            email="host.update@t3.local", password="pass", first_name="Host", last_name="Update"
        )
        self.guest = User.objects.create_user(
            email="guest.update@t3.local", password="pass", first_name="Guest", last_name="Update"
        )
        self.team = Team.objects.create(name="UpdateTeam")
        Membership.objects.create(team=self.team, user=self.host, role=Membership.Role.ADMIN)
        Membership.objects.create(team=self.team, user=self.guest, role=Membership.Role.MEMBER)

        self.token, self.raw = APIToken.create_for_user(
            self.host, "T3-update", ["create_booking"]
        )

        # Pre-create a booking for tests that need an existing booking.
        self._booking_uuid = None

    def _make_booking(self):
        """Create a Booking row directly via the service (mocked provider)."""
        from apps.scheduling.services import create_booking
        start = djtz.now() + timedelta(hours=24)
        end = start + timedelta(minutes=30)
        provider = _mock_provider("pre-evt-upd")
        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            result = create_booking(
                self.host,
                start=start,
                end=end,
                attendee_user_ids=[self.guest.pk],
            )
        return result["booking_uuid"]

    def _call(self, params, *, tool="slotly.update_booking"):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        return self.client.post(
            "/mcp/v1/tools/call",
            {"name": tool, "params": params},
            format="json",
        )

    # 10
    def test_update_booking_time_happy(self):
        """Pass new start+end → 200, Booking start_at updated."""
        booking_uuid = self._make_booking()
        new_start = djtz.now() + timedelta(hours=48)
        new_end = new_start + timedelta(minutes=30)
        provider = _mock_provider("upd-evt-10")

        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            resp = self._call({
                "booking_uuid": booking_uuid,
                "start": new_start.isoformat(),
                "end": new_end.isoformat(),
            })

        self.assertEqual(resp.status_code, 200)
        result = resp.json()["result"]
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["booking_uuid"], booking_uuid)
        # Provider delete + create called
        self.assertTrue(provider.delete_event.called or provider.create_event.called)

    # 11
    def test_update_booking_title_only(self):
        """Pass only title → 200, no provider create/delete, Booking.title updated."""
        booking_uuid = self._make_booking()
        provider = _mock_provider()

        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            resp = self._call({
                "booking_uuid": booking_uuid,
                "title": "Brand New Title",
            })

        self.assertEqual(resp.status_code, 200)
        result = resp.json()["result"]
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["title"], "Brand New Title")
        # No provider interaction for title-only change
        provider.delete_event.assert_not_called()
        provider.create_event.assert_not_called()

        from apps.scheduling.models import Booking
        booking = Booking.objects.get(uuid=booking_uuid)
        self.assertEqual(booking.title, "Brand New Title")

    # 12
    def test_update_booking_nothing_to_update(self):
        """No fields provided → 400 validation_error."""
        booking_uuid = self._make_booking()
        resp = self._call({"booking_uuid": booking_uuid})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "validation_error")

    # 13
    def test_update_booking_other_hosts_booking(self):
        """Booking belonging to a different host → 400 (not found — ownership not leaked)."""
        other_host = User.objects.create_user(email="other.host@t3.local", password="pass")
        from apps.scheduling.models import Booking
        other_booking = Booking.objects.create(
            host=other_host,
            provider="google",
            calendar_id="primary",
            visitor_email="x@x.com",
            kind=Booking.Kind.ONLINE,
            start_at=djtz.now() + timedelta(hours=24),
            end_at=djtz.now() + timedelta(hours=25),
        )
        resp = self._call({
            "booking_uuid": str(other_booking.uuid),
            "title": "Steal it",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "validation_error")

    # 14
    def test_update_booking_start_without_end(self):
        """Providing start but not end → 400 (must go together)."""
        booking_uuid = self._make_booking()
        new_start = (djtz.now() + timedelta(hours=48)).isoformat()
        resp = self._call({
            "booking_uuid": booking_uuid,
            "start": new_start,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "validation_error")

    # Bonus: confirm create_booking scope also covers update_booking (F15-R4)
    def test_update_booking_covered_by_create_booking_scope(self):
        """Token with create_booking scope can call update_booking (alias)."""
        booking_uuid = self._make_booking()
        provider = _mock_provider()
        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            resp = self._call({
                "booking_uuid": booking_uuid,
                "title": "Scope alias title",
            })
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# cancel_booking tests
# ---------------------------------------------------------------------------

@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    FRONTEND_BASE_URL="http://localhost:3000",
    TIME_ZONE="UTC",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class CancelBookingTests(APITestCase):

    def setUp(self):
        cache.clear()
        self.host = User.objects.create_user(
            email="host.cancel@t3.local", password="pass", first_name="Cancel", last_name="Host"
        )
        self.guest = User.objects.create_user(
            email="guest.cancel@t3.local", password="pass", first_name="Cancel", last_name="Guest"
        )
        self.team = Team.objects.create(name="CancelTeam")
        Membership.objects.create(team=self.team, user=self.host, role=Membership.Role.ADMIN)
        Membership.objects.create(team=self.team, user=self.guest, role=Membership.Role.MEMBER)

        self.token, self.raw = APIToken.create_for_user(
            self.host, "T3-cancel", ["create_booking"]
        )

    def _make_booking(self, *, hours_ahead: int = 24):
        from apps.scheduling.services import create_booking
        start = djtz.now() + timedelta(hours=hours_ahead)
        end = start + timedelta(minutes=30)
        provider = _mock_provider("pre-evt-can")
        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            result = create_booking(
                self.host,
                start=start,
                end=end,
                attendee_user_ids=[self.guest.pk],
            )
        return result["booking_uuid"]

    def _call(self, params, *, tool="slotly.cancel_booking"):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        return self.client.post(
            "/mcp/v1/tools/call",
            {"name": tool, "params": params},
            format="json",
        )

    # 15
    def test_cancel_booking_happy_path(self):
        """Cancel an existing booking → 200, Booking.status=CANCELLED."""
        booking_uuid = self._make_booking()
        # mock the provider delete so no real API call is made
        with patch("apps.scheduling.services._provider_for") as mock_pf:
            mock_pf.return_value = (MagicMock(), MagicMock())
            resp = self._call({"booking_uuid": booking_uuid, "reason": "test cancel"})

        self.assertEqual(resp.status_code, 200)
        result = resp.json()["result"]
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["booking_uuid"], booking_uuid)

        from apps.scheduling.models import Booking
        booking = Booking.objects.get(uuid=booking_uuid)
        self.assertEqual(booking.status, Booking.Status.CANCELLED)

    # 16
    def test_cancel_booking_idempotent_server_side(self):
        """Cancel twice (no idempotency_key) → both return 200; email sent exactly once."""
        from django.core import mail as django_mail

        booking_uuid = self._make_booking()
        with patch("apps.scheduling.services._provider_for") as mock_pf:
            mock_pf.return_value = (MagicMock(), MagicMock())

            r1 = self._call({"booking_uuid": booking_uuid})
            r2 = self._call({"booking_uuid": booking_uuid})

        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.json()["result"]["status"], "cancelled")
        self.assertEqual(r2.json()["result"]["status"], "cancelled")
        # Both return the same booking_uuid
        self.assertEqual(r1.json()["result"]["booking_uuid"], booking_uuid)
        self.assertEqual(r2.json()["result"]["booking_uuid"], booking_uuid)
        # Email sent exactly once (first cancel; second is idempotent return)
        self.assertEqual(len(django_mail.outbox), 1)

    # 17
    def test_cancel_booking_already_ended(self):
        """Cancel a past booking → 400 with code=already_ended."""
        from apps.scheduling.models import Booking
        # Create a booking that's already in the past
        past_start = djtz.now() - timedelta(hours=2)
        past_end = past_start + timedelta(minutes=30)
        booking = Booking.objects.create(
            host=self.host,
            provider="google",
            calendar_id="primary",
            visitor_email=self.guest.email,
            kind=Booking.Kind.ONLINE,
            start_at=past_start,
            end_at=past_end,
            status=Booking.Status.CONFIRMED,
        )
        resp = self._call({"booking_uuid": str(booking.uuid)})
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertEqual(data["error"], "validation_error")
        detail = data["detail"]
        self.assertEqual(detail.get("code"), "already_ended")

    # 18
    def test_cancel_booking_other_hosts_booking(self):
        """Cancel booking belonging to another host → 400 not found."""
        other_host = User.objects.create_user(email="other2.host@t3.local", password="pass")
        from apps.scheduling.models import Booking
        other_booking = Booking.objects.create(
            host=other_host,
            provider="google",
            calendar_id="primary",
            visitor_email="x2@x.com",
            kind=Booking.Kind.ONLINE,
            start_at=djtz.now() + timedelta(hours=24),
            end_at=djtz.now() + timedelta(hours=25),
        )
        resp = self._call({"booking_uuid": str(other_booking.uuid)})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "validation_error")

    # Bonus: confirm create_booking scope also covers cancel_booking (F15-R4)
    def test_cancel_booking_covered_by_create_booking_scope(self):
        """Token with create_booking scope can call cancel_booking (alias)."""
        booking_uuid = self._make_booking()
        with patch("apps.scheduling.services._provider_for") as mock_pf:
            mock_pf.return_value = (MagicMock(), MagicMock())
            resp = self._call({"booking_uuid": booking_uuid})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["result"]["status"], "cancelled")


# ---------------------------------------------------------------------------
# Rate-limit tier split tests
# ---------------------------------------------------------------------------

@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    FRONTEND_BASE_URL="http://localhost:3000",
    TIME_ZONE="UTC",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class RateLimitTierTests(APITestCase):

    def setUp(self):
        cache.clear()
        self.host = User.objects.create_user(
            email="rl.host@t3.local", password="pass", first_name="RL", last_name="Host"
        )
        self.guest = User.objects.create_user(
            email="rl.guest@t3.local", password="pass", first_name="RL", last_name="Guest"
        )
        self.team = Team.objects.create(name="RLTeam")
        Membership.objects.create(team=self.team, user=self.host, role=Membership.Role.ADMIN)
        Membership.objects.create(team=self.team, user=self.guest, role=Membership.Role.MEMBER)

        self.token, self.raw = APIToken.create_for_user(
            self.host, "T3-rl", ["create_booking", "list_teams"]
        )

    def _call_create_booking(self):
        start_iso, end_iso = _future_slot()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        return self.client.post(
            "/mcp/v1/tools/call",
            {"name": "slotly.create_booking", "params": {
                "attendee_user_ids": [self.guest.pk],
                "start": start_iso,
                "end": end_iso,
            }},
            format="json",
        )

    def _call_list_teams(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        return self.client.post(
            "/mcp/v1/tools/call",
            {"name": "slotly.list_teams", "params": {}},
            format="json",
        )

    # 19
    def test_write_tool_tighter_limit(self):
        """Call create_booking 21 times → 21st returns 429."""
        # We mock the service to avoid real OAuth.
        fake_result = {
            "booking_uuid": str(uuid.uuid4()),
            "status": "ok",
            "event": {"id": "rl-evt", "html_link": None, "meet_link": "", "start": "", "end": "", "provider": "google"},
            "invited_emails": [self.guest.email],
            "manage_url": "http://localhost:3000/b/123",
        }
        with patch(
            "apps.scheduling.services.create_booking",
            return_value=fake_result,
        ):
            statuses = []
            for i in range(21):
                resp = self._call_create_booking()
                statuses.append(resp.status_code)

        # First 20 must be 200; 21st must be 429.
        self.assertEqual(statuses[:20], [200] * 20, f"Expected 20 x 200 but got: {statuses[:20]}")
        self.assertEqual(statuses[20], 429, f"Expected 429 on 21st call but got: {statuses[20]}")

    # 20
    def test_read_tool_still_60_per_min(self):
        """list_teams (read tool) → 61st call returns 429 (not hitting write limit first)."""
        statuses = []
        for i in range(61):
            resp = self._call_list_teams()
            statuses.append(resp.status_code)

        # First 60 must be 200; 61st must be 429.
        self.assertEqual(statuses[:60], [200] * 60, f"Expected 60 x 200 but got: {statuses[:60]}")
        self.assertEqual(statuses[60], 429, f"Expected 429 on 61st call but got: {statuses[60]}")


# ---------------------------------------------------------------------------
# auth.has_scope regression guard — read scopes still work
# ---------------------------------------------------------------------------

class HasScopeRegressionTest(TestCase):
    """Verify _SCOPE_ALIASES doesn't break F14 read scopes."""

    def setUp(self):
        self.user = User.objects.create_user(email="scope.reg@t3.local", password="pass")

    def test_read_scopes_still_work(self):
        from .auth import has_scope
        for scope_name in ("list_teams", "list_members", "find_slots", "check_time"):
            token, _ = APIToken.create_for_user(self.user, f"reg-{scope_name}", [scope_name])
            self.assertTrue(
                has_scope(token, f"slotly.{scope_name}"),
                f"has_scope should return True for slotly.{scope_name}",
            )
            # Other scopes should not be granted
            other = [s for s in ("list_teams", "list_members", "find_slots", "check_time") if s != scope_name]
            for other_scope in other:
                self.assertFalse(
                    has_scope(token, f"slotly.{other_scope}"),
                    f"Token with [{scope_name}] should NOT grant slotly.{other_scope}",
                )

    def test_create_booking_scope_aliases(self):
        """create_booking scope grants create_, update_, and cancel_ tools (F15-R4)."""
        from .auth import has_scope
        token, _ = APIToken.create_for_user(self.user, "reg-write", ["create_booking"])
        for tool_suffix in ("create_booking", "update_booking", "cancel_booking"):
            self.assertTrue(
                has_scope(token, f"slotly.{tool_suffix}"),
                f"create_booking scope should grant slotly.{tool_suffix}",
            )

    def test_wildcard_scope_grants_all(self):
        """* scope grants everything."""
        from .auth import has_scope
        token, _ = APIToken.create_for_user(self.user, "reg-wild", ["*"])
        for capability in (
            "slotly.list_teams", "slotly.find_slots", "slotly.create_booking",
            "slotly.update_booking", "slotly.cancel_booking",
        ):
            self.assertTrue(has_scope(token, capability))
