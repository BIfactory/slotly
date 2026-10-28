"""
Tests for slotly.check_time MCP tool handler.

Fixture: alice (admin), bob, carol in one team. APIToken for alice with
scope "check_time". Fixed deterministic window: 2026-07-06 (Mon) 10:00-11:00
Europe/Prague (CEST = UTC+2).

Privacy baseline (R9): response NEVER includes event title, calendar name,
or Unavailability label — CalendarEvent has no title column by design.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.availability.models import Unavailability
from apps.calendars.models import Calendar, CalendarEvent
from apps.mcp.models import APIToken, MCPCall
from apps.teams.models import Membership, Team

CEST = dt_timezone(timedelta(hours=2))
UTC = dt_timezone.utc

# Fixed window: 2026-07-06 Mon 10:00-11:00 CEST
WIN_START = "2026-07-06T10:00:00+02:00"
WIN_END = "2026-07-06T11:00:00+02:00"

DT_WIN_START = datetime(2026, 7, 6, 10, 0, 0, tzinfo=CEST)
DT_WIN_END = datetime(2026, 7, 6, 11, 0, 0, tzinfo=CEST)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class CheckTimeTests(APITestCase):

    def setUp(self):
        cache.clear()

        # Users
        self.alice = User.objects.create_user(
            email="alice@example.com", password="pass",
            first_name="Alice", last_name="A",
        )
        self.bob = User.objects.create_user(
            email="bob@example.com", password="pass",
            first_name="Bob", last_name="B",
        )
        self.carol = User.objects.create_user(
            email="carol@example.com", password="pass",
            first_name="Carol", last_name="C",
        )
        self.dave = User.objects.create_user(
            email="dave@example.com", password="pass",
        )  # NOT in team

        # Team: alice admin, bob and carol members
        self.team = Team.objects.create(name="Alpha", created_by=self.alice)
        Membership.objects.create(team=self.team, user=self.alice, role=Membership.Role.ADMIN)
        Membership.objects.create(team=self.team, user=self.bob, role=Membership.Role.MEMBER)
        Membership.objects.create(team=self.team, user=self.carol, role=Membership.Role.MEMBER)

        # APIToken for alice with check_time scope
        self.token, self.raw = APIToken.create_for_user(self.alice, "T6-test", ["check_time"])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _call(self, params: dict, token: str | None = None):
        raw = token or self.raw
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        return self.client.post(
            "/mcp/v1/tools/call",
            {"name": "slotly.check_time", "params": params},
            format="json",
        )

    def _make_bob_calendar(self, include_in_busy=True):
        return Calendar.objects.create(
            owner=self.bob,
            name="Bob's Cal",
            url_encrypted="x",
            include_in_busy=include_in_busy,
        )

    _evt_counter = 0

    def _make_event(self, calendar, dtstart, dtend, transp=CalendarEvent.Transparency.OPAQUE,
                    is_all_day=False, status=None):
        CheckTimeTests._evt_counter += 1
        kwargs = dict(
            calendar=calendar,
            uid=f"evt-test-{CheckTimeTests._evt_counter}",
            dtstart=dtstart,
            dtend=dtend,
            transp=transp,
            is_all_day=is_all_day,
        )
        if status:
            kwargs["status"] = status
        return CalendarEvent.objects.create(**kwargs)

    def _all_member_ids(self):
        return [self.alice.pk, self.bob.pk, self.carol.pk]

    # ------------------------------------------------------------------
    # 1. All free
    # ------------------------------------------------------------------

    def test_check_time_all_free(self):
        r = self._call({
            "team_id": self.team.pk,
            "member_ids": self._all_member_ids(),
            "start": WIN_START,
            "end": WIN_END,
        })
        self.assertEqual(r.status_code, 200, r.data)
        data = r.data["result"]
        self.assertTrue(data["available"])
        for m in data["members"]:
            self.assertEqual(m["status"], "free")
            self.assertEqual(m["conflicts"], [])

    # ------------------------------------------------------------------
    # 2. One blocked by OPAQUE event
    # ------------------------------------------------------------------

    def test_check_time_one_blocked_opaque(self):
        cal = self._make_bob_calendar()
        self._make_event(
            cal,
            dtstart=datetime(2026, 7, 6, 10, 15, 0, tzinfo=CEST),
            dtend=datetime(2026, 7, 6, 10, 45, 0, tzinfo=CEST),
        )
        r = self._call({
            "team_id": self.team.pk,
            "member_ids": self._all_member_ids(),
            "start": WIN_START,
            "end": WIN_END,
        })
        self.assertEqual(r.status_code, 200, r.data)
        data = r.data["result"]
        self.assertFalse(data["available"])

        bob_row = next(m for m in data["members"] if m["email"] == "bob@example.com")
        alice_row = next(m for m in data["members"] if m["email"] == "alice@example.com")
        carol_row = next(m for m in data["members"] if m["email"] == "carol@example.com")

        self.assertEqual(bob_row["status"], "busy")
        self.assertEqual(len(bob_row["conflicts"]), 1)
        # Timestamps may be in UTC or CEST depending on DB storage — parse and compare
        from django.utils.dateparse import parse_datetime
        c_start = parse_datetime(bob_row["conflicts"][0]["start"])
        c_end = parse_datetime(bob_row["conflicts"][0]["end"])
        self.assertEqual(c_start, datetime(2026, 7, 6, 10, 15, 0, tzinfo=CEST))
        self.assertEqual(c_end, datetime(2026, 7, 6, 10, 45, 0, tzinfo=CEST))

        self.assertEqual(alice_row["status"], "free")
        self.assertEqual(carol_row["status"], "free")

    # ------------------------------------------------------------------
    # 3. Blocked by all-day event
    # ------------------------------------------------------------------

    def test_check_time_blocked_by_all_day_event(self):
        cal = self._make_bob_calendar()
        # All-day event for the whole Monday; dtstart/dtend span the day
        self._make_event(
            cal,
            dtstart=datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC),
            dtend=datetime(2026, 7, 7, 0, 0, 0, tzinfo=UTC),
            is_all_day=True,
            transp=CalendarEvent.Transparency.TRANSPARENT,  # transp irrelevant — is_all_day wins
        )
        r = self._call({
            "team_id": self.team.pk,
            "member_ids": [self.bob.pk],
            "start": WIN_START,
            "end": WIN_END,
        })
        self.assertEqual(r.status_code, 200, r.data)
        data = r.data["result"]
        self.assertFalse(data["available"])
        bob_row = data["members"][0]
        self.assertEqual(bob_row["status"], "busy")
        # Conflicts must be clipped to [WIN_START, WIN_END]
        self.assertEqual(len(bob_row["conflicts"]), 1)
        # The conflict covers the whole window
        conflict = bob_row["conflicts"][0]
        from django.utils.dateparse import parse_datetime
        c_start = parse_datetime(conflict["start"])
        c_end = parse_datetime(conflict["end"])
        self.assertEqual(c_start, DT_WIN_START)
        self.assertEqual(c_end, DT_WIN_END)

    # ------------------------------------------------------------------
    # 4. Transparent (free) event ignored
    # ------------------------------------------------------------------

    def test_check_time_transparent_event_ignored(self):
        cal = self._make_bob_calendar()
        self._make_event(
            cal,
            dtstart=datetime(2026, 7, 6, 10, 15, 0, tzinfo=CEST),
            dtend=datetime(2026, 7, 6, 10, 45, 0, tzinfo=CEST),
            transp=CalendarEvent.Transparency.TRANSPARENT,
        )
        r = self._call({
            "team_id": self.team.pk,
            "member_ids": [self.bob.pk],
            "start": WIN_START,
            "end": WIN_END,
        })
        self.assertEqual(r.status_code, 200, r.data)
        data = r.data["result"]
        self.assertTrue(data["available"])
        self.assertEqual(data["members"][0]["status"], "free")

    # ------------------------------------------------------------------
    # 5. Cancelled event ignored
    # ------------------------------------------------------------------

    def test_check_time_cancelled_event_ignored(self):
        cal = self._make_bob_calendar()
        self._make_event(
            cal,
            dtstart=datetime(2026, 7, 6, 10, 15, 0, tzinfo=CEST),
            dtend=datetime(2026, 7, 6, 10, 45, 0, tzinfo=CEST),
            transp=CalendarEvent.Transparency.OPAQUE,
            status=CalendarEvent.Status.CANCELLED,
        )
        r = self._call({
            "team_id": self.team.pk,
            "member_ids": [self.bob.pk],
            "start": WIN_START,
            "end": WIN_END,
        })
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data["result"]["available"])
        self.assertEqual(r.data["result"]["members"][0]["status"], "free")

    # ------------------------------------------------------------------
    # 6. Calendar not include_in_busy → ignored
    # ------------------------------------------------------------------

    def test_check_time_calendar_not_in_busy(self):
        cal = self._make_bob_calendar(include_in_busy=False)
        self._make_event(
            cal,
            dtstart=datetime(2026, 7, 6, 10, 15, 0, tzinfo=CEST),
            dtend=datetime(2026, 7, 6, 10, 45, 0, tzinfo=CEST),
        )
        r = self._call({
            "team_id": self.team.pk,
            "member_ids": [self.bob.pk],
            "start": WIN_START,
            "end": WIN_END,
        })
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data["result"]["available"])
        self.assertEqual(r.data["result"]["members"][0]["status"], "free")

    # ------------------------------------------------------------------
    # 7. Unavailability block — no label in response
    # ------------------------------------------------------------------

    def test_check_time_unavailability_block(self):
        Unavailability.objects.create(
            user=self.bob,
            label="Sick",
            starts_at=datetime(2026, 7, 6, 10, 30, 0, tzinfo=CEST),
            ends_at=datetime(2026, 7, 6, 10, 50, 0, tzinfo=CEST),
        )
        r = self._call({
            "team_id": self.team.pk,
            "member_ids": [self.bob.pk],
            "start": WIN_START,
            "end": WIN_END,
        })
        self.assertEqual(r.status_code, 200, r.data)
        data = r.data["result"]
        self.assertFalse(data["available"])
        bob_row = data["members"][0]
        self.assertEqual(bob_row["status"], "busy")
        self.assertEqual(len(bob_row["conflicts"]), 1)
        from django.utils.dateparse import parse_datetime
        c_start = parse_datetime(bob_row["conflicts"][0]["start"])
        c_end = parse_datetime(bob_row["conflicts"][0]["end"])
        self.assertEqual(c_start, datetime(2026, 7, 6, 10, 30, 0, tzinfo=CEST))
        self.assertEqual(c_end, datetime(2026, 7, 6, 10, 50, 0, tzinfo=CEST))

        # Privacy guard: no "label" field anywhere in the response
        import json
        serialized = json.dumps(data)
        self.assertNotIn('"label"', serialized)
        self.assertNotIn("Sick", serialized)

    # ------------------------------------------------------------------
    # 8. Overlapping blocks merged
    # ------------------------------------------------------------------

    def test_check_time_overlapping_blocks_merged(self):
        cal = self._make_bob_calendar()
        self._make_event(
            cal,
            dtstart=datetime(2026, 7, 6, 10, 15, 0, tzinfo=CEST),
            dtend=datetime(2026, 7, 6, 10, 35, 0, tzinfo=CEST),
        )
        self._make_event(
            cal,
            dtstart=datetime(2026, 7, 6, 10, 30, 0, tzinfo=CEST),
            dtend=datetime(2026, 7, 6, 10, 50, 0, tzinfo=CEST),
        )
        r = self._call({
            "team_id": self.team.pk,
            "member_ids": [self.bob.pk],
            "start": WIN_START,
            "end": WIN_END,
        })
        self.assertEqual(r.status_code, 200, r.data)
        bob_row = r.data["result"]["members"][0]
        self.assertEqual(len(bob_row["conflicts"]), 1)
        from django.utils.dateparse import parse_datetime
        c_start = parse_datetime(bob_row["conflicts"][0]["start"])
        c_end = parse_datetime(bob_row["conflicts"][0]["end"])
        self.assertEqual(c_start, datetime(2026, 7, 6, 10, 15, 0, tzinfo=CEST))
        self.assertEqual(c_end, datetime(2026, 7, 6, 10, 50, 0, tzinfo=CEST))

    # ------------------------------------------------------------------
    # 9. Conflicts clipped to window
    # ------------------------------------------------------------------

    def test_check_time_conflicts_clipped_to_window(self):
        cal = self._make_bob_calendar()
        self._make_event(
            cal,
            dtstart=datetime(2026, 7, 6, 9, 30, 0, tzinfo=CEST),
            dtend=datetime(2026, 7, 6, 10, 30, 0, tzinfo=CEST),
        )
        r = self._call({
            "team_id": self.team.pk,
            "member_ids": [self.bob.pk],
            "start": WIN_START,
            "end": WIN_END,
        })
        self.assertEqual(r.status_code, 200, r.data)
        bob_row = r.data["result"]["members"][0]
        self.assertEqual(len(bob_row["conflicts"]), 1)
        conflict = bob_row["conflicts"][0]
        # Start clipped to window start; end clipped to event end
        from django.utils.dateparse import parse_datetime
        c_start = parse_datetime(conflict["start"])
        c_end = parse_datetime(conflict["end"])
        self.assertEqual(c_start, DT_WIN_START)
        self.assertEqual(c_end, datetime(2026, 7, 6, 10, 30, 0, tzinfo=CEST))

    # ------------------------------------------------------------------
    # 10. Non-member caller → 403
    # ------------------------------------------------------------------

    def test_check_time_non_member_caller(self):
        # dave token, not in team
        _, dave_raw = APIToken.create_for_user(self.dave, "dave-tok", ["check_time"])
        r = self._call({
            "team_id": self.team.pk,
            "member_ids": [self.alice.pk],
            "start": WIN_START,
            "end": WIN_END,
        }, token=dave_raw)
        self.assertEqual(r.status_code, 403, r.data)

    # ------------------------------------------------------------------
    # 11. member_ids contains non-team member → 400
    # ------------------------------------------------------------------

    def test_check_time_member_not_in_team(self):
        r = self._call({
            "team_id": self.team.pk,
            "member_ids": [self.dave.pk],
            "start": WIN_START,
            "end": WIN_END,
        })
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("member_ids", str(r.data))
        self.assertIn("not team members", str(r.data))

    # ------------------------------------------------------------------
    # 12. Duplicate member_ids → 400
    # ------------------------------------------------------------------

    def test_check_time_duplicates(self):
        r = self._call({
            "team_id": self.team.pk,
            "member_ids": [self.alice.pk, self.alice.pk],
            "start": WIN_START,
            "end": WIN_END,
        })
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("must be unique", str(r.data))

    # ------------------------------------------------------------------
    # 13. Missing member_ids → 400
    # ------------------------------------------------------------------

    def test_check_time_missing_member_ids(self):
        r = self._call({
            "team_id": self.team.pk,
            "start": WIN_START,
            "end": WIN_END,
        })
        self.assertEqual(r.status_code, 400, r.data)

    # ------------------------------------------------------------------
    # 14. Empty member_ids → 400
    # ------------------------------------------------------------------

    def test_check_time_empty_member_ids(self):
        r = self._call({
            "team_id": self.team.pk,
            "member_ids": [],
            "start": WIN_START,
            "end": WIN_END,
        })
        self.assertEqual(r.status_code, 400, r.data)

    # ------------------------------------------------------------------
    # 15. Naive start (no TZ) → 400
    # ------------------------------------------------------------------

    def test_check_time_naive_start(self):
        r = self._call({
            "team_id": self.team.pk,
            "member_ids": [self.alice.pk],
            "start": "2026-07-06T10:00:00",
            "end": WIN_END,
        })
        self.assertEqual(r.status_code, 400, r.data)

    # ------------------------------------------------------------------
    # 16. end <= start → 400
    # ------------------------------------------------------------------

    def test_check_time_end_before_start(self):
        r = self._call({
            "team_id": self.team.pk,
            "member_ids": [self.alice.pk],
            "start": WIN_END,
            "end": WIN_START,
        })
        self.assertEqual(r.status_code, 400, r.data)

    # ------------------------------------------------------------------
    # 17. Window too wide (>24h) → 400
    # ------------------------------------------------------------------

    def test_check_time_window_too_wide(self):
        r = self._call({
            "team_id": self.team.pk,
            "member_ids": [self.alice.pk],
            "start": WIN_START,
            "end": "2026-07-07T11:00:01+02:00",  # 25h01s
        })
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("24 hours", str(r.data))

    # ------------------------------------------------------------------
    # 18. Scope denied → 403
    # ------------------------------------------------------------------

    def test_check_time_scope_denied(self):
        _, wrong_raw = APIToken.create_for_user(self.alice, "wrong-scope", ["list_teams"])
        r = self._call({
            "team_id": self.team.pk,
            "member_ids": [self.alice.pk],
            "start": WIN_START,
            "end": WIN_END,
        }, token=wrong_raw)
        self.assertEqual(r.status_code, 403, r.data)
        self.assertEqual(r.data.get("error"), "scope_denied")

    # ------------------------------------------------------------------
    # 19. Response shape regression (privacy guard)
    # ------------------------------------------------------------------

    def test_check_time_response_shape(self):
        r = self._call({
            "team_id": self.team.pk,
            "member_ids": self._all_member_ids(),
            "start": WIN_START,
            "end": WIN_END,
        })
        self.assertEqual(r.status_code, 200, r.data)
        data = r.data["result"]

        # Top-level keys exactly
        self.assertEqual(set(data.keys()), {"team_id", "start", "end", "available", "members"})

        # Each member has exactly the allowed fields
        allowed_member_keys = {"user_id", "display_name", "email", "status", "conflicts"}
        for m in data["members"]:
            self.assertEqual(set(m.keys()), allowed_member_keys,
                             f"Unexpected keys in member: {set(m.keys()) - allowed_member_keys}")

        # Conflicts have exactly start/end
        for m in data["members"]:
            for c in m["conflicts"]:
                self.assertEqual(set(c.keys()), {"start", "end"})

        # No stray fields that could leak title or label
        import json
        serialized = json.dumps(data)
        for forbidden in ("title", "label", "summary", "description", "first_name", "last_name"):
            self.assertNotIn(f'"{forbidden}"', serialized,
                             f"Forbidden field '{forbidden}' found in response")

    # ------------------------------------------------------------------
    # 20. MCPCall audit record written on success
    # ------------------------------------------------------------------

    def test_check_time_mcpcall_audit_ok(self):
        before_count = MCPCall.objects.filter(capability="slotly.check_time").count()
        r = self._call({
            "team_id": self.team.pk,
            "member_ids": [self.alice.pk],
            "start": WIN_START,
            "end": WIN_END,
        })
        self.assertEqual(r.status_code, 200, r.data)
        after_count = MCPCall.objects.filter(capability="slotly.check_time").count()
        self.assertEqual(after_count, before_count + 1)
        call = MCPCall.objects.filter(capability="slotly.check_time").latest("created_at")
        self.assertEqual(call.status, "ok")
