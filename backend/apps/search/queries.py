"""Shared busy-interval helper for all availability-reading endpoints.

``busy_intervals_for_users`` is the single source of truth for reading
busy time from the three sources that can block a user's calendar:

1. CalendarEvent (ICS pipeline) — opaque or all-day events.
2. OAuthBusyWindow (Google freebusy / MS Graph getSchedule).
3. Unavailability (manual blocks, PRD M12).

Return type::

    dict[int, list[tuple[datetime, datetime]]]

The outer key is the user-id (int).  Each value is a sorted, merged list
of (start, end) tz-aware UTC datetime tuples covering that user's busy
intervals within [window_start, window_end).  Intervals are merged so that
overlapping windows become one; adjacent-but-not-overlapping intervals stay
separate (same semantics as the existing _merge_intervals helper in
apps.search.views).

Callers that need an empty list for a user not yet seen in the result dict
can use ``result.get(uid, [])``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from django.db.models import Q

from apps.availability.models import Unavailability
from apps.calendars.models import CalendarEvent
from apps.scheduling.models import OAuthBusyWindow


def busy_intervals_for_users(
    user_ids: Iterable[int],
    window_start: datetime,
    window_end: datetime,
) -> dict[int, list[tuple[datetime, datetime]]]:
    """Return merged busy intervals per user within [window_start, window_end).

    Parameters
    ----------
    user_ids:
        The set of user primary-keys to query.  May be any iterable; will be
        consumed once.
    window_start:
        Inclusive window boundary — tz-aware UTC datetime.
    window_end:
        Exclusive window boundary — tz-aware UTC datetime.

    Returns
    -------
    dict[int, list[tuple[datetime, datetime]]]
        Per-user sorted, merged list of (start, end) tz-aware UTC tuples.
        Users with no busy intervals appear with an empty list.

    Raises
    ------
    ValueError
        If ``window_start`` or ``window_end`` are tz-naive (guards against
        accidental UTC-strip from ORM or serialiser).
    """
    _assert_tz_aware(window_start, "window_start")
    _assert_tz_aware(window_end, "window_end")

    uid_set = set(user_ids)
    if not uid_set:
        return {}

    raw: dict[int, list[tuple[datetime, datetime]]] = {uid: [] for uid in uid_set}

    # ---- 1. CalendarEvent (ICS / manual subscriptions) ----------------------
    events = CalendarEvent.objects.filter(
        Q(transp=CalendarEvent.Transparency.OPAQUE) | Q(is_all_day=True),
        calendar__owner_id__in=uid_set,
        calendar__include_in_busy=True,
        dtstart__lt=window_end,
        dtend__gt=window_start,
    ).exclude(
        status=CalendarEvent.Status.CANCELLED,
    ).values("calendar__owner_id", "dtstart", "dtend")

    for ev in events:
        dtstart = ev["dtstart"]
        dtend = ev["dtend"]
        _assert_tz_aware(dtstart, "CalendarEvent.dtstart")
        _assert_tz_aware(dtend, "CalendarEvent.dtend")
        raw[ev["calendar__owner_id"]].append((dtstart, dtend))

    # ---- 2. OAuthBusyWindow (Google freebusy / MS Graph getSchedule) --------
    # No additional filter beyond user + time range: every row in
    # OAuthBusyWindow is opaque by definition (Google freebusy and MS Graph
    # getSchedule only return blocking intervals).  The calendar_id column is
    # informational only; secondary-calendar rows (c_<hash> IDs) are included
    # just like primary-calendar rows.
    for ob in OAuthBusyWindow.objects.filter(
        user_id__in=uid_set,
        starts_at__lt=window_end,
        ends_at__gt=window_start,
    ).values("user_id", "starts_at", "ends_at"):
        starts_at = ob["starts_at"]
        ends_at = ob["ends_at"]
        _assert_tz_aware(starts_at, "OAuthBusyWindow.starts_at")
        _assert_tz_aware(ends_at, "OAuthBusyWindow.ends_at")
        raw[ob["user_id"]].append((starts_at, ends_at))

    # ---- 3. Unavailability (manual blocks, PRD M12) -------------------------
    for u in Unavailability.objects.filter(
        user_id__in=uid_set,
        starts_at__lt=window_end,
        ends_at__gt=window_start,
    ).values("user_id", "starts_at", "ends_at"):
        starts_at = u["starts_at"]
        ends_at = u["ends_at"]
        _assert_tz_aware(starts_at, "Unavailability.starts_at")
        _assert_tz_aware(ends_at, "Unavailability.ends_at")
        raw[u["user_id"]].append((starts_at, ends_at))

    return {uid: _merge_intervals(intervals) for uid, intervals in raw.items()}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Merge overlapping (start, end) intervals. Input need not be sorted;
    output is sorted by start.  Adjacent-but-not-overlapping intervals stay
    separate (``s <= last_e`` for overlap, strict less-than for touching)."""
    if not intervals:
        return []
    items = sorted(intervals, key=lambda x: x[0])
    out = [items[0]]
    for s, e in items[1:]:
        last_s, last_e = out[-1]
        if s <= last_e:  # overlap: merge
            out[-1] = (last_s, max(last_e, e))
        else:
            out.append((s, e))
    return out


def _assert_tz_aware(dt: datetime, label: str) -> None:
    """Raise ValueError if *dt* is tz-naive.

    This guards both Google freebusy (returns UTC 'Z'-suffix strings, parsed
    as tz-aware) and MS Graph (requires ``Prefer: outlook.timezone="UTC"`` to
    normalise; tasks.py enforces this).  A naive datetime here means a source
    stripped the timezone info, which would produce silent UTC-drift bugs.
    """
    if dt.tzinfo is None:
        raise ValueError(
            f"busy_intervals_for_users: {label} is tz-naive ({dt!r}). "
            "All datetime sources must return tz-aware UTC datetimes."
        )
