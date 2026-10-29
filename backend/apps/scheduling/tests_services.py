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
                with patch("apps.scheduling.services._notify_booking_rescheduled") as mock_notify:
                    result = update_booking(
                        self.host,
                        str(self.booking.uuid),
                        start=new_start,
                        end=new_end,
                    )

        self.assertEqual(result["status"], "updated")
        # Host-initiated reschedule must NOT fire the visitor-reschedule notification.
        mock_notify.assert_not_called()
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


# ---------------------------------------------------------------------------
# Task A2: _pick_write_provider provider kwarg
# ---------------------------------------------------------------------------


from apps.scheduling.models import GoogleAccount, MicrosoftAccount, WriteCalendarDefault
from apps.scheduling.security import encrypt


def _make_google_svc(user, email="x@gmail.com"):
    from django.utils import timezone
    from datetime import timedelta
    return GoogleAccount.objects.create(
        user=user,
        google_email=email,
        access_token_encrypted=encrypt("AT"),
        refresh_token_encrypted=encrypt("RT"),
        expires_at=timezone.now() + timedelta(hours=1),
    )


def _make_microsoft_svc(user, email="x@outlook.com"):
    from django.utils import timezone
    from datetime import timedelta
    return MicrosoftAccount.objects.create(
        user=user,
        microsoft_email=email,
        access_token_encrypted=encrypt("AT"),
        refresh_token_encrypted=encrypt("RT"),
        expires_at=timezone.now() + timedelta(hours=1),
    )


@override_settings(FRONTEND_BASE_URL="http://localhost:3000")
class PickWriteProviderPerProviderTests(TestCase):
    """A2: _pick_write_provider(user, provider=...) tests."""

    def setUp(self):
        self.user = User.objects.create_user(email="ppv@slotly.test", password="x" * 12)

    def test_pick_write_provider_default_returns_primary_google_when_both_connected(self):
        """provider=None → returns Google when both providers are connected."""
        from apps.scheduling.services import _pick_write_provider

        g = _make_google_svc(self.user, email="ppv@gmail.com")
        m = _make_microsoft_svc(self.user, email="ppv@outlook.com")
        WriteCalendarDefault.objects.create(
            user=self.user, google_account=g, microsoft_account=m
        )

        provider = _pick_write_provider(self.user)
        self.assertIsNotNone(provider)
        self.assertEqual(provider.name, "google")
        self.assertEqual(provider.account_id, g.pk)

    def test_pick_write_provider_forced_microsoft_returns_ms_even_when_primary_is_google(self):
        """provider='microsoft' → returns Microsoft even though Google default exists."""
        from apps.scheduling.services import _pick_write_provider

        g = _make_google_svc(self.user, email="ppv@gmail.com")
        m = _make_microsoft_svc(self.user, email="ppv@outlook.com")
        WriteCalendarDefault.objects.create(
            user=self.user, google_account=g, microsoft_account=m
        )

        provider = _pick_write_provider(self.user, provider="microsoft")
        self.assertIsNotNone(provider)
        self.assertEqual(provider.name, "microsoft")
        self.assertEqual(provider.account_id, m.pk)

    def test_pick_write_provider_forced_provider_returns_none_when_that_provider_not_connected(self):
        """provider='google' → returns None when only MS is connected (no fallback)."""
        from apps.scheduling.services import _pick_write_provider

        m = _make_microsoft_svc(self.user, email="ppv@outlook.com")
        WriteCalendarDefault.objects.create(user=self.user, microsoft_account=m)

        provider = _pick_write_provider(self.user, provider="google")
        self.assertIsNone(provider)

    def test_pick_write_provider_default_falls_back_to_microsoft_when_only_ms_connected(self):
        """provider=None → falls back to Microsoft when no Google account exists."""
        from apps.scheduling.services import _pick_write_provider

        m = _make_microsoft_svc(self.user, email="ppv@outlook.com")
        WriteCalendarDefault.objects.create(user=self.user, microsoft_account=m)

        provider = _pick_write_provider(self.user)
        self.assertIsNotNone(provider)
        self.assertEqual(provider.name, "microsoft")
        self.assertEqual(provider.account_id, m.pk)

    def test_pick_write_provider_forced_google_returns_google_account(self):
        """provider='google' → returns Google account when Google default is set."""
        from apps.scheduling.services import _pick_write_provider

        g = _make_google_svc(self.user, email="ppv@gmail.com")
        m = _make_microsoft_svc(self.user, email="ppv@outlook.com")
        WriteCalendarDefault.objects.create(
            user=self.user, google_account=g, microsoft_account=m
        )

        provider = _pick_write_provider(self.user, provider="google")
        self.assertIsNotNone(provider)
        self.assertEqual(provider.name, "google")
        self.assertEqual(provider.account_id, g.pk)

    def test_pick_write_provider_forced_microsoft_returns_none_when_ms_not_set_in_default(self):
        """provider='microsoft' → returns None when WCD only has Google FK."""
        from apps.scheduling.services import _pick_write_provider

        g = _make_google_svc(self.user, email="ppv@gmail.com")
        WriteCalendarDefault.objects.create(user=self.user, google_account=g)

        provider = _pick_write_provider(self.user, provider="microsoft")
        self.assertIsNone(provider)


# ---------------------------------------------------------------------------
# Task B2: _resolve_write_provider_for_kind + create_booking meeting_link_kind
# ---------------------------------------------------------------------------


@override_settings(FRONTEND_BASE_URL="http://localhost:3000", TIME_ZONE="UTC")
class ResolveWriteProviderForKindTests(TestCase):
    """Unit tests for _resolve_write_provider_for_kind."""

    def setUp(self):
        self.user = User.objects.create_user(email="kind@slotly.test", password="x" * 12)

    def test_google_meet_kind_routes_to_google(self):
        from apps.scheduling.services import _resolve_write_provider_for_kind

        g = _make_google_svc(self.user, email="kind@gmail.com")
        m = _make_microsoft_svc(self.user, email="kind@outlook.com")
        WriteCalendarDefault.objects.create(
            user=self.user, google_account=g, microsoft_account=m
        )

        provider = _resolve_write_provider_for_kind(self.user, "google_meet")
        self.assertIsNotNone(provider)
        self.assertEqual(provider.name, "google")

    def test_teams_kind_routes_to_microsoft(self):
        from apps.scheduling.services import _resolve_write_provider_for_kind

        g = _make_google_svc(self.user, email="kind@gmail.com")
        m = _make_microsoft_svc(self.user, email="kind@outlook.com")
        WriteCalendarDefault.objects.create(
            user=self.user, google_account=g, microsoft_account=m
        )

        provider = _resolve_write_provider_for_kind(self.user, "teams")
        self.assertIsNotNone(provider)
        self.assertEqual(provider.name, "microsoft")

    def test_auto_kind_returns_primary_provider(self):
        from apps.scheduling.services import _resolve_write_provider_for_kind

        g = _make_google_svc(self.user, email="kind@gmail.com")
        WriteCalendarDefault.objects.create(user=self.user, google_account=g)

        provider = _resolve_write_provider_for_kind(self.user, "auto")
        self.assertIsNotNone(provider)
        self.assertEqual(provider.name, "google")

    def test_none_kind_returns_primary_provider(self):
        from apps.scheduling.services import _resolve_write_provider_for_kind

        g = _make_google_svc(self.user, email="kind@gmail.com")
        WriteCalendarDefault.objects.create(user=self.user, google_account=g)

        provider = _resolve_write_provider_for_kind(self.user, "none")
        self.assertIsNotNone(provider)
        self.assertEqual(provider.name, "google")

    def test_google_meet_kind_raises_when_google_not_connected(self):
        from apps.scheduling.services import _resolve_write_provider_for_kind
        from rest_framework.exceptions import ValidationError

        m = _make_microsoft_svc(self.user, email="kind@outlook.com")
        WriteCalendarDefault.objects.create(user=self.user, microsoft_account=m)

        with self.assertRaises(ValidationError) as ctx:
            _resolve_write_provider_for_kind(self.user, "google_meet")
        self.assertIn("meeting_link_kind", ctx.exception.detail)
        self.assertIn("Google", str(ctx.exception.detail["meeting_link_kind"]))

    def test_teams_kind_raises_when_microsoft_not_connected(self):
        from apps.scheduling.services import _resolve_write_provider_for_kind
        from rest_framework.exceptions import ValidationError

        g = _make_google_svc(self.user, email="kind@gmail.com")
        WriteCalendarDefault.objects.create(user=self.user, google_account=g)

        with self.assertRaises(ValidationError) as ctx:
            _resolve_write_provider_for_kind(self.user, "teams")
        self.assertIn("meeting_link_kind", ctx.exception.detail)
        self.assertIn("Microsoft", str(ctx.exception.detail["meeting_link_kind"]))

    def test_invalid_kind_raises_validation_error(self):
        from apps.scheduling.services import _resolve_write_provider_for_kind
        from rest_framework.exceptions import ValidationError

        g = _make_google_svc(self.user, email="kind@gmail.com")
        WriteCalendarDefault.objects.create(user=self.user, google_account=g)

        with self.assertRaises(ValidationError) as ctx:
            _resolve_write_provider_for_kind(self.user, "zoom")
        self.assertIn("meeting_link_kind", ctx.exception.detail)


@override_settings(FRONTEND_BASE_URL="http://localhost:3000", TIME_ZONE="UTC")
class CreateBookingMeetingLinkKindTests(TestCase):
    """B2: create_booking routes to correct provider via meeting_link_kind."""

    def setUp(self):
        self.host = User.objects.create_user(email="host@b2.local", password="pw123")
        self.attendee = User.objects.create_user(email="att@b2.local", password="pw123")
        from apps.teams.models import Team, Membership
        team = Team.objects.create(name="B2Team")
        Membership.objects.create(team=team, user=self.host, role=Membership.Role.ADMIN)
        Membership.objects.create(team=team, user=self.attendee, role=Membership.Role.MEMBER)

    def _make_providers(self):
        g = _make_google_svc(self.host, email="host@gmail.com")
        m = _make_microsoft_svc(self.host, email="host@outlook.com")
        WriteCalendarDefault.objects.create(
            user=self.host, google_account=g, microsoft_account=m
        )
        return g, m

    def test_create_booking_meeting_link_kind_teams_routes_to_microsoft(self):
        """meeting_link_kind='teams' → Booking.provider == 'microsoft'."""
        g, m = self._make_providers()
        start, end = _future_slot()
        ms_provider = MagicMock()
        ms_provider.name = "microsoft"
        ms_provider.write_calendar_id = ""
        ms_provider.create_event.return_value = {"id": "ms-evt-1", "webLink": "https://outlook.com/e/ms-evt-1"}

        with patch("apps.scheduling.services._resolve_write_provider_for_kind", return_value=ms_provider):
            result = create_booking(
                self.host,
                start=start,
                end=end,
                attendee_user_ids=[self.attendee.pk],
                meeting_link_kind="teams",
            )

        self.assertEqual(result["status"], "ok")
        booking = Booking.objects.get(uuid=result["booking_uuid"])
        self.assertEqual(booking.provider, "microsoft")
        self.assertEqual(booking.meeting_link_kind, "teams")

    def test_create_booking_meeting_link_kind_google_meet_routes_to_google(self):
        """meeting_link_kind='google_meet' → Booking.provider == 'google'."""
        g, m = self._make_providers()
        start, end = _future_slot()
        google_provider = _mock_provider("goog-evt-1")

        with patch("apps.scheduling.services._resolve_write_provider_for_kind", return_value=google_provider):
            result = create_booking(
                self.host,
                start=start,
                end=end,
                attendee_user_ids=[self.attendee.pk],
                meeting_link_kind="google_meet",
            )

        self.assertEqual(result["status"], "ok")
        booking = Booking.objects.get(uuid=result["booking_uuid"])
        self.assertEqual(booking.provider, "google")
        self.assertEqual(booking.meeting_link_kind, "google_meet")

    def test_create_booking_requested_provider_not_connected_raises_validation_error(self):
        """meeting_link_kind='teams' when only Google connected → ValidationError."""
        g = _make_google_svc(self.host, email="host@gmail.com")
        WriteCalendarDefault.objects.create(user=self.host, google_account=g)

        start, end = _future_slot()
        with self.assertRaises(Exception) as ctx:
            create_booking(
                self.host,
                start=start,
                end=end,
                attendee_user_ids=[self.attendee.pk],
                meeting_link_kind="teams",
            )
        # Expect a DRF ValidationError keyed on "meeting_link_kind".
        from rest_framework.exceptions import ValidationError
        self.assertIsInstance(ctx.exception, ValidationError)
        self.assertIn("meeting_link_kind", ctx.exception.detail)
        self.assertIn("Microsoft", str(ctx.exception.detail["meeting_link_kind"]))
