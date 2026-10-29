"""
TDD tests for Task G1 — UserLocation model.

Covers:
  1. Schema accepts a basic row.
  2. Cascade on user delete removes the user's locations.
  3. UniqueConstraint(user, name) rejects a second row with the same (user, name).
  4. Two different users can both have a location named "Praha" (duplicates OK across users).
  5. is_default defaults to False.
"""

from __future__ import annotations

from django.db import IntegrityError
from django.test import TestCase

from apps.accounts.models import User

from .models import UserLocation


def _make_user(email: str = "host@test.local") -> User:
    return User.objects.create_user(email=email, password="pw-test-123")


def _make_location(user: User, name: str = "Kavárna U Fialek", **kwargs) -> UserLocation:
    defaults = dict(
        user=user,
        name=name,
        address="Vinohradská 42, Praha 2",
    )
    defaults.update(kwargs)
    return UserLocation.objects.create(**defaults)


class UserLocationSchemaTests(TestCase):
    """Case 1: Schema accepts a basic row."""

    def test_create_basic_row(self):
        user = _make_user()
        loc = _make_location(user)
        self.assertIsNotNone(loc.pk)
        refreshed = UserLocation.objects.get(pk=loc.pk)
        self.assertEqual(refreshed.name, "Kavárna U Fialek")
        self.assertEqual(refreshed.address, "Vinohradská 42, Praha 2")
        self.assertEqual(refreshed.user_id, user.pk)

    def test_timestamps_are_set_on_create(self):
        user = _make_user("ts@test.local")
        loc = _make_location(user)
        self.assertIsNotNone(loc.created_at)
        self.assertIsNotNone(loc.updated_at)


class UserLocationCascadeDeleteTests(TestCase):
    """Case 2: Cascade on user delete removes the user's locations."""

    def test_locations_deleted_when_user_deleted(self):
        user = _make_user("cascade@test.local")
        loc1 = _make_location(user, name="Office")
        loc2 = _make_location(user, name="Home")
        loc_ids = [loc1.pk, loc2.pk]

        user.delete()

        remaining = UserLocation.objects.filter(pk__in=loc_ids).count()
        self.assertEqual(remaining, 0)


class UserLocationUniqueConstraintTests(TestCase):
    """Case 3: UniqueConstraint(user, name) rejects duplicate (user, name)."""

    def test_duplicate_name_same_user_raises(self):
        user = _make_user("unique@test.local")
        _make_location(user, name="Praha")

        with self.assertRaises(IntegrityError):
            _make_location(user, name="Praha")


class UserLocationCrossUserTests(TestCase):
    """Case 4: Two different users can both have a location named 'Praha'."""

    def test_same_name_allowed_for_different_users(self):
        user_a = _make_user("a@test.local")
        user_b = _make_user("b@test.local")

        loc_a = _make_location(user_a, name="Praha")
        loc_b = _make_location(user_b, name="Praha")

        self.assertIsNotNone(loc_a.pk)
        self.assertIsNotNone(loc_b.pk)
        self.assertNotEqual(loc_a.pk, loc_b.pk)


class UserLocationIsDefaultTests(TestCase):
    """Case 5: is_default defaults to False."""

    def test_is_default_is_false_by_default(self):
        user = _make_user("default@test.local")
        loc = _make_location(user)
        self.assertFalse(loc.is_default)

    def test_is_default_can_be_set_true(self):
        user = _make_user("default2@test.local")
        loc = _make_location(user, is_default=True)
        refreshed = UserLocation.objects.get(pk=loc.pk)
        self.assertTrue(refreshed.is_default)
