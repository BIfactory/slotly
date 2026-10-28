from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from django.db import models

Handler = Callable[[Any, dict], dict]  # (user: User, params: dict) -> dict

TOOL_SCOPES = ("list_teams", "list_members", "find_slots", "check_time")


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str                       # e.g. "slotly.list_teams"
    description: str
    input_schema: dict              # JSON Schema (object) for params
    handler: Handler


_REGISTRY: dict[str, ToolDefinition] = {}


def register_tool(
    name: str,
    *,
    description: str,
    input_schema: dict,
) -> Callable[[Handler], Handler]:
    def _decorator(func: Handler) -> Handler:
        if name in _REGISTRY:
            raise ValueError(f"Tool {name!r} already registered")
        _REGISTRY[name] = ToolDefinition(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=func,
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
    return {"everyone_free": True, "people": []}  # dummy — T6 replaces
