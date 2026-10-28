"""
Tests for slotly.find_slots MCP tool handler.

Fixture: alice (admin), bob, carol in one team. APIToken for alice with
scope "find_slots". All users have default working hours Mon-Fri 08:00-17:00.
Fixed deterministic window: 2026-07-06 (Mon) 08:00 CEST to 2026-07-10 (Fri) 17:00 CEST.
CEST = UTC+2, so those datetimes in UTC are 06:00 and 15:00 respectively.
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
from apps.search.models import RecentSearch
from apps.teams.models import Membership, Team

CEST = dt_timezone(timedelta(hours=2))
UTC = dt_timezone.utc

# Fixed window: 2026-07-06 Mon 08:00 CEST → 2026-07-10 Fri 17:00 CEST
WIN_START = "2026-07-06T08:00:00+02:00"
WIN_END = "2026-07-10T17:00:00+02:00"

# Mon 08:00–17:00 only
MON_START = "2026-07-06T08:00:00+02:00"
MON_END = "2026-07-06T17:00:00+02:00"


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class FindSlotsTests(APITestCase):

    def setUp(self):
        cache.clear()

        # Users
        self.alice = User.objects.create_user(email="alice@example.com", password="pass")
        self.bob = User.objects.create_user(email="bob@example.com", password="pass")
        self.carol = User.objects.create_user(email="carol@example.com", password="pass")
        self.dave = User.objects.create_user(email="dave@example.com", password="pass")  # NOT in team

        # Team: alice admin, bob and carol members
        self.team = Team.objects.create(name="Alpha", created_by=self.alice)
        Membership.objects.create(team=self.team, user=self.alice, role=Membership.Role.ADMIN)
        Membership.objects.create(team=self.team, user=self.bob, role=Membership.Role.MEMBER)
        Membership.objects.create(team=self.team, user=self.carol, role=Membership.Role.MEMBER)

        # APIToken for alice with find_slots scope
        self.token, self.raw = APIToken.create_for_user(self.alice, "T5-test", ["find_slots"])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _call(self, params: dict, token: str | None = None):
        raw = token or self.raw
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        return self.client.post(
            "/mcp/v1/tools/call",
            {"name": "slotly.find_slots", "params": params},
            format="json",
        )

    def _make_bob_calendar(self):
        return Calendar.objects.create(
            owner=self.bob,
            name="Bob's Cal",
            url_encrypted="x",
            include_in_busy=True,
        )

    # ------------------------------------------------------------------
    # 1. Happy path — all free
    # ------------------------------------------------------------------

    def test_find_slots_happy_path_all_free(self):
        """No busy events; 60-min slots fit within Mon 08:00-17:00."""
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["result"]
        self.assertIn("slots", data)
        self.assertIn("count", data)
        self.assertIn("truncated", data)
        self.assertGreater(data["count"], 0)
        self.assertFalse(data["truncated"])
        # Every slot must be exactly 60 min and within working hours
        for slot in data["slots"]:
            start = datetime.fromisoformat(slot["start"])
            end = datetime.fromisoformat(slot["end"])
            start_cest = start.astimezone(CEST)
            end_cest = end.astimezone(CEST)
            self.assertEqual((end - start).total_seconds(), 3600)
            self.assertGreaterEqual(start_cest.hour, 8)
            self.assertLessEqual(end_cest.hour, 17)

    # ------------------------------------------------------------------
    # 2. member_ids omitted → full team
    # ------------------------------------------------------------------

    def test_find_slots_defaults_to_all_members(self):
        """Omitting member_ids gives same result as passing all three."""
        base = {
            "team_id": self.team.pk,
            "duration_min": 60,
            "window_start": MON_START,
            "window_end": MON_END,
        }
        resp_all = self._call({**base, "member_ids": [self.alice.pk, self.bob.pk, self.carol.pk]})
        resp_omit = self._call(base)
        self.assertEqual(resp_all.status_code, 200)
        self.assertEqual(resp_omit.status_code, 200)
        result_all = resp_all.json()["result"]
        result_omit = resp_omit.json()["result"]
        self.assertEqual(result_all["count"], result_omit["count"])

    # ------------------------------------------------------------------
    # 3. Subset of members
    # ------------------------------------------------------------------

    def test_find_slots_subset_of_members(self):
        """Passing member_ids=[alice, bob] gives same count as full team (no events)."""
        # Full-team count
        resp_full = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp_full.status_code, 200)
        full_count = resp_full.json()["result"]["count"]

        # Subset count
        resp_sub = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "member_ids": [self.alice.pk, self.bob.pk],
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp_sub.status_code, 200)
        sub_count = resp_sub.json()["result"]["count"]

        self.assertGreater(sub_count, 0)
        self.assertEqual(sub_count, full_count)

    # ------------------------------------------------------------------
    # 4. duration_min too short
    # ------------------------------------------------------------------

    def test_find_slots_duration_too_short(self):
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 5,
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"], "validation_error")
        self.assertIn("duration_min", body["detail"])

    # ------------------------------------------------------------------
    # 5. duration_min too long
    # ------------------------------------------------------------------

    def test_find_slots_duration_too_long(self):
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 5000,
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"], "validation_error")
        self.assertIn("duration_min", body["detail"])

    # ------------------------------------------------------------------
    # 6. member_ids contains non-member
    # ------------------------------------------------------------------

    def test_find_slots_member_not_in_team(self):
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "member_ids": [self.dave.pk],
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"], "validation_error")
        self.assertIn("member_ids", body["detail"])
        detail_str = str(body["detail"])
        self.assertIn("not team members", detail_str)

    # ------------------------------------------------------------------
    # 7. member_ids duplicates
    # ------------------------------------------------------------------

    def test_find_slots_member_ids_duplicates(self):
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "member_ids": [self.alice.pk, self.alice.pk],
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"], "validation_error")
        self.assertIn("member_ids", body["detail"])
        detail_str = str(body["detail"])
        self.assertIn("unique", detail_str)

    # ------------------------------------------------------------------
    # 8. team_not_found
    # ------------------------------------------------------------------

    def test_find_slots_team_not_found(self):
        resp = self._call({
            "team_id": 99999,
            "duration_min": 60,
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"], "validation_error")
        self.assertIn("team_id", body["detail"])

    # ------------------------------------------------------------------
    # 9. Caller not a member → 403
    # ------------------------------------------------------------------

    def test_find_slots_non_member_caller(self):
        # dave has a find_slots token but is not in the team
        _, dave_raw = APIToken.create_for_user(self.dave, "dave-token", ["find_slots"])
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "window_start": MON_START,
            "window_end": MON_END,
        }, token=dave_raw)
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertEqual(body["error"], "forbidden")

    # ------------------------------------------------------------------
    # 10. window_start naive
    # ------------------------------------------------------------------

    def test_find_slots_window_naive(self):
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "window_start": "2026-07-06T08:00:00",  # no TZ
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"], "validation_error")
        self.assertIn("window_start", body["detail"])

    # ------------------------------------------------------------------
    # 11. window_start unparseable
    # ------------------------------------------------------------------

    def test_find_slots_window_unparseable(self):
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "window_start": "not a date",
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"], "validation_error")
        self.assertIn("window_start", body["detail"])

    # ------------------------------------------------------------------
    # 12. window too wide (> 60 days)
    # ------------------------------------------------------------------

    def test_find_slots_window_too_wide(self):
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "window_start": "2026-07-06T08:00:00+02:00",
            "window_end": "2026-09-10T17:00:00+02:00",  # 66 days
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"], "validation_error")
        self.assertIn("window_end", body["detail"])

    # ------------------------------------------------------------------
    # 13. window_end before window_start
    # ------------------------------------------------------------------

    def test_find_slots_window_end_before_start(self):
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "window_start": MON_END,
            "window_end": MON_START,
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"], "validation_error")
        self.assertIn("window_end", body["detail"])

    # ------------------------------------------------------------------
    # 14. Respects CalendarEvent busy (opaque)
    # ------------------------------------------------------------------

    def test_find_slots_respects_calendar_busy(self):
        """Bob has an OPAQUE event 10:00-12:00 Mon; no slot should overlap it."""
        cal = self._make_bob_calendar()
        # 10:00-12:00 CEST = 08:00-10:00 UTC
        dtstart = datetime(2026, 7, 6, 8, 0, 0, tzinfo=UTC)
        dtend = datetime(2026, 7, 6, 10, 0, 0, tzinfo=UTC)
        CalendarEvent.objects.create(
            calendar=cal,
            uid="busy-001",
            recurrence_id="",
            dtstart=dtstart,
            dtend=dtend,
            is_all_day=False,
            transp=CalendarEvent.Transparency.OPAQUE,
            status=CalendarEvent.Status.CONFIRMED,
        )
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "member_ids": [self.bob.pk],
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["result"]
        # No slot should overlap [10:00, 12:00) CEST
        busy_s = datetime(2026, 7, 6, 10, 0, 0, tzinfo=CEST)
        busy_e = datetime(2026, 7, 6, 12, 0, 0, tzinfo=CEST)
        for slot in data["slots"]:
            slot_s = datetime.fromisoformat(slot["start"])
            slot_e = datetime.fromisoformat(slot["end"])
            # slot and busy must not overlap
            self.assertTrue(
                slot_e <= busy_s or slot_s >= busy_e,
                f"Slot {slot['start']}-{slot['end']} overlaps busy 10:00-12:00",
            )

    # ------------------------------------------------------------------
    # 15. Respects Unavailability
    # ------------------------------------------------------------------

    def test_find_slots_respects_unavailability(self):
        """Bob has an all-day Unavailability on Monday; search Mon → count == 0 for bob."""
        # Monday all-day CEST: 00:00-24:00
        starts_at = datetime(2026, 7, 6, 0, 0, 0, tzinfo=CEST)
        ends_at = datetime(2026, 7, 7, 0, 0, 0, tzinfo=CEST)
        Unavailability.objects.create(
            user=self.bob,
            label="OOO",
            starts_at=starts_at,
            ends_at=ends_at,
            is_all_day=True,
        )
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "member_ids": [self.bob.pk],
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["result"]
        self.assertEqual(data["count"], 0)

    # ------------------------------------------------------------------
    # 16. buffer_min is applied
    # ------------------------------------------------------------------

    def test_find_slots_buffer_min_applied(self):
        """Bob busy 10:00-11:00 + buffer_min=15 → no slot overlaps 09:45-11:15."""
        cal = self._make_bob_calendar()
        dtstart = datetime(2026, 7, 6, 8, 0, 0, tzinfo=UTC)   # 10:00 CEST
        dtend = datetime(2026, 7, 6, 9, 0, 0, tzinfo=UTC)     # 11:00 CEST
        CalendarEvent.objects.create(
            calendar=cal,
            uid="busy-buf",
            recurrence_id="",
            dtstart=dtstart,
            dtend=dtend,
            is_all_day=False,
            transp=CalendarEvent.Transparency.OPAQUE,
            status=CalendarEvent.Status.CONFIRMED,
        )
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "member_ids": [self.bob.pk],
            "buffer_min": 15,
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["result"]
        # Buffered busy = 09:45–11:15 CEST
        buffered_s = datetime(2026, 7, 6, 9, 45, 0, tzinfo=CEST)
        buffered_e = datetime(2026, 7, 6, 11, 15, 0, tzinfo=CEST)
        for slot in data["slots"]:
            slot_s = datetime.fromisoformat(slot["start"])
            slot_e = datetime.fromisoformat(slot["end"])
            self.assertTrue(
                slot_e <= buffered_s or slot_s >= buffered_e,
                f"Slot {slot['start']}-{slot['end']} overlaps buffered busy",
            )

    # ------------------------------------------------------------------
    # 17. No RecentSearch created
    # ------------------------------------------------------------------

    def test_find_slots_no_recent_search_created(self):
        """MCP handler must NOT create a RecentSearch row."""
        self.assertEqual(RecentSearch.objects.count(), 0)
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(RecentSearch.objects.count(), 0)

    # ------------------------------------------------------------------
    # 18. truncated=True when limit hit
    # ------------------------------------------------------------------

    def test_find_slots_truncated_true_when_limit_hit(self):
        """15-min slots in a free Mon window with limit=4 → truncated=True, len==4."""
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 15,
            "limit": 4,
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["result"]
        self.assertTrue(data["truncated"])
        self.assertEqual(len(data["slots"]), 4)
        self.assertEqual(data["count"], 4)

    # ------------------------------------------------------------------
    # 19. MCPCall audit row written on success
    # ------------------------------------------------------------------

    def test_find_slots_mcpcall_audit_ok(self):
        """Successful call creates MCPCall with status="ok" and error_code=""."""
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 200)
        call = MCPCall.objects.filter(capability="slotly.find_slots", status="ok").first()
        self.assertIsNotNone(call)
        self.assertEqual(call.error_code, "")

    # ------------------------------------------------------------------
    # 20. Scope denied
    # ------------------------------------------------------------------

    def test_find_slots_scopes_denied(self):
        """Token with only list_teams scope → 403 {"error": "scope_denied"}."""
        _, restricted_raw = APIToken.create_for_user(self.alice, "restricted", ["list_teams"])
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "window_start": MON_START,
            "window_end": MON_END,
        }, token=restricted_raw)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json(), {"error": "scope_denied"})

    # ------------------------------------------------------------------
    # Bonus: window_end naive
    # ------------------------------------------------------------------

    def test_find_slots_window_end_naive(self):
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "window_start": MON_START,
            "window_end": "2026-07-06T17:00:00",  # no TZ
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"], "validation_error")
        self.assertIn("window_end", body["detail"])

    # ------------------------------------------------------------------
    # Bonus: window_end unparseable
    # ------------------------------------------------------------------

    def test_find_slots_window_end_unparseable(self):
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "window_start": MON_START,
            "window_end": "garbage",
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"], "validation_error")
        self.assertIn("window_end", body["detail"])

    # ------------------------------------------------------------------
    # Bonus: response shape — only slots/count/truncated, no working_hours_range
    # ------------------------------------------------------------------

    def test_find_slots_response_shape(self):
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 200)
        result = resp.json()["result"]
        self.assertEqual(set(result.keys()), {"slots", "count", "truncated"})
        self.assertNotIn("working_hours_range", result)

    # ------------------------------------------------------------------
    # Bonus: team_id not an integer
    # ------------------------------------------------------------------

    def test_find_slots_team_id_not_integer(self):
        resp = self._call({
            "team_id": "not-an-int",
            "duration_min": 60,
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"], "validation_error")
        self.assertIn("team_id", body["detail"])

    # ------------------------------------------------------------------
    # Fix-round: window boundary — exactly 60 days must be allowed
    # ------------------------------------------------------------------

    def test_find_slots_window_exactly_60_days_allowed(self):
        """A window of exactly 60 days must not be rejected."""
        from datetime import datetime, timezone as dt_timezone, timedelta as td
        start = datetime(2026, 7, 6, 8, 0, 0, tzinfo=CEST)
        end = start + td(days=60)
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
        })
        self.assertEqual(resp.status_code, 200)

    # ------------------------------------------------------------------
    # Fix-round: window boundary — 60 days + 1 second must be rejected
    # ------------------------------------------------------------------

    def test_find_slots_window_boundary_60_days_plus_one_second(self):
        """A window of 60 days + 1 second must be rejected with 400."""
        from datetime import datetime, timezone as dt_timezone, timedelta as td
        start = datetime(2026, 7, 6, 8, 0, 0, tzinfo=CEST)
        end = start + td(days=60, seconds=1)
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"], "validation_error")
        self.assertIn("window_end", body["detail"])

    # ------------------------------------------------------------------
    # Fix-round: buffer_min too large → 400
    # ------------------------------------------------------------------

    def test_find_slots_buffer_min_too_large(self):
        """buffer_min=300 (> 240 max) must return 400."""
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "buffer_min": 300,
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"], "validation_error")
        self.assertIn("buffer_min", body["detail"])

    # ------------------------------------------------------------------
    # Fix-round: buffer_min negative → 400
    # ------------------------------------------------------------------

    def test_find_slots_buffer_min_negative(self):
        """buffer_min=-1 must return 400."""
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "buffer_min": -1,
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"], "validation_error")
        self.assertIn("buffer_min", body["detail"])

    # ------------------------------------------------------------------
    # Fix-round: limit=0 → 400
    # ------------------------------------------------------------------

    def test_find_slots_limit_zero(self):
        """limit=0 (< 1 min) must return 400."""
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "limit": 0,
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"], "validation_error")
        self.assertIn("limit", body["detail"])

    # ------------------------------------------------------------------
    # Fix-round: limit too large → 400
    # ------------------------------------------------------------------

    def test_find_slots_limit_too_large(self):
        """limit=501 (> 500 max) must return 400."""
        resp = self._call({
            "team_id": self.team.pk,
            "duration_min": 60,
            "limit": 501,
            "window_start": MON_START,
            "window_end": MON_END,
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"], "validation_error")
        self.assertIn("limit", body["detail"])
