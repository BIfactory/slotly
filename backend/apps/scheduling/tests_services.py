"""
Tests for apps/scheduling/services.py — the pure-Python service layer.

These tests mock the calendar provider at the services module level
(``apps.scheduling.services._pick_write_provider``) so no real Google or
Microsoft API calls are made. Each test exercises one of the four service
functions: create_booking, cancel_booking, update_booking, create_event.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone as djtz
from rest_framework.exceptions import ValidationError

from apps.accounts.models import User
from apps.teams.models import Team, Membership
from .models import Booking
from .services import (
    cancel_booking,
    create_booking,
    create_event,
    update_booking,
)


def _future_slot(hours_ahead: int = 24, duration_min: int = 30):
    """Return (start, end) datetimes in the future."""
    start = djtz.now() + timedelta(hours=hours_ahead)
    end = start + timedelta(minutes=duration_min)
    return start, end


def _mock_provider(event_id: str = "svc-evt-1") -> MagicMock:
    """Return a mock _WriteProvider that simulates a successful Google insert."""
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


@override_settings(
    FRONTEND_BASE_URL="http://localhost:3000",
    TIME_ZONE="UTC",
)
class CreateBookingHappyPathTest(TestCase):
    """test_create_booking_happy_path — mock provider; assert Booking row + response shape."""

    def setUp(self):
        self.host = User.objects.create_user(email="host@svc.local", password="pw123")
        self.attendee = User.objects.create_user(email="att@svc.local", password="pw123")
        team = Team.objects.create(name="SvcTeam")
        Membership.objects.create(team=team, user=self.host, role=Membership.Role.ADMIN)
        Membership.objects.create(team=team, user=self.attendee, role=Membership.Role.MEMBER)

    def test_create_booking_happy_path(self):
        start, end = _future_slot()
        provider = _mock_provider("happy-evt")

        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            result = create_booking(
                self.host,
                start=start,
                end=end,
                attendee_user_ids=[self.attendee.pk],
            )

        # Service returns expected shape.
        self.assertEqual(result["status"], "ok")
        self.assertIn("booking_uuid", result)
        self.assertEqual(result["event"]["id"], "happy-evt")
        self.assertEqual(result["event"]["provider"], "google")
        self.assertIn("meet_link", result["event"])
        self.assertEqual(result["invited_emails"], [self.attendee.email])
        self.assertIn("manage_url", result)

        # Booking row was persisted.
        booking = Booking.objects.get(uuid=result["booking_uuid"])
        self.assertEqual(booking.host, self.host)
        self.assertEqual(booking.event_id, "happy-evt")
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertIn(self.attendee.email, booking.attendee_emails)

        # Provider was called with the right args.
        provider.create_event.assert_called_once()
        _, kwargs = provider.create_event.call_args
        self.assertIn(self.attendee.email, kwargs["attendees"])


@override_settings(
    FRONTEND_BASE_URL="http://localhost:3000",
    TIME_ZONE="UTC",
)
class CreateBookingSlotConflictTest(TestCase):
    """test_create_booking_slot_conflict — busy attendee raises ValidationError slot_conflict."""

    def setUp(self):
        self.host = User.objects.create_user(email="host@svc.local", password="pw123")
        self.attendee = User.objects.create_user(email="att@svc.local", password="pw123")
        team = Team.objects.create(name="SvcTeam")
        Membership.objects.create(team=team, user=self.host, role=Membership.Role.ADMIN)
        Membership.objects.create(team=team, user=self.attendee, role=Membership.Role.MEMBER)

    def test_create_booking_slot_conflict(self):
        start, end = _future_slot()
        provider = _mock_provider()

        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            # Pretend the host is busy at this slot.
            with patch("apps.scheduling.services._first_busy_user", return_value=self.host):
                with self.assertRaises(ValidationError) as ctx:
                    create_booking(
                        self.host,
                        start=start,
                        end=end,
                        attendee_user_ids=[self.attendee.pk],
                    )

        self.assertEqual(ctx.exception.detail["code"], "slot_conflict")
        # No Booking row was created.
        self.assertEqual(Booking.objects.count(), 0)


@override_settings(FRONTEND_BASE_URL="http://localhost:3000", TIME_ZONE="UTC")
class CreateBookingDurationBoundsTest(TestCase):
    """test_create_booking_duration_bounds — 4 min and 13 h both rejected."""

    def setUp(self):
        self.host = User.objects.create_user(email="host@svc.local", password="pw123")
        self.attendee = User.objects.create_user(email="att@svc.local", password="pw123")
        team = Team.objects.create(name="SvcTeam")
        Membership.objects.create(team=team, user=self.host, role=Membership.Role.ADMIN)
        Membership.objects.create(team=team, user=self.attendee, role=Membership.Role.MEMBER)

    def test_four_minute_booking_rejected(self):
        start = djtz.now() + timedelta(hours=24)
        end = start + timedelta(minutes=4)
        with self.assertRaises(ValidationError):
            create_booking(self.host, start=start, end=end, attendee_user_ids=[self.attendee.pk])

    def test_thirteen_hour_booking_rejected(self):
        start = djtz.now() + timedelta(hours=24)
        end = start + timedelta(hours=13)
        with self.assertRaises(ValidationError):
            create_booking(self.host, start=start, end=end, attendee_user_ids=[self.attendee.pk])


@override_settings(FRONTEND_BASE_URL="http://localhost:3000", TIME_ZONE="UTC")
class CancelBookingHappyPathTest(TestCase):
    """test_cancel_booking_happy_path — booking becomes CANCELLED."""

    def setUp(self):
        self.host = User.objects.create_user(email="host@svc.local", password="pw123")
        start, end = _future_slot()
        self.booking = Booking.objects.create(
            host=self.host,
            provider="google",
            calendar_id="primary",
            visitor_name="Visitor",
            visitor_email="v@example.com",
            attendee_emails=["v@example.com"],
            kind=Booking.Kind.ONLINE,
            title="Test Meeting",
            start_at=start,
            end_at=end,
            event_id="evt-cancel-1",
        )

    def test_cancel_booking_happy_path(self):
        with patch("apps.scheduling.services._provider_for") as mock_pf:
            mock_delete = MagicMock()
            mock_pf.return_value = (MagicMock(), mock_delete)
            with patch("apps.scheduling.services._mail_visitor_host_cancellation"):
                result = cancel_booking(self.host, str(self.booking.uuid), reason="Sick")

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["booking_uuid"], str(self.booking.uuid))
        self.assertEqual(result["reason"], "Sick")
        self.assertIn("cancelled_at", result)

        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CANCELLED)
        self.assertFalse(self.booking.cancelled_by_visitor)
        mock_delete.assert_called_once()


@override_settings(FRONTEND_BASE_URL="http://localhost:3000", TIME_ZONE="UTC")
class CancelBookingIdempotentTest(TestCase):
    """test_cancel_booking_idempotent — second call returns state without re-emailing."""

    def setUp(self):
        self.host = User.objects.create_user(email="host@svc.local", password="pw123")
        start, end = _future_slot()
        self.booking = Booking.objects.create(
            host=self.host,
            provider="google",
            calendar_id="primary",
            visitor_name="Visitor",
            visitor_email="v@example.com",
            attendee_emails=["v@example.com"],
            kind=Booking.Kind.ONLINE,
            title="Test Meeting",
            start_at=start,
            end_at=end,
            event_id="evt-idem",
            status=Booking.Status.CANCELLED,
            cancelled_at=djtz.now(),
            cancellation_reason="Already cancelled",
        )

    def test_cancel_booking_idempotent(self):
        with patch("apps.scheduling.services._mail_visitor_host_cancellation") as mock_mail:
            result = cancel_booking(self.host, str(self.booking.uuid), reason="Again")

        # Returns success without re-mailing.
        self.assertEqual(result["status"], "cancelled")
        mock_mail.assert_not_called()


@override_settings(FRONTEND_BASE_URL="http://localhost:3000", TIME_ZONE="UTC")
class CancelBookingAlreadyEndedTest(TestCase):
    """test_cancel_booking_already_ended — past booking raises ValidationError already_ended."""

    def setUp(self):
        self.host = User.objects.create_user(email="host@svc.local", password="pw123")
        past_end = djtz.now() - timedelta(hours=2)
        past_start = past_end - timedelta(minutes=30)
        self.booking = Booking.objects.create(
            host=self.host,
            provider="google",
            calendar_id="primary",
            visitor_name="Visitor",
            visitor_email="v@example.com",
            attendee_emails=["v@example.com"],
            kind=Booking.Kind.ONLINE,
            title="Past Meeting",
            start_at=past_start,
            end_at=past_end,
            event_id="evt-past",
        )

    def test_cancel_booking_already_ended(self):
        with self.assertRaises(ValidationError) as ctx:
            cancel_booking(self.host, str(self.booking.uuid))

        self.assertEqual(ctx.exception.detail["code"], "already_ended")


@override_settings(FRONTEND_BASE_URL="http://localhost:3000", TIME_ZONE="UTC")
class UpdateBookingTimeChangeTest(TestCase):
    """test_update_booking_time_change — delete_event + create_event called once each."""

    def setUp(self):
        self.host = User.objects.create_user(email="host@svc.local", password="pw123")
        start, end = _future_slot()
        self.booking = Booking.objects.create(
            host=self.host,
            provider="google",
            calendar_id="primary",
            visitor_name="Visitor",
            visitor_email="v@example.com",
            attendee_emails=["v@example.com"],
            kind=Booking.Kind.ONLINE,
            title="Update Me",
            start_at=start,
            end_at=end,
            event_id="evt-before",
        )

    def test_update_booking_time_change(self):
        new_start, new_end = _future_slot(hours_ahead=48)
        provider = _mock_provider("evt-after")

        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            with patch("apps.scheduling.services._first_busy_user", return_value=None):
                with patch("apps.scheduling.services._notify_booking_rescheduled"):
                    result = update_booking(
                        self.host,
                        str(self.booking.uuid),
                        start=new_start,
                        end=new_end,
                    )

        self.assertEqual(result["status"], "updated")
        provider.delete_event.assert_called_once()
        provider.create_event.assert_called_once()
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.event_id, "evt-after")
        self.assertEqual(self.booking.start_at, new_start)


@override_settings(FRONTEND_BASE_URL="http://localhost:3000", TIME_ZONE="UTC")
class UpdateBookingTitleOnlyTest(TestCase):
    """test_update_booking_title_only — no provider delete+create (title-only path)."""

    def setUp(self):
        self.host = User.objects.create_user(email="host@svc.local", password="pw123")
        start, end = _future_slot()
        self.booking = Booking.objects.create(
            host=self.host,
            provider="google",
            calendar_id="primary",
            visitor_name="Visitor",
            visitor_email="v@example.com",
            attendee_emails=["v@example.com"],
            kind=Booking.Kind.ONLINE,
            title="Old Title",
            start_at=start,
            end_at=end,
            event_id="evt-title",
        )

    def test_update_booking_title_only_no_provider_calls(self):
        """Title-only update: no delete_event or create_event is called."""
        provider = _mock_provider()

        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            result = update_booking(
                self.host,
                str(self.booking.uuid),
                title="New Title",
            )

        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["title"], "New Title")
        # No provider calls for title-only update.
        provider.delete_event.assert_not_called()
        provider.create_event.assert_not_called()
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.title, "New Title")


@override_settings(FRONTEND_BASE_URL="http://localhost:3000", TIME_ZONE="UTC")
class UpdateBookingSlotConflictTest(TestCase):
    """test_update_booking_slot_conflict — new time conflicts → ValidationError."""

    def setUp(self):
        self.host = User.objects.create_user(email="host@svc.local", password="pw123")
        start, end = _future_slot()
        self.booking = Booking.objects.create(
            host=self.host,
            provider="google",
            calendar_id="primary",
            visitor_name="Visitor",
            visitor_email="v@example.com",
            attendee_emails=["v@example.com"],
            kind=Booking.Kind.ONLINE,
            title="Test",
            start_at=start,
            end_at=end,
            event_id="evt-conflict",
        )

    def test_update_booking_slot_conflict(self):
        new_start, new_end = _future_slot(hours_ahead=48)
        provider = _mock_provider()

        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            with patch("apps.scheduling.services._first_busy_user", return_value=self.host):
                with self.assertRaises(ValidationError) as ctx:
                    update_booking(
                        self.host,
                        str(self.booking.uuid),
                        start=new_start,
                        end=new_end,
                    )

        self.assertEqual(ctx.exception.detail["code"], "slot_conflict")
        # Provider not called.
        provider.delete_event.assert_not_called()
        provider.create_event.assert_not_called()


@override_settings(FRONTEND_BASE_URL="http://localhost:3000", TIME_ZONE="UTC")
class CreateEventHappyPathTest(TestCase):
    """test_create_event_happy_path — provider.create_event called; no Booking row."""

    def setUp(self):
        self.host = User.objects.create_user(email="host@svc.local", password="pw123")

    def test_create_event_happy_path(self):
        start, end = _future_slot()
        provider = _mock_provider("bare-evt-1")

        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            result = create_event(
                self.host,
                start=start,
                end=end,
                title="All Hands",
                attendee_emails=["alice@example.com", "bob@example.com"],
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["event"]["id"], "bare-evt-1")
        self.assertIn("alice@example.com", result["invited_emails"])
        # No Booking row created.
        self.assertEqual(Booking.objects.count(), 0)
        # Provider called with correct attendees.
        provider.create_event.assert_called_once()
        _, kwargs = provider.create_event.call_args
        self.assertEqual(sorted(kwargs["attendees"]), ["alice@example.com", "bob@example.com"])


@override_settings(FRONTEND_BASE_URL="http://localhost:3000", TIME_ZONE="UTC")
class CreateEventDuration24hMaxTest(TestCase):
    """test_create_event_duration_24h_max — 25 h → ValidationError."""

    def setUp(self):
        self.host = User.objects.create_user(email="host@svc.local", password="pw123")

    def test_create_event_25h_rejected(self):
        start = djtz.now() + timedelta(hours=24)
        end = start + timedelta(hours=25)
        provider = _mock_provider()

        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            with self.assertRaises(ValidationError):
                create_event(self.host, start=start, end=end, title="Too Long")

        provider.create_event.assert_not_called()


@override_settings(FRONTEND_BASE_URL="http://localhost:3000", TIME_ZONE="UTC")
class CreateEventNoAttendeesTest(TestCase):
    """test_create_event_no_attendees — empty list passed to provider (not omitted)."""

    def setUp(self):
        self.host = User.objects.create_user(email="host@svc.local", password="pw123")

    def test_create_event_no_attendees_passes_empty_list(self):
        start, end = _future_slot()
        provider = _mock_provider("solo-evt")

        with patch("apps.scheduling.services._pick_write_provider", return_value=provider):
            result = create_event(
                self.host,
                start=start,
                end=end,
                title="Solo Block",
                attendee_emails=[],
            )

        self.assertEqual(result["invited_emails"], [])
        _, kwargs = provider.create_event.call_args
        # attendees kwarg must be present and be an empty list, not omitted.
        self.assertIn("attendees", kwargs)
        self.assertEqual(kwargs["attendees"], [])
