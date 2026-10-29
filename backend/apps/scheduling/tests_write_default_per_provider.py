"""
Tests for Task A1 — WriteCalendarDefault per-provider schema.

Validates that both google_account and microsoft_account can be simultaneously
non-null on a single WriteCalendarDefault row (one user, two providers), along
with the helper methods: default_write_target, has_default, primary_write_target.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.scheduling.models import GoogleAccount, MicrosoftAccount, WriteCalendarDefault
from apps.scheduling.security import encrypt


def _make_google(user, email="g@gmail.com"):
    return GoogleAccount.objects.create(
        user=user,
        google_email=email,
        access_token_encrypted=encrypt("AT"),
        refresh_token_encrypted=encrypt("RT"),
        expires_at=timezone.now() + timedelta(hours=1),
    )


def _make_microsoft(user, email="m@outlook.com"):
    return MicrosoftAccount.objects.create(
        user=user,
        microsoft_email=email,
        access_token_encrypted=encrypt("AT"),
        refresh_token_encrypted=encrypt("RT"),
        expires_at=timezone.now() + timedelta(hours=1),
    )


class WriteDefaultPerProviderTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(email="alice@wdpp.test", password="x" * 12)

    def test_can_have_both_google_and_microsoft_defaults_simultaneously(self):
        """Core A1 requirement: both FKs may be non-null at the same time."""
        g = _make_google(self.alice, email="alice@gmail.com")
        m = _make_microsoft(self.alice, email="alice@outlook.com")
        wd = WriteCalendarDefault.objects.create(
            user=self.alice, google_account=g, microsoft_account=m,
        )
        self.assertEqual(wd.default_write_target("google").pk, g.pk)
        self.assertEqual(wd.default_write_target("microsoft").pk, m.pk)

    def test_default_write_target_google_only(self):
        """default_write_target returns the FK for google when only google is set."""
        g = _make_google(self.alice)
        wd = WriteCalendarDefault.objects.create(user=self.alice, google_account=g)
        self.assertEqual(wd.default_write_target("google").pk, g.pk)
        self.assertIsNone(wd.default_write_target("microsoft"))

    def test_default_write_target_microsoft_only(self):
        """default_write_target returns the FK for microsoft when only ms is set."""
        m = _make_microsoft(self.alice)
        wd = WriteCalendarDefault.objects.create(user=self.alice, microsoft_account=m)
        self.assertIsNone(wd.default_write_target("google"))
        self.assertEqual(wd.default_write_target("microsoft").pk, m.pk)

    def test_default_write_target_unknown_provider_returns_none(self):
        """Unknown provider string returns None, does not raise."""
        g = _make_google(self.alice)
        wd = WriteCalendarDefault.objects.create(user=self.alice, google_account=g)
        self.assertIsNone(wd.default_write_target("yahoo"))

    def test_has_default_true_for_set_provider(self):
        g = _make_google(self.alice)
        wd = WriteCalendarDefault.objects.create(user=self.alice, google_account=g)
        self.assertTrue(wd.has_default("google"))
        self.assertFalse(wd.has_default("microsoft"))

    def test_has_default_both_true_when_both_set(self):
        g = _make_google(self.alice)
        m = _make_microsoft(self.alice)
        wd = WriteCalendarDefault.objects.create(
            user=self.alice, google_account=g, microsoft_account=m
        )
        self.assertTrue(wd.has_default("google"))
        self.assertTrue(wd.has_default("microsoft"))

    def test_primary_write_target_prefers_google(self):
        """primary_write_target returns google_account when both are set."""
        g = _make_google(self.alice)
        m = _make_microsoft(self.alice)
        wd = WriteCalendarDefault.objects.create(
            user=self.alice, google_account=g, microsoft_account=m
        )
        self.assertEqual(wd.primary_write_target().pk, g.pk)

    def test_primary_write_target_falls_back_to_microsoft(self):
        """primary_write_target returns microsoft when google is not set."""
        m = _make_microsoft(self.alice)
        wd = WriteCalendarDefault.objects.create(user=self.alice, microsoft_account=m)
        self.assertEqual(wd.primary_write_target().pk, m.pk)

    def test_primary_write_target_returns_google_when_only_google(self):
        g = _make_google(self.alice)
        wd = WriteCalendarDefault.objects.create(user=self.alice, google_account=g)
        self.assertEqual(wd.primary_write_target().pk, g.pk)

    def test_at_least_one_provider_constraint_enforced(self):
        """Both-null row must be rejected by the DB constraint."""
        with self.assertRaises(IntegrityError):
            WriteCalendarDefault.objects.create(user=self.alice)

    def test_google_only_satisfies_at_least_one_constraint(self):
        """Google-only row is valid."""
        g = _make_google(self.alice)
        wd = WriteCalendarDefault.objects.create(user=self.alice, google_account=g)
        self.assertIsNotNone(wd.pk)

    def test_microsoft_only_satisfies_at_least_one_constraint(self):
        """Microsoft-only row is valid."""
        m = _make_microsoft(self.alice)
        wd = WriteCalendarDefault.objects.create(user=self.alice, microsoft_account=m)
        self.assertIsNotNone(wd.pk)
