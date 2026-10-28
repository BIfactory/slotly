"""
Tests for slotly.list_teams and slotly.list_members real handlers (T4).
"""
from __future__ import annotations

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.teams.models import Membership, Team

from .models import APIToken, MCPCall


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class ListTeamsTests(APITestCase):
    """Tests for slotly.list_teams real handler."""

    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user(
            email="alice@example.com", password="pass123",
            first_name="Alice", last_name="Doe",
        )
        self.bob = User.objects.create_user(
            email="bob@example.com", password="pass123",
            first_name="Bob", last_name="Smith",
        )
        self.team_a = Team.objects.create(name="Team A", description="Alpha team")
        self.team_b = Team.objects.create(name="Team B", description="Beta team")

        # alice is admin in team_a, bob is member of team_a
        Membership.objects.create(team=self.team_a, user=self.alice, role=Membership.Role.ADMIN)
        Membership.objects.create(team=self.team_a, user=self.bob, role=Membership.Role.MEMBER)
        # bob is in team_b, alice is NOT
        Membership.objects.create(team=self.team_b, user=self.bob, role=Membership.Role.MEMBER)

        self.token, self.raw = APIToken.create_for_user(
            self.alice, "T4-alice", ["list_teams", "list_members"]
        )

    def _call_list_teams(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        return self.client.post(
            "/mcp/v1/tools/call",
            {"name": "slotly.list_teams", "params": {}},
            format="json",
        )

    def test_list_teams_only_mine(self):
        """list_teams returns only teams alice is a member of (team_a), not team_b."""
        response = self._call_list_teams()
        self.assertEqual(response.status_code, 200)
        teams = response.json()["result"]["teams"]
        self.assertEqual(len(teams), 1)
        self.assertEqual(teams[0]["name"], "Team A")
        # team_b must not appear
        names = [t["name"] for t in teams]
        self.assertNotIn("Team B", names)

    def test_list_teams_member_count(self):
        """list_teams returns correct member_count (team_a has alice + bob = 2)."""
        response = self._call_list_teams()
        self.assertEqual(response.status_code, 200)
        teams = response.json()["result"]["teams"]
        self.assertEqual(len(teams), 1)
        self.assertEqual(teams[0]["member_count"], 2)

    def test_list_teams_sorted_by_name(self):
        """list_teams returns teams ordered alphabetically by name."""
        team_z = Team.objects.create(name="Team Z", description="Zulu")
        team_m = Team.objects.create(name="Team M", description="Mike")
        Membership.objects.create(team=team_z, user=self.alice, role=Membership.Role.MEMBER)
        Membership.objects.create(team=team_m, user=self.alice, role=Membership.Role.MEMBER)

        response = self._call_list_teams()
        self.assertEqual(response.status_code, 200)
        teams = response.json()["result"]["teams"]
        names = [t["name"] for t in teams]
        self.assertEqual(names, sorted(names))
        # Explicitly: A < M < Z
        self.assertEqual(names, ["Team A", "Team M", "Team Z"])

    def test_list_teams_result_shape(self):
        """Each team entry has id, name, description, member_count keys."""
        response = self._call_list_teams()
        self.assertEqual(response.status_code, 200)
        teams = response.json()["result"]["teams"]
        for team in teams:
            self.assertIn("id", team)
            self.assertIn("name", team)
            self.assertIn("description", team)
            self.assertIn("member_count", team)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class ListMembersTests(APITestCase):
    """Tests for slotly.list_members real handler."""

    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user(
            email="alice@example.com", password="pass123",
            first_name="Alice", last_name="Doe",
        )
        self.bob = User.objects.create_user(
            email="bob@example.com", password="pass123",
            first_name="Bob", last_name="Smith",
        )
        self.team_a = Team.objects.create(name="Team A", description="Alpha team")
        self.team_b = Team.objects.create(name="Team B", description="Beta team")

        # alice is admin in team_a, bob is member of team_a
        Membership.objects.create(team=self.team_a, user=self.alice, role=Membership.Role.ADMIN)
        Membership.objects.create(team=self.team_a, user=self.bob, role=Membership.Role.MEMBER)
        # bob only in team_b — alice is NOT a member
        Membership.objects.create(team=self.team_b, user=self.bob, role=Membership.Role.MEMBER)

        self.token, self.raw = APIToken.create_for_user(
            self.alice, "T4-alice", ["list_teams", "list_members"]
        )

    def _call_list_members(self, params):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        return self.client.post(
            "/mcp/v1/tools/call",
            {"name": "slotly.list_members", "params": params},
            format="json",
        )

    def test_list_members_happy_path(self):
        """list_members on team_a (alice is member) → 200, 2 members with correct keys."""
        response = self._call_list_members({"team_id": self.team_a.pk})
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertEqual(result["team_id"], self.team_a.pk)
        members = result["members"]
        self.assertEqual(len(members), 2)
        required_keys = {"id", "email", "display_name", "timezone", "working_hours", "role"}
        for m in members:
            self.assertEqual(set(m.keys()), required_keys)
            self.assertEqual(m["timezone"], "Europe/Prague")

    def test_list_members_display_name_fallback(self):
        """User with blank first/last name has display_name == email."""
        blank_user = User.objects.create_user(
            email="blank@example.com", password="pass123",
            first_name="", last_name="",
        )
        Membership.objects.create(team=self.team_a, user=blank_user, role=Membership.Role.MEMBER)

        response = self._call_list_members({"team_id": self.team_a.pk})
        self.assertEqual(response.status_code, 200)
        members = response.json()["result"]["members"]
        blank_entry = next(m for m in members if m["email"] == "blank@example.com")
        self.assertEqual(blank_entry["display_name"], "blank@example.com")

    def test_list_members_full_name(self):
        """User with first_name='Alice' and last_name='Doe' has display_name == 'Alice Doe'."""
        response = self._call_list_members({"team_id": self.team_a.pk})
        self.assertEqual(response.status_code, 200)
        members = response.json()["result"]["members"]
        alice_entry = next(m for m in members if m["email"] == "alice@example.com")
        self.assertEqual(alice_entry["display_name"], "Alice Doe")

    def test_list_members_not_a_member(self):
        """Alice calls list_members on team_b (not a member) → 403 with correct body."""
        response = self._call_list_members({"team_id": self.team_b.pk})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"error": "forbidden", "detail": {"team_id": "you are not a member of this team"}},
        )
        # MCPCall audit row must have error_code="forbidden"
        call = MCPCall.objects.filter(
            capability="slotly.list_members", error_code="forbidden"
        ).first()
        self.assertIsNotNone(call)
        self.assertEqual(call.status, "error")

    def test_list_members_team_not_found(self):
        """team_id that doesn't exist → 400 with correct body."""
        response = self._call_list_members({"team_id": 999999})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {"error": "validation_error", "detail": {"team_id": "team not found"}},
        )

    def test_list_members_missing_team_id(self):
        """Empty params (no team_id) → 400 with correct body."""
        response = self._call_list_members({})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {"error": "validation_error", "detail": {"team_id": "must be an integer"}},
        )

    def test_list_members_members_sorted(self):
        """Members are returned sorted by first_name ASC."""
        # alice (first_name="Alice"), bob (first_name="Bob")
        response = self._call_list_members({"team_id": self.team_a.pk})
        self.assertEqual(response.status_code, 200)
        members = response.json()["result"]["members"]
        first_names = [m["display_name"].split()[0] for m in members]
        self.assertEqual(first_names, sorted(first_names))
        # Explicitly: Alice before Bob
        self.assertEqual(members[0]["email"], "alice@example.com")
        self.assertEqual(members[1]["email"], "bob@example.com")

    def test_list_members_timezone_is_settings_value(self):
        """timezone field equals settings.TIME_ZONE ('Europe/Prague') for every member."""
        response = self._call_list_members({"team_id": self.team_a.pk})
        self.assertEqual(response.status_code, 200)
        members = response.json()["result"]["members"]
        for m in members:
            self.assertEqual(m["timezone"], "Europe/Prague")

    def test_list_members_role_values(self):
        """role field reflects actual membership role (admin/member)."""
        response = self._call_list_members({"team_id": self.team_a.pk})
        self.assertEqual(response.status_code, 200)
        members = response.json()["result"]["members"]
        alice_entry = next(m for m in members if m["email"] == "alice@example.com")
        bob_entry = next(m for m in members if m["email"] == "bob@example.com")
        self.assertEqual(alice_entry["role"], "admin")
        self.assertEqual(bob_entry["role"], "member")

    def test_list_members_team_id_not_int(self):
        """Non-integer team_id (e.g. None via missing key) → 400 validation_error."""
        # Simulate missing team_id by passing None explicitly
        response = self._call_list_members({"team_id": None})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {"error": "validation_error", "detail": {"team_id": "must be an integer"}},
        )
