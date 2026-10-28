from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from django.db import models

Handler = Callable[[Any, dict], dict]  # (user: User, params: dict) -> dict

TOOL_SCOPES = ("list_teams", "list_members", "find_slots", "check_time", "create_booking")

# Scope display definitions for Settings UI (T5). The ORDER is the UI order.
TOKEN_SCOPE_UI = [
    {"name": "list_teams", "label": "List teams", "group": "read"},
    {"name": "list_members", "label": "List team members", "group": "read"},
    {"name": "find_slots", "label": "Find free slots", "group": "read"},
    {"name": "check_time", "label": "Check specific time", "group": "read"},
    {"name": "create_booking", "label": "Create / update / cancel bookings", "group": "write"},
]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str                       # e.g. "slotly.list_teams"
    description: str
    input_schema: dict              # JSON Schema (object) for params
    handler: Handler
    sensitivity: str = "read_only"  # "read_only" | "write_external"


_REGISTRY: dict[str, ToolDefinition] = {}


def register_tool(
    name: str,
    *,
    description: str,
    input_schema: dict,
    sensitivity: str = "read_only",
) -> Callable[[Handler], Handler]:
    def _decorator(func: Handler) -> Handler:
        if name in _REGISTRY:
            raise ValueError(f"Tool {name!r} already registered")
        _REGISTRY[name] = ToolDefinition(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=func,
            sensitivity=sensitivity,
        )
        return func
    return _decorator


def get_tool(name: str) -> ToolDefinition | None:
    return _REGISTRY.get(name)


def list_tools() -> list[ToolDefinition]:
    return sorted(_REGISTRY.values(), key=lambda t: t.name)


@register_tool(
    "slotly.list_teams",
    description="Vrátí seznam týmů, ve kterých je uživatel členem.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
def list_teams(user, params):
    from apps.teams.models import Team, Membership  # import inside to avoid app-load loops

    team_ids = list(
        Membership.objects.filter(user=user).values_list("team_id", flat=True)
    )
    teams = Team.objects.filter(pk__in=team_ids).order_by("name")

    # One extra query, aggregated — avoids N+1.
    counts = dict(
        Membership.objects
        .filter(team_id__in=team_ids)
        .values_list("team_id")
        .annotate(c=models.Count("id"))
    )

    return {
        "teams": [
            {
                "id": t.pk,
                "name": t.name,
                "description": t.description,
                "member_count": counts.get(t.pk, 0),
            }
            for t in teams
        ],
    }


@register_tool(
    "slotly.list_members",
    description="Vrátí členy daného týmu.",
    input_schema={
        "type": "object",
        "properties": {"team_id": {"type": "integer"}},
        "required": ["team_id"],
        "additionalProperties": False,
    },
)
def list_members(user, params):
    from django.conf import settings
    from apps.teams.models import Team, Membership
    from rest_framework.exceptions import ValidationError, PermissionDenied

    team_id = params.get("team_id")
    if not isinstance(team_id, int):
        raise ValidationError({"team_id": "must be an integer"})

    try:
        team = Team.objects.get(pk=team_id)
    except Team.DoesNotExist:
        raise ValidationError({"team_id": "team not found"})

    if not Membership.objects.filter(team=team, user=user).exists():
        raise PermissionDenied({"team_id": "you are not a member of this team"})

    tz = settings.TIME_ZONE
    rows = Membership.objects.filter(team=team).select_related("user").order_by(
        "user__first_name", "user__last_name", "user__email",
    )

    def _display_name(u):
        full = f"{u.first_name} {u.last_name}".strip()
        return full or u.email

    return {
        "team_id": team.pk,
        "members": [
            {
                "id": m.user_id,
                "email": m.user.email,
                "display_name": _display_name(m.user),
                "timezone": tz,
                "working_hours": m.user.working_hours,
                "role": m.role,
            }
            for m in rows
        ],
    }


@register_tool(
    "slotly.find_slots",
    description="Najdi společné volné sloty pro členy týmu v zadaném okně.",
    input_schema={
        "type": "object",
        "properties": {
            "team_id": {"type": "integer"},
            "member_ids": {"type": "array", "items": {"type": "integer"}},
            "duration_min": {"type": "integer", "minimum": 15, "maximum": 1440},
            "window_start": {"type": "string", "format": "date-time"},
            "window_end": {"type": "string", "format": "date-time"},
            "buffer_min": {"type": "integer", "minimum": 0, "maximum": 240, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
        },
        "required": ["team_id", "duration_min"],
        "additionalProperties": False,
    },
)
def find_slots(user, params):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from django.conf import settings
    from django.db.models import Q
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime
    from rest_framework.exceptions import PermissionDenied, ValidationError

    from apps.accounts.models import User as _User
    from apps.availability.models import Unavailability
    from apps.calendars.models import CalendarEvent
    from apps.search.engine import compute_slots
    from apps.teams.models import Membership, Team

    # 1. Validate team_id
    team_id = params.get("team_id")
    if not isinstance(team_id, int):
        raise ValidationError({"team_id": "must be an integer"})

    try:
        team = Team.objects.get(pk=team_id)
    except Team.DoesNotExist:
        raise ValidationError({"team_id": "team not found"})

    # 2. Caller must be a member
    if not Membership.objects.filter(team=team, user=user).exists():
        raise PermissionDenied({"team_id": "you are not a member of this team"})

    # 3. Validate duration_min
    duration_min = params.get("duration_min")
    if not isinstance(duration_min, int) or duration_min < 15 or duration_min > 1440:
        raise ValidationError({"duration_min": "must be an integer between 15 and 1440"})

    # 4. Resolve member_ids
    raw_member_ids = params.get("member_ids")
    if raw_member_ids is not None and len(raw_member_ids) > 0:
        # Validate uniqueness
        if len(raw_member_ids) != len(set(raw_member_ids)):
            raise ValidationError({"member_ids": "must be unique"})
        # Validate all are members of this team
        valid_ids = set(
            Membership.objects.filter(team=team, user_id__in=raw_member_ids)
            .values_list("user_id", flat=True)
        )
        bad = set(raw_member_ids) - valid_ids
        if bad:
            raise ValidationError({"member_ids": f"not team members: {sorted(bad)}"})
        member_ids = set(raw_member_ids)
    else:
        # Default to full team roster (R3)
        member_ids = set(
            Membership.objects.filter(team=team).values_list("user_id", flat=True)
        )

    # 5. Parse window_start / window_end
    now = timezone.now()
    default_window_start = now

    raw_window_start = params.get("window_start")
    if raw_window_start is not None:
        window_start = parse_datetime(raw_window_start)
        if window_start is None:
            raise ValidationError({"window_start": "invalid ISO 8601 timestamp"})
        if window_start.tzinfo is None:
            raise ValidationError({"window_start": "must include timezone offset"})
    else:
        window_start = default_window_start

    raw_window_end = params.get("window_end")
    if raw_window_end is not None:
        window_end = parse_datetime(raw_window_end)
        if window_end is None:
            raise ValidationError({"window_end": "invalid ISO 8601 timestamp"})
        if window_end.tzinfo is None:
            raise ValidationError({"window_end": "must include timezone offset"})
    else:
        window_end = window_start + timedelta(days=14)

    # 6. Validate window range
    if window_end <= window_start:
        raise ValidationError({"window_end": "must be after window_start"})
    if (window_end - window_start) > timedelta(days=60):
        raise ValidationError({"window_end": "window cannot exceed 60 days"})

    # 7. Validate buffer_min and limit
    buffer_min = params.get("buffer_min", 0)
    if not isinstance(buffer_min, int) or isinstance(buffer_min, bool) or not (0 <= buffer_min <= 240):
        raise ValidationError({"buffer_min": "must be an integer in [0, 240]"})

    limit = params.get("limit", 100)
    if not isinstance(limit, int) or isinstance(limit, bool) or not (1 <= limit <= 500):
        raise ValidationError({"limit": "must be an integer in [1, 500]"})

    # 8. Fetch working hours
    users = _User.objects.filter(pk__in=member_ids)
    working_hours_per_user = {u.pk: u.working_hours for u in users}

    # 9. Fetch busy intervals from CalendarEvent (opaque + all-day) and Unavailability
    events = CalendarEvent.objects.filter(
        Q(transp=CalendarEvent.Transparency.OPAQUE) | Q(is_all_day=True),
        calendar__owner_id__in=member_ids,
        calendar__include_in_busy=True,
        dtstart__lt=window_end,
        dtend__gt=window_start,
    ).exclude(
        status=CalendarEvent.Status.CANCELLED,
    ).values("calendar__owner_id", "dtstart", "dtend")

    busy_per_user: dict[int, list] = {uid: [] for uid in member_ids}
    for ev in events:
        busy_per_user[ev["calendar__owner_id"]].append((ev["dtstart"], ev["dtend"]))

    for u in Unavailability.objects.filter(
        user_id__in=member_ids,
        starts_at__lt=window_end,
        ends_at__gt=window_start,
    ).values("user_id", "starts_at", "ends_at"):
        busy_per_user[u["user_id"]].append((u["starts_at"], u["ends_at"]))

    # 10. Call compute_slots — do NOT create a RecentSearch row
    slots, truncated = compute_slots(
        working_hours_per_user=working_hours_per_user,
        busy_per_user=busy_per_user,
        user_ids=list(member_ids),
        window_start=window_start,
        window_end=window_end,
        duration=timedelta(minutes=duration_min),
        buffer=timedelta(minutes=buffer_min),
        tz=ZoneInfo(settings.TIME_ZONE),
        max_results=limit,
    )

    return {
        "slots": [
            {"start": s.start.isoformat(), "end": s.end.isoformat()}
            for s in slots
        ],
        "count": len(slots),
        "truncated": truncated,
    }


def _merge_intervals(intervals: list[tuple]) -> list[tuple]:
    """Merge overlapping (start, end) intervals into a minimal set.

    Input need not be sorted; output is sorted by start.

    NOTE: this is a local copy of apps/search/views._merge_intervals.
    If the logic ever changes, update both; future consolidation should
    move the function to apps/search/utils.py and import from there.
    """
    if not intervals:
        return []
    items = sorted(intervals, key=lambda x: x[0])
    out = [items[0]]
    for s, e in items[1:]:
        last_s, last_e = out[-1]
        if s <= last_e:
            out[-1] = (last_s, max(last_e, e))
        else:
            out.append((s, e))
    return out


@register_tool(
    "slotly.check_time",
    description="Ověř zda jsou vybraní členové týmu volní v daném okně.",
    input_schema={
        "type": "object",
        "properties": {
            "team_id": {"type": "integer"},
            "member_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
            "start": {"type": "string", "format": "date-time"},
            "end": {"type": "string", "format": "date-time"},
        },
        "required": ["team_id", "member_ids", "start", "end"],
        "additionalProperties": False,
    },
)
def check_time(user, params):
    from datetime import timedelta

    from django.db.models import Q
    from django.utils.dateparse import parse_datetime
    from rest_framework.exceptions import PermissionDenied, ValidationError

    from apps.availability.models import Unavailability
    from apps.calendars.models import CalendarEvent
    from apps.teams.models import Membership, Team

    # 1. Validate team_id
    team_id = params.get("team_id")
    if not isinstance(team_id, int):
        raise ValidationError({"team_id": "must be an integer"})

    try:
        team = Team.objects.get(pk=team_id)
    except Team.DoesNotExist:
        raise ValidationError({"team_id": "team not found"})

    # 2. Caller must be a team member
    if not Membership.objects.filter(team=team, user=user).exists():
        raise PermissionDenied({"team_id": "you are not a member of this team"})

    # 3. Validate member_ids
    raw_member_ids = params.get("member_ids")
    if raw_member_ids is None:
        raise ValidationError({"member_ids": "this field is required"})
    if not isinstance(raw_member_ids, list) or len(raw_member_ids) == 0:
        raise ValidationError({"member_ids": "must be a non-empty list"})
    if not all(isinstance(x, int) for x in raw_member_ids):
        raise ValidationError({"member_ids": "all values must be integers"})
    if len(raw_member_ids) != len(set(raw_member_ids)):
        raise ValidationError({"member_ids": "must be unique"})

    valid_ids = set(
        Membership.objects.filter(team=team, user_id__in=raw_member_ids)
        .values_list("user_id", flat=True)
    )
    bad = set(raw_member_ids) - valid_ids
    if bad:
        raise ValidationError({"member_ids": f"not team members: {sorted(bad)}"})

    member_ids = list(valid_ids)

    # 4. Parse and validate start/end
    raw_start = params.get("start")
    raw_end = params.get("end")

    if raw_start is None:
        raise ValidationError({"start": "this field is required"})
    if raw_end is None:
        raise ValidationError({"end": "this field is required"})

    start = parse_datetime(raw_start)
    if start is None:
        raise ValidationError({"start": "invalid ISO 8601 timestamp"})
    if start.tzinfo is None:
        raise ValidationError({"start": "must include timezone offset"})

    end = parse_datetime(raw_end)
    if end is None:
        raise ValidationError({"end": "invalid ISO 8601 timestamp"})
    if end.tzinfo is None:
        raise ValidationError({"end": "must include timezone offset"})

    if end <= start:
        raise ValidationError({"end": "must be after start"})
    if (end - start) > timedelta(hours=24):
        raise ValidationError({"end": "single check window cannot exceed 24 hours"})

    # 5. Collect busy intervals per member (mirrors CheckTimeView.post)
    events = CalendarEvent.objects.filter(
        Q(transp=CalendarEvent.Transparency.OPAQUE) | Q(is_all_day=True),
        calendar__owner_id__in=member_ids,
        calendar__include_in_busy=True,
        dtstart__lt=end,
        dtend__gt=start,
    ).exclude(
        status=CalendarEvent.Status.CANCELLED,
    ).values("calendar__owner_id", "dtstart", "dtend")

    conflicts_per_user: dict[int, list[tuple]] = {uid: [] for uid in member_ids}
    for ev in events:
        conflicts_per_user[ev["calendar__owner_id"]].append(
            (max(ev["dtstart"], start), min(ev["dtend"], end)),
        )

    for u in Unavailability.objects.filter(
        user_id__in=member_ids,
        starts_at__lt=end,
        ends_at__gt=start,
    ).values("user_id", "starts_at", "ends_at"):
        conflicts_per_user[u["user_id"]].append(
            (max(u["starts_at"], start), min(u["ends_at"], end)),
        )

    # 6. Build response — ordered, privacy-locked (no titles, no labels)
    from apps.accounts.models import User as _User

    users = _User.objects.filter(pk__in=member_ids).order_by(
        "first_name", "last_name", "email",
    )

    def _display_name(u):
        full = f"{u.first_name} {u.last_name}".strip()
        return full or u.email

    members = []
    available = True
    for u in users:
        raw = conflicts_per_user.get(u.pk, [])
        merged = _merge_intervals(raw)
        status_str = "busy" if merged else "free"
        if merged:
            available = False
        members.append({
            "user_id": u.pk,
            "display_name": _display_name(u),
            "email": u.email,
            "status": status_str,
            "conflicts": [
                {"start": s.isoformat(), "end": e.isoformat()} for s, e in merged
            ],
        })

    return {
        "team_id": team.pk,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "available": available,
        "members": members,
    }


# ────────────────────────────────────────────────────────────────────────────
# Write-booking helpers and handlers (T3)
# ────────────────────────────────────────────────────────────────────────────


def _parse_dt(raw, field_name):
    """Parse an ISO 8601 string with timezone into a datetime.

    Raises ValidationError with the field name on any problem.
    """
    from django.utils.dateparse import parse_datetime
    from rest_framework.exceptions import ValidationError

    if not isinstance(raw, str) or not raw:
        raise ValidationError({field_name: "required ISO 8601 string"})
    parsed = parse_datetime(raw)
    if parsed is None:
        raise ValidationError({field_name: "invalid ISO 8601"})
    if parsed.tzinfo is None:
        raise ValidationError({field_name: "must include timezone offset"})
    return parsed


@register_tool(
    "slotly.create_booking",
    description="Vytvoří booking: zapíše event do hostova kalendáře a pozve attendees emailem.",
    input_schema={
        "type": "object",
        "properties": {
            "attendee_user_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
                "maxItems": 100,
                "description": "User IDs ze slotly.list_members. Musí být členové sdíleného týmu s hostem.",
            },
            "start": {"type": "string", "format": "date-time"},
            "end": {"type": "string", "format": "date-time"},
            "title": {"type": "string", "maxLength": 200},
            "notes": {"type": "string", "maxLength": 2000},
            "meeting_type_id": {"type": "integer", "description": "Volitelně odkaz na MeetingType."},
        },
        "required": ["attendee_user_ids", "start", "end"],
        "additionalProperties": False,
    },
    sensitivity="write_external",
)
def create_booking(user, params):
    from rest_framework.exceptions import ValidationError

    from apps.scheduling.services import create_booking as _svc_create_booking

    # Parse input
    attendee_user_ids = params.get("attendee_user_ids")
    if not isinstance(attendee_user_ids, list) or not attendee_user_ids:
        raise ValidationError({"attendee_user_ids": "required non-empty list of integers"})
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in attendee_user_ids):
        raise ValidationError({"attendee_user_ids": "all elements must be integers"})
    if len(attendee_user_ids) > 100:
        raise ValidationError({"attendee_user_ids": "max 100 attendees"})

    start = _parse_dt(params.get("start"), "start")
    end = _parse_dt(params.get("end"), "end")

    title = params.get("title", "")
    if not isinstance(title, str):
        raise ValidationError({"title": "must be a string"})
    notes = params.get("notes", "")
    if not isinstance(notes, str):
        raise ValidationError({"notes": "must be a string"})

    meeting_type_id = params.get("meeting_type_id")
    if meeting_type_id is not None and (not isinstance(meeting_type_id, int) or isinstance(meeting_type_id, bool)):
        raise ValidationError({"meeting_type_id": "must be an integer"})

    # Delegate — service handles all business logic (visibility, conflict,
    # provider write). The service raises ValidationError / PermissionDenied
    # which ToolsCallView translates to 400 / 403.
    return _svc_create_booking(
        host_user=user,
        start=start,
        end=end,
        attendee_user_ids=attendee_user_ids,
        title=title,
        notes=notes,
        meeting_type_id=meeting_type_id,
    )


@register_tool(
    "slotly.update_booking",
    description="Změní čas / název existujícího bookingu + propsat do kalendáře.",
    input_schema={
        "type": "object",
        "properties": {
            "booking_uuid": {"type": "string", "format": "uuid"},
            "start": {"type": "string", "format": "date-time"},
            "end": {"type": "string", "format": "date-time"},
            "title": {"type": "string", "maxLength": 200},
        },
        "required": ["booking_uuid"],
        "additionalProperties": False,
    },
    sensitivity="write_external",
)
def update_booking(user, params):
    from rest_framework.exceptions import ValidationError

    from apps.scheduling.services import update_booking as _svc_update_booking

    booking_uuid = params.get("booking_uuid")
    if not isinstance(booking_uuid, str) or not booking_uuid:
        raise ValidationError({"booking_uuid": "required UUID string"})

    # Validate at least one of (start/end/title) was given.
    has_time = "start" in params or "end" in params
    raw_title = params.get("title")
    has_title = raw_title is not None and isinstance(raw_title, str) and raw_title.strip() != ""
    if not (has_time or has_title):
        raise ValidationError({"detail": "nothing to update — provide start/end and/or non-empty title"})

    start = _parse_dt(params["start"], "start") if "start" in params else None
    end = _parse_dt(params["end"], "end") if "end" in params else None

    # Both start and end must be given together or neither.
    if (start is None) != (end is None):
        raise ValidationError({"detail": "start and end must be provided together"})

    if raw_title is not None and not isinstance(raw_title, str):
        raise ValidationError({"title": "must be a string"})

    title = raw_title.strip() if has_title else None

    return _svc_update_booking(
        host_user=user,
        booking_uuid=booking_uuid,
        start=start,
        end=end,
        title=title,
    )


@register_tool(
    "slotly.cancel_booking",
    description="Zruší booking (idempotentní — druhý call vrátí current state).",
    input_schema={
        "type": "object",
        "properties": {
            "booking_uuid": {"type": "string", "format": "uuid"},
            "reason": {"type": "string", "maxLength": 500},
        },
        "required": ["booking_uuid"],
        "additionalProperties": False,
    },
    sensitivity="write_external",
)
def cancel_booking(user, params):
    from rest_framework.exceptions import ValidationError

    from apps.scheduling.services import cancel_booking as _svc_cancel_booking

    booking_uuid = params.get("booking_uuid")
    if not isinstance(booking_uuid, str) or not booking_uuid:
        raise ValidationError({"booking_uuid": "required UUID string"})
    reason = params.get("reason", "")
    if not isinstance(reason, str):
        raise ValidationError({"reason": "must be a string"})

    return _svc_cancel_booking(
        host_user=user,
        booking_uuid=booking_uuid,
        reason=reason,
    )
