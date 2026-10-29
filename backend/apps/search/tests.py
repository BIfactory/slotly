"""Pure-function tests for the slot-search engine. No DB."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from .engine import compute_slots

# Pick a window mid-July: Europe/Prague is on CEST (UTC+2) the whole time, so
# tests don't have to reason about DST transitions.
TZ = ZoneInfo("Europe/Prague")
W_START = datetime(2026, 7, 6, 0, 0, tzinfo=TZ).astimezone(timezone.utc)  # Mon
W_END = datetime(2026, 7, 13, 0, 0, tzinfo=TZ).astimezone(timezone.utc)  # next Mon


def _local(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


def _wh_default():
    """Mon-Fri 08:00-17:00, weekend off."""
    weekday = {"start": "08:00", "end": "17:00", "available": True}
    weekend = {"start": "08:00", "end": "17:00", "available": False}
    return {
        "monday": weekday,
        "tuesday": weekday,
        "wednesday": weekday,
        "thursday": weekday,
        "friday": weekday,
        "saturday": weekend,
        "sunday": weekend,
    }


class EngineTests(SimpleTestCase):
    # 1) two users, identical 9-5, no events, single weekday → expect a full
    #    grid of 60-min slots from 08:00 to 16:00 in 15-min steps.
    def test_two_free_users_yield_full_grid_one_day(self) -> None:
        one_day_end = _local(2026, 7, 7, 0, 0).astimezone(timezone.utc)
        slots, truncated = compute_slots(
            working_hours_per_user={1: _wh_default(), 2: _wh_default()},
            busy_per_user={1: [], 2: []},
            user_ids=[1, 2],
            window_start=W_START,
            window_end=one_day_end,
            duration=timedelta(hours=1),
            tz=TZ,
        )
        self.assertFalse(truncated)
        # 8 hours × 4 = 33 starts (08:00..16:00 inclusive at 15-min step).
        self.assertEqual(len(slots), 33)
        self.assertEqual(slots[0].start, _local(2026, 7, 6, 8, 0))
        self.assertEqual(slots[0].end, _local(2026, 7, 6, 9, 0))
        self.assertEqual(slots[-1].start, _local(2026, 7, 6, 16, 0))

    # 1b) the result is capped at max_results and `truncated` flips on.
    def test_truncation_caps_results(self) -> None:
        slots, truncated = compute_slots(
            working_hours_per_user={1: _wh_default()},
            busy_per_user={1: []},
            user_ids=[1],
            window_start=W_START,
            window_end=W_END,
            duration=timedelta(hours=1),
            tz=TZ,
            max_results=10,
        )
        self.assertTrue(truncated)
        self.assertEqual(len(slots), 10)

    # 2) one user has a busy event in the middle of Monday → slots overlapping
    #    that interval disappear, slots before/after remain.
    def test_busy_blocks_only_overlapping_slots(self) -> None:
        busy = [(_local(2026, 7, 6, 10, 0), _local(2026, 7, 6, 11, 0))]
        slots, _ = compute_slots(
            working_hours_per_user={1: _wh_default()},
            busy_per_user={1: busy},
            user_ids=[1],
            window_start=W_START,
            window_end=W_END,
            duration=timedelta(hours=1),
            tz=TZ,
        )
        starts_mon = [s.start for s in slots if s.start.date() == _local(2026, 7, 6, 0).date()]
        # 09:00 starts a slot ending 10:00 (touches but does not overlap → allowed)
        self.assertIn(_local(2026, 7, 6, 9, 0), starts_mon)
        # 09:15..10:45 starts overlap the busy 10:00-11:00 → excluded
        self.assertNotIn(_local(2026, 7, 6, 9, 15), starts_mon)
        self.assertNotIn(_local(2026, 7, 6, 10, 0), starts_mon)
        self.assertNotIn(_local(2026, 7, 6, 10, 30), starts_mon)
        # 11:00 starts a slot ending 12:00 (after busy) → allowed
        self.assertIn(_local(2026, 7, 6, 11, 0), starts_mon)

    # 3) buffer expands the busy interval on both sides.
    def test_buffer_extends_busy_interval(self) -> None:
        busy = [(_local(2026, 7, 6, 10, 0), _local(2026, 7, 6, 11, 0))]
        slots, _ = compute_slots(
            working_hours_per_user={1: _wh_default()},
            busy_per_user={1: busy},
            user_ids=[1],
            window_start=W_START,
            window_end=W_END,
            duration=timedelta(hours=1),
            buffer=timedelta(minutes=30),
            tz=TZ,
        )
        starts_mon = [s.start for s in slots if s.start.date() == _local(2026, 7, 6, 0).date()]
        # With 30-min buffer on each side, busy is effectively 09:30-11:30.
        # Slots that end after 09:30 and start before 11:30 must be excluded.
        self.assertNotIn(_local(2026, 7, 6, 9, 0), starts_mon)  # ends 10:00 > 09:30
        self.assertIn(_local(2026, 7, 6, 8, 30), starts_mon)    # ends 09:30 == buffer start, allowed
        self.assertIn(_local(2026, 7, 6, 11, 30), starts_mon)   # starts at buffer end, allowed
        self.assertNotIn(_local(2026, 7, 6, 11, 15), starts_mon)

    # 4) day flagged unavailable → no slots that day.
    def test_unavailable_day_yields_no_slots(self) -> None:
        wh = _wh_default()
        wh["wednesday"] = {"start": "08:00", "end": "17:00", "available": False}
        slots, _ = compute_slots(
            working_hours_per_user={1: wh},
            busy_per_user={1: []},
            user_ids=[1],
            window_start=W_START,
            window_end=W_END,
            duration=timedelta(hours=1),
            tz=TZ,
        )
        wednesdays = [s for s in slots if s.start.date() == _local(2026, 7, 8, 0).date()]
        self.assertEqual(wednesdays, [])

    # 5) user with empty busy list is treated as fully available within their
    #    working hours (PRD §5.4).
    def test_no_calendar_user_constrained_only_by_working_hours(self) -> None:
        wh = _wh_default()
        # Tighter on Tuesday for this user
        wh["tuesday"] = {"start": "10:00", "end": "12:00", "available": True}
        slots, _ = compute_slots(
            working_hours_per_user={1: wh},
            busy_per_user={1: []},
            user_ids=[1],
            window_start=W_START,
            window_end=W_END,
            duration=timedelta(hours=1),
            tz=TZ,
        )
        tuesdays = [s.start for s in slots if s.start.date() == _local(2026, 7, 7, 0).date()]
        # 10:00, 10:15, 10:30, 10:45, 11:00 are valid (last one ends 12:00)
        self.assertEqual(
            tuesdays,
            [
                _local(2026, 7, 7, 10, 0),
                _local(2026, 7, 7, 10, 15),
                _local(2026, 7, 7, 10, 30),
                _local(2026, 7, 7, 10, 45),
                _local(2026, 7, 7, 11, 0),
            ],
        )

    # 7) regression: when a busy event ends at a non-grid time (14:43) the
    #    *first slot* after it must start at the next grid boundary (14:45),
    #    NOT at 14:43. This pins the _snap_up_to_grid invariant in the engine
    #    so frontend consumers don't have to rely on raw interval boundaries.
    def test_snap_up_aligns_first_slot_after_off_grid_busy(self) -> None:
        # Busy: 13:00-14:43 → free window after busy is 14:43-17:00.
        # First emitted slot must start at 14:45 (next 15-min grid after 14:43).
        busy = [(_local(2026, 7, 6, 13, 0), _local(2026, 7, 6, 14, 43))]
        one_day_end = _local(2026, 7, 7, 0, 0).astimezone(timezone.utc)
        slots, _ = compute_slots(
            working_hours_per_user={1: _wh_default()},
            busy_per_user={1: busy},
            user_ids=[1],
            window_start=W_START,
            window_end=one_day_end,
            duration=timedelta(minutes=30),
            tz=TZ,
        )
        # The first slot that falls after the busy block must start at 14:45,
        # not 14:43 — proving _snap_up_to_grid rounds up correctly.
        post_busy_starts = [s.start for s in slots if s.start >= _local(2026, 7, 6, 14, 0)]
        self.assertTrue(len(post_busy_starts) > 0, "Expected slots after 14:00")
        self.assertEqual(post_busy_starts[0], _local(2026, 7, 6, 14, 45))

    # 6) intersection of two users with different working hours yields the
    #    tighter window only.
    def test_intersection_tightens_window(self) -> None:
        wh_a = _wh_default()
        wh_a["monday"] = {"start": "08:00", "end": "12:00", "available": True}
        wh_b = _wh_default()
        wh_b["monday"] = {"start": "11:00", "end": "15:00", "available": True}
        slots, _ = compute_slots(
            working_hours_per_user={1: wh_a, 2: wh_b},
            busy_per_user={1: [], 2: []},
            user_ids=[1, 2],
            window_start=W_START,
            window_end=W_END,
            duration=timedelta(hours=1),
            tz=TZ,
        )
        mondays = [s.start for s in slots if s.start.date() == _local(2026, 7, 6, 0).date()]
        # Intersection on Monday is 11:00-12:00 only → exactly one 1-hour slot starts at 11:00.
        self.assertEqual(mondays, [_local(2026, 7, 6, 11, 0)])
