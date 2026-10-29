"""Regression test for Task G4 — host_locations field on the public profile endpoint.

Coverage:
  - GET /api/public/profile/<token> includes a `host_locations` key.
  - The list is sorted: default row first, then alphabetically by name.
  - Each item carries id, name, address, is_default (no created_at / updated_at).
  - An empty list is returned when the host has no locations.
"""
from __future__ import annotations

import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.scheduling.models import UserLocation

User = get_user_model()


def _make_public_user(email: str) -> User:
    u = User.objects.create_user(email=email, password="testpass123")
    u.share_enabled = True
    u.share_token = uuid.uuid4()
    u.save(update_fields=["share_enabled", "share_token"])
    return u


class HostLocationsOnPublicProfileTest(TestCase):
    """host_locations is returned with the correct shape and sort order."""

    def setUp(self):
        self.client = APIClient()
        self.host = _make_public_user("host_locs@example.com")
        self.url = f"/api/public/profile/{self.host.share_token}"

    def test_host_locations_present_and_empty_when_none_defined(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("host_locations", resp.data)
        self.assertEqual(resp.data["host_locations"], [])

    def test_host_locations_shape_and_sort_order(self):
        """Default row comes first; remaining rows are alphabetical by name."""
        # Create three locations — two non-default, one default.
        UserLocation.objects.create(
            user=self.host,
            name="Zebra Café",
            address="Náměstí Míru 1, Praha 2",
            is_default=False,
        )
        UserLocation.objects.create(
            user=self.host,
            name="Office Karlín",
            address="Pernerova 1, Praha 8",
            is_default=True,
        )
        UserLocation.objects.create(
            user=self.host,
            name="Apple Bar",
            address="Dlouhá 22, Praha 1",
            is_default=False,
        )

        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)

        locs = resp.data["host_locations"]
        self.assertEqual(len(locs), 3)

        # Default row is first.
        self.assertTrue(locs[0]["is_default"])
        self.assertEqual(locs[0]["name"], "Office Karlín")
        self.assertEqual(locs[0]["address"], "Pernerova 1, Praha 8")
        self.assertIn("id", locs[0])

        # Remaining rows are alphabetical.
        self.assertEqual(locs[1]["name"], "Apple Bar")
        self.assertEqual(locs[2]["name"], "Zebra Café")

        # created_at / updated_at must NOT be present (not visitor-facing).
        for loc in locs:
            self.assertNotIn("created_at", loc)
            self.assertNotIn("updated_at", loc)
