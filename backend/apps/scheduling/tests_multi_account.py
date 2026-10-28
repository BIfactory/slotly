"""
Tests for multi-account OAuth model layer.

Covers the WriteCalendarDefault XOR constraint (DB-level) and basic
property accessors.  No OAuth callback or REST assertions here — that
is Task 2/3.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.scheduling.models import GoogleAccount, MicrosoftAccount, WriteCalendarDefault
from apps.scheduling.security import encrypt


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
