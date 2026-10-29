"""
TDD tests for Task G2 — /api/me/locations CRUD endpoints.

Coverage:
  1.  GET /api/me/locations — lists locations ordered by is_default desc, name asc.
  2.  POST /api/me/locations — creates a location; returns 201 + serialized row.
  3.  POST /api/me/locations — 20-cap: 21st create returns 400 with expected detail.
  4.  POST /api/me/locations — duplicate name returns 400 with DRF-style field error.
  5.  POST /api/me/locations — is_default=True unsets siblings atomically.
  6.  PATCH /api/me/locations/<id> — partial update (name + address + is_default).
  7.  PATCH /api/me/locations/<id> — duplicate name on patch returns 400.
  8.  PATCH /api/me/locations/<id> — is_default=True unsets siblings.
  9.  DELETE /api/me/locations/<id> — removes the row; returns 204.
  10. Cross-user isolation: other user's location returns 404 on PATCH/DELETE/set-default.
  11. POST /api/me/locations/<id>/set-default — marks target as default; clears siblings.
  12. Unauthenticated requests return 401/403.
  13. GET ordering: default first, then alphabetical.
  14. Blank name/address rejected.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import User
from .models import UserLocation


def _make_user(email: str = "user@test.local") -> User:
    return User.objects.create_user(email=email, password="pw-test-123")


def _make_location(user: User, name: str = "Office", **kwargs) -> UserLocation:
    defaults = dict(user=user, name=name, address="Pernerova 1, Praha 8")
    defaults.update(kwargs)
    return UserLocation.objects.create(**defaults)


LIST_URL = "/api/me/locations"


def _detail_url(pk: int) -> str:
    return f"/api/me/locations/{pk}"


def _set_default_url(pk: int) -> str:
    return f"/api/me/locations/{pk}/set-default"


class LocationListTests(TestCase):
    """Case 1 + 13: GET /api/me/locations."""

    def setUp(self):
        self.user = _make_user()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_empty_list(self):
        resp = self.client.get(LIST_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_list_returns_own_locations(self):
        _make_location(self.user, "Brno")
        _make_location(self.user, "Praha")
        resp = self.client.get(LIST_URL)
        self.assertEqual(resp.status_code, 200)
        names = [r["name"] for r in resp.json()]
        self.assertIn("Brno", names)
        self.assertIn("Praha", names)
        self.assertEqual(len(names), 2)

    def test_ordering_default_first_then_alphabetical(self):
        _make_location(self.user, "Zlín")
        _make_location(self.user, "Brno", is_default=True)
        _make_location(self.user, "Praha")
        resp = self.client.get(LIST_URL)
        names = [r["name"] for r in resp.json()]
        # Default should come first
        self.assertEqual(names[0], "Brno")
        # Then alphabetical
        self.assertEqual(names[1], "Praha")
        self.assertEqual(names[2], "Zlín")

    def test_cross_user_isolation_on_list(self):
        other = _make_user("other@test.local")
        _make_location(other, "Other's office")
        resp = self.client.get(LIST_URL)
        self.assertEqual(resp.json(), [])

    def test_serializer_shape(self):
        loc = _make_location(self.user, "Test Location")
        resp = self.client.get(LIST_URL)
        row = resp.json()[0]
        self.assertIn("id", row)
        self.assertIn("name", row)
        self.assertIn("address", row)
        self.assertIn("is_default", row)
        self.assertIn("created_at", row)
        self.assertIn("updated_at", row)
        self.assertEqual(row["id"], loc.pk)
        self.assertEqual(row["name"], "Test Location")


class LocationCreateTests(TestCase):
    """Cases 2, 3, 4, 5: POST /api/me/locations."""

    def setUp(self):
        self.user = _make_user()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_create_basic(self):
        resp = self.client.post(
            LIST_URL,
            {"name": "Home", "address": "Vinohradská 42, Praha 2"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["name"], "Home")
        self.assertEqual(data["address"], "Vinohradská 42, Praha 2")
        self.assertFalse(data["is_default"])
        self.assertIn("id", data)
        self.assertIn("created_at", data)
        self.assertIn("updated_at", data)

    def test_create_with_is_default_true(self):
        resp = self.client.post(
            LIST_URL,
            {"name": "Main Office", "address": "Náměstí Míru 1", "is_default": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.json()["is_default"])

    def test_create_20_cap_exactly_20_allowed(self):
        for i in range(20):
            UserLocation.objects.create(
                user=self.user,
                name=f"Location {i}",
                address="Addr",
            )
        # 20 already exist — a 21st must be rejected
        resp = self.client.post(
            LIST_URL,
            {"name": "One too many", "address": "Addr"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("You can have at most 20 locations.", resp.json()["detail"])

    def test_create_exactly_19_then_20_succeeds(self):
        for i in range(19):
            UserLocation.objects.create(user=self.user, name=f"Loc {i}", address="A")
        resp = self.client.post(
            LIST_URL, {"name": "Twentieth", "address": "A"}, format="json"
        )
        self.assertEqual(resp.status_code, 201)

    def test_create_duplicate_name_returns_400(self):
        _make_location(self.user, "Office")
        resp = self.client.post(
            LIST_URL, {"name": "Office", "address": "Somewhere"}, format="json"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("name", resp.json())
        self.assertIn("A location with this name already exists.", resp.json()["name"])

    def test_create_is_default_unsets_siblings(self):
        existing = _make_location(self.user, "Old default", is_default=True)
        self.client.post(
            LIST_URL,
            {"name": "New default", "address": "Addr", "is_default": True},
            format="json",
        )
        existing.refresh_from_db()
        self.assertFalse(existing.is_default)
        # Exactly one location should now be default
        defaults = UserLocation.objects.filter(user=self.user, is_default=True)
        self.assertEqual(defaults.count(), 1)
        self.assertEqual(defaults.first().name, "New default")

    def test_create_blank_name_rejected(self):
        resp = self.client.post(
            LIST_URL, {"name": "  ", "address": "Addr"}, format="json"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("name", resp.json())

    def test_create_blank_address_rejected(self):
        resp = self.client.post(
            LIST_URL, {"name": "Valid Name", "address": "  "}, format="json"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("address", resp.json())

    def test_create_missing_name_rejected(self):
        resp = self.client.post(
            LIST_URL, {"address": "Somewhere"}, format="json"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("name", resp.json())

    def test_create_missing_address_rejected(self):
        resp = self.client.post(
            LIST_URL, {"name": "Valid"}, format="json"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("address", resp.json())

    def test_create_is_default_defaults_to_false(self):
        resp = self.client.post(
            LIST_URL, {"name": "No default flag", "address": "A"}, format="json"
        )
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(resp.json()["is_default"])

    def test_create_duplicate_allowed_for_different_user(self):
        other = _make_user("other@test.local")
        _make_location(other, "Office")
        resp = self.client.post(
            LIST_URL, {"name": "Office", "address": "My addr"}, format="json"
        )
        self.assertEqual(resp.status_code, 201)


class LocationUpdateTests(TestCase):
    """Cases 6, 7, 8: PATCH /api/me/locations/<id>."""

    def setUp(self):
        self.user = _make_user()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.loc = _make_location(self.user, "Office")

    def test_patch_name(self):
        resp = self.client.patch(
            _detail_url(self.loc.pk), {"name": "Home Office"}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "Home Office")
        self.loc.refresh_from_db()
        self.assertEqual(self.loc.name, "Home Office")

    def test_patch_address(self):
        resp = self.client.patch(
            _detail_url(self.loc.pk), {"address": "New Address 5"}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["address"], "New Address 5")

    def test_patch_is_default_true_unsets_siblings(self):
        sibling = _make_location(self.user, "Sibling", is_default=True)
        resp = self.client.patch(
            _detail_url(self.loc.pk), {"is_default": True}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        sibling.refresh_from_db()
        self.assertFalse(sibling.is_default)
        self.loc.refresh_from_db()
        self.assertTrue(self.loc.is_default)

    def test_patch_duplicate_name_returns_400(self):
        other = _make_location(self.user, "Other Location")
        resp = self.client.patch(
            _detail_url(self.loc.pk), {"name": "Other Location"}, format="json"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("name", resp.json())

    def test_patch_same_name_allowed(self):
        """Patching with the same name (no rename) should succeed."""
        resp = self.client.patch(
            _detail_url(self.loc.pk),
            {"name": self.loc.name, "address": "New Addr"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

    def test_patch_blank_name_rejected(self):
        resp = self.client.patch(
            _detail_url(self.loc.pk), {"name": ""}, format="json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_patch_nonexistent_returns_404(self):
        resp = self.client.patch(
            _detail_url(9999), {"name": "X"}, format="json"
        )
        self.assertEqual(resp.status_code, 404)

    def test_patch_cross_user_returns_404(self):
        other = _make_user("other@test.local")
        other_loc = _make_location(other, "Other's place")
        resp = self.client.patch(
            _detail_url(other_loc.pk), {"name": "Hijacked"}, format="json"
        )
        self.assertEqual(resp.status_code, 404)

    def test_patch_does_not_count_against_cap(self):
        """PATCH on existing row must not be blocked by the 20-cap."""
        for i in range(19):  # total 20 including self.loc
            UserLocation.objects.create(user=self.user, name=f"Loc {i}", address="A")
        resp = self.client.patch(
            _detail_url(self.loc.pk), {"address": "Updated Addr"}, format="json"
        )
        self.assertEqual(resp.status_code, 200)

    def test_patch_returns_updated_serializer_shape(self):
        resp = self.client.patch(
            _detail_url(self.loc.pk), {"address": "Updated"}, format="json"
        )
        data = resp.json()
        self.assertIn("id", data)
        self.assertIn("name", data)
        self.assertIn("address", data)
        self.assertIn("is_default", data)
        self.assertIn("created_at", data)
        self.assertIn("updated_at", data)


class LocationDeleteTests(TestCase):
    """Case 9: DELETE /api/me/locations/<id>."""

    def setUp(self):
        self.user = _make_user()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_delete_own_location(self):
        loc = _make_location(self.user)
        resp = self.client.delete(_detail_url(loc.pk))
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(UserLocation.objects.filter(pk=loc.pk).exists())

    def test_delete_nonexistent_returns_404(self):
        resp = self.client.delete(_detail_url(9999))
        self.assertEqual(resp.status_code, 404)

    def test_delete_cross_user_returns_404(self):
        other = _make_user("other@test.local")
        other_loc = _make_location(other, "Other's place")
        resp = self.client.delete(_detail_url(other_loc.pk))
        self.assertEqual(resp.status_code, 404)
        # Row must still exist
        self.assertTrue(UserLocation.objects.filter(pk=other_loc.pk).exists())


class LocationSetDefaultTests(TestCase):
    """Case 11: POST /api/me/locations/<id>/set-default."""

    def setUp(self):
        self.user = _make_user()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_set_default_marks_target(self):
        loc = _make_location(self.user, "Office")
        resp = self.client.post(_set_default_url(loc.pk), format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["is_default"])
        loc.refresh_from_db()
        self.assertTrue(loc.is_default)

    def test_set_default_unsets_siblings(self):
        loc1 = _make_location(self.user, "Alpha", is_default=True)
        loc2 = _make_location(self.user, "Beta")
        resp = self.client.post(_set_default_url(loc2.pk), format="json")
        self.assertEqual(resp.status_code, 200)
        loc1.refresh_from_db()
        loc2.refresh_from_db()
        self.assertFalse(loc1.is_default)
        self.assertTrue(loc2.is_default)

    def test_set_default_atomicity(self):
        """After set-default, exactly one row is the default."""
        locs = [_make_location(self.user, f"Loc {i}", is_default=(i == 0)) for i in range(5)]
        target = locs[3]
        self.client.post(_set_default_url(target.pk), format="json")
        defaults = UserLocation.objects.filter(user=self.user, is_default=True)
        self.assertEqual(defaults.count(), 1)
        self.assertEqual(defaults.first().pk, target.pk)

    def test_set_default_returns_serialized_row(self):
        loc = _make_location(self.user)
        resp = self.client.post(_set_default_url(loc.pk), format="json")
        data = resp.json()
        self.assertIn("id", data)
        self.assertIn("name", data)
        self.assertIn("is_default", data)
        self.assertTrue(data["is_default"])

    def test_set_default_nonexistent_returns_404(self):
        resp = self.client.post(_set_default_url(9999), format="json")
        self.assertEqual(resp.status_code, 404)

    def test_set_default_cross_user_returns_404(self):
        other = _make_user("other@test.local")
        other_loc = _make_location(other, "Other's default")
        resp = self.client.post(_set_default_url(other_loc.pk), format="json")
        self.assertEqual(resp.status_code, 404)
        # Row must not have been changed
        other_loc.refresh_from_db()
        self.assertFalse(other_loc.is_default)

    def test_set_default_already_default_is_idempotent(self):
        loc = _make_location(self.user, "Already default", is_default=True)
        resp = self.client.post(_set_default_url(loc.pk), format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["is_default"])


class LocationAuthTests(TestCase):
    """Case 12: Unauthenticated requests are rejected."""

    def setUp(self):
        self.client = APIClient()
        self.user = _make_user("auth@test.local")
        self.loc = _make_location(self.user)

    def test_list_unauthenticated(self):
        resp = self.client.get(LIST_URL)
        self.assertIn(resp.status_code, [401, 403])

    def test_create_unauthenticated(self):
        resp = self.client.post(
            LIST_URL, {"name": "X", "address": "Y"}, format="json"
        )
        self.assertIn(resp.status_code, [401, 403])

    def test_patch_unauthenticated(self):
        resp = self.client.patch(
            _detail_url(self.loc.pk), {"name": "X"}, format="json"
        )
        self.assertIn(resp.status_code, [401, 403])

    def test_delete_unauthenticated(self):
        resp = self.client.delete(_detail_url(self.loc.pk))
        self.assertIn(resp.status_code, [401, 403])

    def test_set_default_unauthenticated(self):
        resp = self.client.post(_set_default_url(self.loc.pk), format="json")
        self.assertIn(resp.status_code, [401, 403])
