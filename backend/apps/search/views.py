from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ModelViewSet

from apps.teams.models import Membership, Team
from apps.teams.permissions import is_member

from .engine import compute_slots
from .queries import _merge_intervals, busy_intervals_for_users
from .models import RECENT_SEARCH_LIMIT, RecentSearch, SavedSearch
from .serializers import (
    CheckTimeInputSerializer,
    RecentSearchSerializer,
    SavedSearchSerializer,
    SearchInputSerializer,
)

User = get_user_model()


class SearchView(APIView):
    """
    POST /api/search
        body: {
            team_id, member_ids[], duration_min,
            window_start?, window_end?, buffer_min?
        }
        response: { slots: [{start, end}], count, truncated }

    Side-effect: a RecentSearch row is recorded for the caller and the
    list is trimmed to the latest RECENT_SEARCH_LIMIT entries.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        s = SearchInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        team = get_object_or_404(Team, pk=data["team_id"])
        if not is_member(request.user, team):
            return Response({"detail": "You are not a member of this team."}, status=403)

        team_member_ids = set(
            Membership.objects.filter(team=team, user_id__in=data["member_ids"])
            .values_list("user_id", flat=True),
        )
        bad = set(data["member_ids"]) - team_member_ids
        if bad:
            return Response(
                {"detail": f"Some users are not members of this team: {sorted(bad)}"},
                status=400,
            )

        users = User.objects.filter(pk__in=team_member_ids)
        working_hours_per_user = {u.pk: u.working_hours for u in users}

        # Union of CalendarEvent + OAuthBusyWindow + Unavailability via shared helper.
        # All-day events (vacation, sick day, "off") block the day regardless of TRANSP.
        # OAuth busy intervals (Google freebusy / MS Graph) are included alongside the
        # ICS pipeline — not a replacement. Privacy: OAuthBusyWindow stores only
        # (starts_at, ends_at), no event details. See docs/oauth-busy.md.
        busy_per_user = busy_intervals_for_users(
            team_member_ids,
            data["window_start"],
            data["window_end"],
        )
        # Ensure every requested user appears in the dict even with no busy rows.
        for uid in team_member_ids:
            busy_per_user.setdefault(uid, [])

        slots, truncated = compute_slots(
            working_hours_per_user=working_hours_per_user,
            busy_per_user=busy_per_user,
            user_ids=list(team_member_ids),
            window_start=data["window_start"],
            window_end=data["window_end"],
            duration=timedelta(minutes=data["duration_min"]),
            buffer=timedelta(minutes=data["buffer_min"]),
            tz=ZoneInfo(settings.TIME_ZONE),
            max_results=data["limit"],
        )

        # Record to history (best-effort; never block the search response).
        try:
            _record_recent_search(request.user, team, data)
        except Exception:  # noqa: BLE001
            pass

        # Compact "earliest start, latest end" across the working_hours of
        # every selected member — anchors the calendar time axis on the
        # frontend so multi-member searches show a stable day range.
        wh_range = _working_hours_range(working_hours_per_user.values())

        return Response({
            "slots": [
                {"start": slot.start.isoformat(), "end": slot.end.isoformat()}
                for slot in slots
            ],
            "count": len(slots),
            "truncated": truncated,
            "working_hours_range": wh_range,
        })


class CheckTimeView(APIView):
    """
    POST /api/search/check-time
        body: { team_id, member_ids[], start, end }
        response: {
            everyone_free: bool,
            people: [
                { user_id, first_name, last_name, email,
                  status: "free" | "busy",
                  conflicts: [{ start, end }]  # clipped to the requested window
                }, ...
            ]
        }

    Companion mode to /api/search — instead of 'find slots in a range',
    answers the more common everyday question 'is everyone free at this
    specific time?'. Working-hours are intentionally NOT applied here:
    the user asked a literal question about a real moment, so a person
    with an empty calendar at that moment is 'free' even if it's outside
    their normal working hours.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        s = CheckTimeInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        team = get_object_or_404(Team, pk=data["team_id"])
        if not is_member(request.user, team):
            return Response({"detail": "You are not a member of this team."}, status=403)

        team_member_ids = set(
            Membership.objects.filter(team=team, user_id__in=data["member_ids"])
            .values_list("user_id", flat=True),
        )
        bad = set(data["member_ids"]) - team_member_ids
        if bad:
            return Response(
                {"detail": f"Some users are not members of this team: {sorted(bad)}"},
                status=400,
            )

        win_start, win_end = data["start"], data["end"]

        # Union of CalendarEvent + OAuthBusyWindow + Unavailability via shared helper.
        # Same TRANSP + all-day semantics as /api/search. Intervals are clipped to
        # [win_start, win_end) below so the conflicts list only shows overlap.
        busy_raw = busy_intervals_for_users(team_member_ids, win_start, win_end)

        conflicts_per_user: dict[int, list[tuple]] = {uid: [] for uid in team_member_ids}
        for uid in team_member_ids:
            for s, e in busy_raw.get(uid, []):
                conflicts_per_user[uid].append(
                    (max(s, win_start), min(e, win_end)),
                )

        users = User.objects.filter(pk__in=team_member_ids).order_by(
            "first_name", "last_name", "email",
        )
        people = []
        everyone_free = True
        for u in users:
            raw = conflicts_per_user.get(u.pk, [])
            merged = _merge_intervals(raw)
            status_str = "busy" if merged else "free"
            if merged:
                everyone_free = False
            people.append({
                "user_id": u.pk,
                "first_name": u.first_name,
                "last_name": u.last_name,
                "email": u.email,
                "status": status_str,
                "conflicts": [
                    {"start": s.isoformat(), "end": e.isoformat()} for s, e in merged
                ],
            })

        return Response({"everyone_free": everyone_free, "people": people})


def _working_hours_range(per_user_hours) -> list[int] | None:
    """Earliest start hour and latest end hour across all available days,
    over all users. Returns [start_hour, end_hour] or None when nobody has
    any available day. Hours are 0-24, end hour is rounded up to the next
    full hour when minutes>0."""
    min_start = 24
    max_end = 0
    seen = False
    for wh in per_user_hours:
        if not isinstance(wh, dict):
            continue
        for day in wh.values():
            if not isinstance(day, dict) or not day.get("available"):
                continue
            seen = True
            try:
                sh, _sm = (int(x) for x in str(day["start"]).split(":"))
                eh, em = (int(x) for x in str(day["end"]).split(":"))
            except (KeyError, ValueError, TypeError):
                continue
            if sh < min_start:
                min_start = sh
            cap = eh + 1 if em > 0 else eh
            if cap > max_end:
                max_end = cap
    if not seen:
        return None
    return [min_start, max_end]


def _record_recent_search(user, team: Team, data: dict) -> None:
    RecentSearch.objects.create(
        owner=user,
        team=team,
        member_ids=data["member_ids"],
        duration_min=data["duration_min"],
        buffer_min=data["buffer_min"],
        window_start=data["window_start"],
        window_end=data["window_end"],
    )
    keep_ids = list(
        RecentSearch.objects.filter(owner=user)
        .order_by("-created_at")
        .values_list("pk", flat=True)[:RECENT_SEARCH_LIMIT],
    )
    RecentSearch.objects.filter(owner=user).exclude(pk__in=keep_ids).delete()


# ---------------------------------------------------------------------------
# Saved searches CRUD
# ---------------------------------------------------------------------------


class SavedSearchViewSet(ModelViewSet):
    serializer_class = SavedSearchSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return SavedSearch.objects.filter(owner=self.request.user)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    def perform_update(self, serializer):
        serializer.save(owner=self.request.user)


# ---------------------------------------------------------------------------
# Recent searches (read-only list + delete-one)
# ---------------------------------------------------------------------------


class RecentSearchListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        qs = RecentSearch.objects.filter(owner=request.user).order_by("-created_at")[:RECENT_SEARCH_LIMIT]
        return Response(RecentSearchSerializer(qs, many=True).data)


class RecentSearchDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request: Request, pk: int) -> Response:
        deleted, _ = RecentSearch.objects.filter(owner=request.user, pk=pk).delete()
        if deleted == 0:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)
