"""
TDD tests for Task G5 — MeetingType.default_location FK.

Coverage:
  R3  — Deleting a UserLocation that is referenced as default_location sets
        the FK to NULL but leaves MeetingType.location (string) intact.
  XU  — Cross-user FK rejection: User B supplying User A's location ID in
        POST /api/meeting-types returns 400 {"default_location_id": [...]}.
  RT  — Round-trip: create a MeetingType with default_location_id, GET it
        back, confirm the response carries default_location {id, name, address}.
"""

from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from .models import MeetingType, UserLocation


def _make_user(email: str) -> User:
    return User.objects.create_user(email=email, password="pw-test-g5")


def _make_location(user: User, name: str = "Café", address: str = "Náměstí Míru 1, Praha 2") -> UserLocation:
    return UserLocation.objects.create(user=user, name=name, address=address)


def _make_meeting_type(host: User, **kwargs) -> MeetingType:
    defaults = dict(
        host=host,
        name="Quick chat",
        slug="quick-chat",
        duration_min=30,
        kind="physical",
        location="Fallback address",
    )
    defaults.update(kwargs)
    return MeetingType.objects.create(**defaults)


class R3SetNullOnLocationDeleteTest(TestCase):
    """R3: Deleting the referenced UserLocation NULLs the FK, string stays."""

    def test_delete_location_nulls_fk_and_preserves_string(self):
        user = _make_user("r3@test.local")
        loc = _make_location(user, name="Meeting Room A", address="Wenceslas Sq 1")
        mt = _make_meeting_type(user, default_location=loc, location="Wenceslas Sq 1")

        # Sanity-check setup.
        self.assertEqual(mt.default_location_id, loc.pk)
        self.assertEqual(mt.location, "Wenceslas Sq 1")

        # Delete the location.
        loc.delete()

        mt.refresh_from_db()
        self.assertIsNone(mt.default_location_id, "FK must become NULL after location deleted")
        self.assertEqual(mt.location, "Wenceslas Sq 1", "free-form location string must survive")

    def test_delete_location_does_not_delete_meeting_type(self):
        user = _make_user("r3b@test.local")
        loc = _make_location(user, name="Room B", address="Addr B")
        mt = _make_meeting_type(user, default_location=loc)

        loc.delete()

        # MeetingType should still exist.
        self.assertTrue(MeetingType.objects.filter(pk=mt.pk).exists())


LIST_URL = "/api/meeting-types"


class CrossUserRejectionTest(TestCase):
    """Cross-user: User B's FK pointing at User A's location → 400."""

    def setUp(self):
        self.user_a = _make_user("a@test.local")
        self.user_b = _make_user("b@test.local")
        self.loc_a = _make_location(self.user_a, name="Loc A", address="Addr A")
        self.client_b = APIClient()
        self.client_b.force_authenticate(user=self.user_b)

    def test_post_with_other_users_location_id_returns_400(self):
        payload = {
            "name": "Meeting type",
            "duration_min": 30,
            "default_location_id": self.loc_a.pk,
        }
        resp = self.client_b.post(LIST_URL, payload, format="json")
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertIn("default_location_id", data, f"Expected field error, got: {data}")

    def test_patch_with_other_users_location_id_returns_400(self):
        mt = _make_meeting_type(self.user_b, slug="mt-b")
        resp = self.client_b.patch(
            f"{LIST_URL}/{mt.pk}",
            {"default_location_id": self.loc_a.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertIn("default_location_id", data, f"Expected field error, got: {data}")

    def test_nonexistent_location_id_returns_400(self):
        payload = {
            "name": "Another type",
            "duration_min": 30,
            "default_location_id": 99999,
        }
        resp = self.client_b.post(LIST_URL, payload, format="json")
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertIn("default_location_id", data)


class RoundTripTest(TestCase):
    """Round-trip: create → GET → confirm default_location nested object."""

    def setUp(self):
        self.user = _make_user("rt@test.local")
        self.loc = _make_location(self.user, name="My Office", address="Korunní 5, Praha 2")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_create_with_default_location_id_and_get_back(self):
        payload = {
            "name": "Deep dive",
            "duration_min": 60,
            "kind": "physical",
            "default_location_id": self.loc.pk,
        }
        create_resp = self.client.post(LIST_URL, payload, format="json")
        self.assertEqual(create_resp.status_code, 201, create_resp.json())
        data = create_resp.json()

        # Inline response carries the nested object.
        self.assertEqual(data["default_location_id"], self.loc.pk)
        self.assertIsNotNone(data["default_location"])
        self.assertEqual(data["default_location"]["id"], self.loc.pk)
        self.assertEqual(data["default_location"]["name"], self.loc.name)
        self.assertEqual(data["default_location"]["address"], self.loc.address)

    def test_get_list_carries_default_location(self):
        mt = _make_meeting_type(self.user, slug="deep-dive-2", default_location=self.loc)

        resp = self.client.get(LIST_URL)
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["types"]
        item = next((x for x in items if x["id"] == mt.pk), None)
        self.assertIsNotNone(item)
        self.assertEqual(item["default_location_id"], self.loc.pk)
        self.assertIsNotNone(item["default_location"])
        self.assertEqual(item["default_location"]["name"], self.loc.name)

    def test_patch_clears_default_location_with_null(self):
        mt = _make_meeting_type(self.user, slug="nullable-test", default_location=self.loc)

        resp = self.client.patch(
            f"{LIST_URL}/{mt.pk}",
            {"default_location_id": None},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.json())
        data = resp.json()
        self.assertIsNone(data["default_location_id"])
        self.assertIsNone(data["default_location"])

    def test_create_without_default_location_returns_null(self):
        payload = {
            "name": "Online meeting",
            "duration_min": 30,
        }
        resp = self.client.post(LIST_URL, payload, format="json")
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertIsNone(data["default_location_id"])
        self.assertIsNone(data["default_location"])
