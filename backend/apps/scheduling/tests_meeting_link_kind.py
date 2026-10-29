"""
TDD tests for Task B1 — MeetingLinkKind schema on Booking + MeetingType.

Covers:
  - Both models have the field with "auto" default.
  - All four valid values are accepted by full_clean().
  - Invalid values are rejected by full_clean().
  - MeetingLinkKind choices are identical on both models (shared class).
"""

from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone as djtz

from apps.accounts.models import User

from .models import Booking, MeetingLinkKind, MeetingType


def _make_user(email: str = "host@test.local") -> User:
    return User.objects.create_user(email=email, password="pw-test-123")


def _future_slot(hours_ahead: int = 24, duration_min: int = 30):
    start = djtz.now() + timedelta(hours=hours_ahead)
    end = start + timedelta(minutes=duration_min)
    return start, end


def _make_booking(host: User, **kwargs) -> Booking:
    start, end = _future_slot()
    defaults = dict(
        host=host,
        provider="google",
        calendar_id="primary",
        visitor_name="Test Visitor",
        visitor_email="visitor@test.local",
        attendee_emails=["visitor@test.local"],
        kind=Booking.Kind.ONLINE,
        title="Test Meeting",
        start_at=start,
        end_at=end,
    )
    defaults.update(kwargs)
    return Booking.objects.create(**defaults)


def _make_meeting_type(host: User, **kwargs) -> MeetingType:
    defaults = dict(
        host=host,
        name="30-min chat",
        slug="30-min-chat",
        duration_min=30,
    )
    defaults.update(kwargs)
    return MeetingType.objects.create(**defaults)


class BookingMeetingLinkKindDefaultTests(TestCase):
    def test_booking_has_meeting_link_kind_field_with_auto_default(self):
        host = _make_user()
        booking = _make_booking(host)
        self.assertEqual(booking.meeting_link_kind, "auto")

    def test_booking_meeting_link_kind_persists_across_refresh(self):
        host = _make_user()
        booking = _make_booking(host)
        refreshed = Booking.objects.get(pk=booking.pk)
        self.assertEqual(refreshed.meeting_link_kind, MeetingLinkKind.AUTO)


class MeetingTypeMeetingLinkKindDefaultTests(TestCase):
    def test_meeting_type_has_meeting_link_kind_field_with_auto_default(self):
        host = _make_user()
        mt = _make_meeting_type(host)
        self.assertEqual(mt.meeting_link_kind, "auto")

    def test_meeting_type_meeting_link_kind_persists_across_refresh(self):
        host = _make_user()
        mt = _make_meeting_type(host)
        refreshed = MeetingType.objects.get(pk=mt.pk)
        self.assertEqual(refreshed.meeting_link_kind, MeetingLinkKind.AUTO)


class MeetingLinkKindChoicesTests(TestCase):
    """All four valid values are accepted; invalid values are rejected."""

    VALID_VALUES = ["auto", "google_meet", "teams", "none"]

    def test_booking_accepts_all_valid_values(self):
        host = _make_user()
        for value in self.VALID_VALUES:
            with self.subTest(value=value):
                booking = _make_booking(host, visitor_email=f"{value}@test.local")
                booking.meeting_link_kind = value
                # Should not raise
                booking.full_clean()
                booking.save()
                self.assertEqual(
                    Booking.objects.get(pk=booking.pk).meeting_link_kind,
                    value,
                )

    def test_meeting_type_accepts_all_valid_values(self):
        host = _make_user("host2@test.local")
        for i, value in enumerate(self.VALID_VALUES):
            with self.subTest(value=value):
                mt = _make_meeting_type(host, slug=f"slug-{i}", name=f"Type {i}")
                mt.meeting_link_kind = value
                # Should not raise
                mt.full_clean()
                mt.save()
                self.assertEqual(
                    MeetingType.objects.get(pk=mt.pk).meeting_link_kind,
                    value,
                )

    def test_booking_rejects_invalid_meeting_link_kind(self):
        host = _make_user("host3@test.local")
        booking = _make_booking(host)
        booking.meeting_link_kind = "zoom"  # not in choices
        with self.assertRaises(ValidationError) as ctx:
            booking.full_clean()
        self.assertIn("meeting_link_kind", ctx.exception.message_dict)

    def test_meeting_type_rejects_invalid_meeting_link_kind(self):
        host = _make_user("host4@test.local")
        mt = _make_meeting_type(host)
        mt.meeting_link_kind = "webex"  # not in choices
        with self.assertRaises(ValidationError) as ctx:
            mt.full_clean()
        self.assertIn("meeting_link_kind", ctx.exception.message_dict)


class MeetingLinkKindSharedChoicesTests(TestCase):
    """Both models reference the same MeetingLinkKind choices."""

    def test_booking_and_meeting_type_share_choices(self):
        booking_field = Booking._meta.get_field("meeting_link_kind")
        mt_field = MeetingType._meta.get_field("meeting_link_kind")
        self.assertEqual(booking_field.choices, mt_field.choices)

    def test_meeting_link_kind_has_exactly_four_choices(self):
        self.assertEqual(len(MeetingLinkKind.choices), 4)

    def test_meeting_link_kind_values(self):
        values = [v for v, _ in MeetingLinkKind.choices]
        self.assertIn("auto", values)
        self.assertIn("google_meet", values)
        self.assertIn("teams", values)
        self.assertIn("none", values)

    def test_meeting_link_kind_auto_is_default_for_booking(self):
        booking_field = Booking._meta.get_field("meeting_link_kind")
        self.assertEqual(booking_field.default, MeetingLinkKind.AUTO)

    def test_meeting_link_kind_auto_is_default_for_meeting_type(self):
        mt_field = MeetingType._meta.get_field("meeting_link_kind")
        self.assertEqual(mt_field.default, MeetingLinkKind.AUTO)
