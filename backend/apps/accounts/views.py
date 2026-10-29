from __future__ import annotations

import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model, logout
from django.db import models, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.availability.models import Unavailability
from apps.calendars.models import Calendar, CalendarEvent
from apps.notifications.dispatch import notify
from apps.search.queries import busy_intervals_for_users as _busy_intervals_for_users
from apps.notifications.models import Notification
from apps.teams.models import Membership, Team

from .serializers import MeSerializer, PublicProfileSerializer, TeammateSerializer

User = get_user_model()


class MeView(APIView):
    """GET / PATCH the authenticated user's profile + working hours."""

    permission_classes = [IsAuthenticated]
    parser_classes = (JSONParser, FormParser, MultiPartParser)

    def get(self, request: Request) -> Response:
        return Response(MeSerializer(request.user, context={"request": request}).data)

    def patch(self, request: Request) -> Response:
        serializer = MeSerializer(
            request.user,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        # Avatar comes through request.FILES, not in MeSerializer fields.
        if "avatar" in request.FILES:
            request.user.avatar = request.FILES["avatar"]
            request.user.save(update_fields=["avatar"])
        return Response(MeSerializer(request.user, context={"request": request}).data)


class RegenerateShareTokenView(APIView):
    """POST /api/me/share/regenerate — invalidate old public link, mint a new one."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        request.user.share_token = uuid.uuid4()
        request.user.save(update_fields=["share_token"])
        return Response(
            MeSerializer(request.user, context={"request": request}).data,
        )


class MyAvailabilityView(APIView):
    """
    GET /api/me/availability?from=YYYY-MM-DD&to=YYYY-MM-DD

    Same shape as the public profile endpoint, but for the authenticated
    user — feeds the /profile overview page with busy, working_hours and
    holidays without needing the share token.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        user = request.user
        from_param = request.query_params.get("from")
        to_param = request.query_params.get("to")
        now = timezone.now()
        try:
            window_start = (
                _parse_iso_date(from_param) if from_param else now.replace(
                    hour=0, minute=0, second=0, microsecond=0,
                )
            )
            window_end = (
                _parse_iso_date(to_param) if to_param else window_start + timedelta(days=56)
            )
        except ValueError:
            return Response({"detail": "Invalid date format; expected YYYY-MM-DD."}, status=400)
        if window_end <= window_start:
            return Response({"detail": "to must be after from."}, status=400)
        if (window_end - window_start) > timedelta(days=84):
            window_end = window_start + timedelta(days=84)

        # Union of CalendarEvent + OAuthBusyWindow + Unavailability via shared helper.
        # This fixes bug #1/#2: OAuthBusyWindow rows were silently dropped before.
        busy = _busy_intervals_for_users([user.pk], window_start, window_end).get(user.pk, [])
        busy = _inflate_busy(busy, user.buffer_before_min, user.buffer_after_min)

        return Response({
            "profile": PublicProfileSerializer(user, context={"request": request}).data,
            "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
            "busy": [
                {"start": s.isoformat(), "end": e.isoformat()}
                for s, e in busy
            ],
            "holidays": _holidays_in_range(user.country, window_start.date(), window_end.date()),
        })


class PeerAvailabilityView(APIView):
    """
    POST /api/me/peer-availability/<user_id>
        body: { from?, to?, duration_min, buffer_min? }

    Compute the intersection of the authenticated caller's free time and
    the target user's free time over the requested window. Mirrors the
    main SearchView but takes one peer instead of a team.

    Authorization: caller may peer-search anyone they share a team with,
    or anyone whose share_enabled=True (effectively anyone publicly
    bookable). Self-peer is allowed but pointless.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, user_id: int) -> Response:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZoneInfo
        from django.conf import settings as _settings

        from apps.search.engine import compute_slots as _compute_slots
        from apps.teams.models import Membership as _Membership, Team as _Team

        peer = get_object_or_404(User, pk=user_id)
        if peer.pk == request.user.pk:
            return Response({"detail": "Pick a peer other than yourself."}, status=400)

        shared = _Team.objects.filter(memberships__user=request.user).filter(
            memberships__user=peer,
        ).exists()
        from apps.connections.models import Connection as _Connection
        connected = _Connection.are_connected(request.user.pk, peer.pk)
        if not (shared or connected or peer.share_enabled):
            return Response(
                {
                    "detail": (
                        "You're not connected to this user, don't share a group, "
                        "and their profile isn't public."
                    ),
                },
                status=403,
            )

        body = request.data or {}
        try:
            duration_min = int(body.get("duration_min", 30))
        except (TypeError, ValueError):
            return Response({"detail": "duration_min must be an integer."}, status=400)
        if duration_min < 5 or duration_min > 8 * 60:
            return Response({"detail": "duration_min out of range (5..480)."}, status=400)
        try:
            buffer_min = int(body.get("buffer_min", 0))
        except (TypeError, ValueError):
            return Response({"detail": "buffer_min must be an integer."}, status=400)

        now = timezone.now()
        try:
            window_start = (
                _parse_iso_datetime(body.get("from"))
                if body.get("from")
                else now.replace(hour=0, minute=0, second=0, microsecond=0)
            )
            window_end = (
                _parse_iso_datetime(body.get("to"))
                if body.get("to")
                else window_start + timedelta(days=56)
            )
        except ValueError:
            return Response(
                {"detail": "Invalid date format; expected ISO 8601 or YYYY-MM-DD."},
                status=400,
            )
        if window_end <= window_start:
            return Response({"detail": "to must be after from."}, status=400)
        if (window_end - window_start) > timedelta(days=84):
            window_end = window_start + timedelta(days=84)

        user_ids = [request.user.pk, peer.pk]
        working_hours_per_user = {
            request.user.pk: request.user.working_hours,
            peer.pk: peer.working_hours,
        }

        # Union of CalendarEvent + OAuthBusyWindow + Unavailability via shared helper.
        # This fixes bug #2: OAuthBusyWindow rows were silently dropped before.
        busy_per_user = _busy_intervals_for_users(user_ids, window_start, window_end)
        # Ensure both keys present even if no busy rows exist.
        for uid in user_ids:
            busy_per_user.setdefault(uid, [])
        # Apply each user's own buffer so back-to-back meetings don't leak
        # into the intersection either.
        busy_per_user[request.user.pk] = _inflate_busy(
            busy_per_user[request.user.pk],
            request.user.buffer_before_min,
            request.user.buffer_after_min,
        )
        busy_per_user[peer.pk] = _inflate_busy(
            busy_per_user[peer.pk],
            peer.buffer_before_min,
            peer.buffer_after_min,
        )

        slots, truncated = _compute_slots(
            working_hours_per_user=working_hours_per_user,
            busy_per_user=busy_per_user,
            user_ids=user_ids,
            window_start=window_start,
            window_end=window_end,
            duration=timedelta(minutes=duration_min),
            buffer=timedelta(minutes=buffer_min),
            tz=_ZoneInfo(_settings.TIME_ZONE),
            max_results=200,
        )

        return Response({
            "peer": PublicProfileSerializer(peer, context={"request": request}).data,
            "window": {
                "start": window_start.isoformat(),
                "end": window_end.isoformat(),
            },
            "slots": [
                {"start": s.start.isoformat(), "end": s.end.isoformat()}
                for s in slots
            ],
            "count": len(slots),
            "truncated": truncated,
            "holidays": _holidays_in_range(
                request.user.country, window_start.date(), window_end.date(),
            ),
        })


def _parse_iso_datetime(value: str):
    """Accept ISO 8601 datetimes OR YYYY-MM-DD (treated as midnight in app TZ)."""
    from datetime import datetime
    from django.conf import settings as dj_settings
    from zoneinfo import ZoneInfo
    s = value.strip()
    if len(s) == 10 and s.count("-") == 2:
        naive = datetime.strptime(s, "%Y-%m-%d")
        return naive.replace(tzinfo=ZoneInfo(dj_settings.TIME_ZONE))
    # Python 3.11+ tolerates trailing Z
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class TeammateAvailabilityView(APIView):
    """
    GET /api/users/<user_id>/availability?from=YYYY-MM-DD&to=YYYY-MM-DD

    Same shape as /api/me/availability, but for a peer. Renders the *peer's*
    own free time (working_hours minus their busy) — not an intersection with
    the caller. Powers /people/<id> so a teammate sees the same view the
    person sees when they open /profile themselves.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, user_id: int) -> Response:
        peer = get_object_or_404(User, pk=user_id)

        shared = Team.objects.filter(memberships__user=request.user).filter(
            memberships__user=peer,
        ).exists()
        from apps.connections.models import Connection as _Connection
        connected = _Connection.are_connected(request.user.pk, peer.pk)
        if not (shared or connected or peer.share_enabled):
            return Response(
                {
                    "detail": (
                        "You're not connected to this user, don't share a group, "
                        "and their profile isn't public."
                    ),
                },
                status=403,
            )

        from_param = request.query_params.get("from")
        to_param = request.query_params.get("to")
        now = timezone.now()
        try:
            window_start = (
                _parse_iso_date(from_param) if from_param else now.replace(
                    hour=0, minute=0, second=0, microsecond=0,
                )
            )
            window_end = (
                _parse_iso_date(to_param) if to_param else window_start + timedelta(days=56)
            )
        except ValueError:
            return Response({"detail": "Invalid date format; expected YYYY-MM-DD."}, status=400)
        if window_end <= window_start:
            return Response({"detail": "to must be after from."}, status=400)
        if (window_end - window_start) > timedelta(days=84):
            window_end = window_start + timedelta(days=84)

        # Union of CalendarEvent + OAuthBusyWindow + Unavailability via shared helper.
        # TeammateAvailabilityView had the same CalendarEvent-only bug as MyAvailabilityView.
        busy = _busy_intervals_for_users([peer.pk], window_start, window_end).get(peer.pk, [])
        busy = _inflate_busy(busy, peer.buffer_before_min, peer.buffer_after_min)

        return Response({
            "profile": PublicProfileSerializer(peer, context={"request": request}).data,
            "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
            "busy": [
                {"start": s.isoformat(), "end": e.isoformat()}
                for s, e in busy
            ],
            "holidays": _holidays_in_range(peer.country, window_start.date(), window_end.date()),
        })


class PublicProfileView(APIView):
    """
    GET /api/public/profile/<token>?from=YYYY-MM-DD&to=YYYY-MM-DD

    Anonymous read of the user's busy windows + name + avatar + working hours.
    Returns 404 when share_enabled is False or the token doesn't match —
    deliberately no signal that "the user exists but is private".

    Window defaults to today through today+56 days when from/to omitted.
    Cap range to 84 days to keep payload bounded.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []  # never authenticate, never set session

    def get(self, request: Request, token: uuid.UUID) -> Response:
        user = User.objects.filter(share_token=token, share_enabled=True).first()
        if user is None:
            return Response(status=404)

        from_param = request.query_params.get("from")
        to_param = request.query_params.get("to")
        now = timezone.now()
        try:
            window_start = (
                _parse_iso_date(from_param) if from_param else now.replace(
                    hour=0, minute=0, second=0, microsecond=0,
                )
            )
            window_end = (
                _parse_iso_date(to_param) if to_param else window_start + timedelta(days=56)
            )
        except ValueError:
            return Response({"detail": "Invalid date format; expected YYYY-MM-DD."}, status=400)

        if window_end <= window_start:
            return Response({"detail": "to must be after from."}, status=400)
        if (window_end - window_start) > timedelta(days=84):
            window_end = window_start + timedelta(days=84)

        # Aggregate busy intervals from calendars + manual unavailability blocks.
        # All-day events count as busy regardless of TRANSP — see SearchView.
        from django.db.models import Q as _Q
        busy: list[tuple] = []
        for ev in CalendarEvent.objects.filter(
            _Q(transp=CalendarEvent.Transparency.OPAQUE) | _Q(is_all_day=True),
            calendar__owner_id=user.pk,
            calendar__include_in_busy=True,
            dtstart__lt=window_end,
            dtend__gt=window_start,
        ).exclude(status=CalendarEvent.Status.CANCELLED).values("dtstart", "dtend"):
            busy.append((ev["dtstart"], ev["dtend"]))
        for u in Unavailability.objects.filter(
            user_id=user.pk,
            starts_at__lt=window_end,
            ends_at__gt=window_start,
        ).values("starts_at", "ends_at"):
            busy.append((u["starts_at"], u["ends_at"]))
        busy = _inflate_busy(busy, user.buffer_before_min, user.buffer_after_min)

        profile = PublicProfileSerializer(user, context={"request": request}).data
        # Active meeting types the visitor can pick from. Empty list means
        # the host hasn't defined any — /u/<token> falls back to the
        # generic "pick any free time" flow.
        from apps.scheduling.models import MeetingType as _MT
        types = list(
            _MT.objects.filter(host=user, is_active=True)
            .order_by("display_order", "id")
            .values(
                "slug", "name", "description", "duration_min",
                "kind", "location", "color", "questions", "redirect_url",
            )
        )
        return Response({
            "profile": profile,
            "window": {
                "start": window_start.isoformat(),
                "end": window_end.isoformat(),
            },
            "busy": [
                {"start": s.isoformat(), "end": e.isoformat()}
                for s, e in busy
            ],
            "holidays": _holidays_in_range(user.country, window_start.date(), window_end.date()),
            # Public flag consumed by /u/<token>. When False, the frontend
            # shows a soft "not yet accepting online bookings" banner and
            # hides the booking dialog.
            "booking_enabled": _has_writable_provider(user),
            "meeting_types": types,
        })


def _inflate_busy(pairs, buffer_before_min: int, buffer_after_min: int):
    """Expand each (start, end) busy tuple by the user's configured buffer.
    "Before" pads earlier so a new meeting ending too close to the next
    busy block doesn't fit; "After" pads later so a new meeting starting
    too close to the previous busy block doesn't fit. Called by every
    availability view so downstream computation stays buffer-agnostic."""
    from datetime import timedelta as _td
    if not buffer_before_min and not buffer_after_min:
        return list(pairs)
    before = _td(minutes=buffer_before_min)
    after = _td(minutes=buffer_after_min)
    return [(s - before, e + after) for s, e in pairs]


def _has_writable_provider(user) -> bool:
    """True if `user` has at least one OAuth-connected calendar we can write
    events into. Right now that's Google; adding Microsoft (or others) later
    just extends this check — everything above (public flag, /people hint,
    booking endpoints) then picks the change up automatically."""
    from apps.scheduling.models import GoogleAccount
    if GoogleAccount.objects.filter(user=user).exists():
        return True
    try:
        from apps.scheduling.models import MicrosoftAccount  # optional in early builds
    except ImportError:  # noqa: BLE001
        return False
    return MicrosoftAccount.objects.filter(user=user).exists()


def _holidays_in_range(country: str, from_date, to_date) -> list[dict]:
    """Return public holidays for `country` falling within [from_date, to_date)."""
    import holidays as holidays_lib
    years = list(range(from_date.year, to_date.year + 1))
    try:
        entries = holidays_lib.country_holidays(country, years=years, language="en_US")
    except Exception:  # noqa: BLE001 — some countries lack en_US
        try:
            entries = holidays_lib.country_holidays(country, years=years)
        except Exception:  # noqa: BLE001 — unsupported country code
            return []
    return [
        {"date": d.isoformat(), "name": name}
        for d, name in sorted(entries.items())
        if from_date <= d < to_date
    ]


def _parse_iso_date(value: str):
    """Parse YYYY-MM-DD into a tz-aware datetime at midnight in the app TZ."""
    from datetime import datetime
    from django.conf import settings as dj_settings
    from zoneinfo import ZoneInfo
    naive = datetime.strptime(value, "%Y-%m-%d")
    return naive.replace(tzinfo=ZoneInfo(dj_settings.TIME_ZONE))


class DeleteMeView(APIView):
    """
    GET  /api/me/delete  — preview: counts of what will be cascaded.
    POST /api/me/delete  — perform hard-delete. Body: {"password": "..."}.

    The user is removed immediately (PRD §5.7). Email is freed up.
    Teams where the user is the sole admin are deleted up-front (with
    team_deleted notifications to remaining members). Teams that lose their
    last admin via CASCADE on user.delete() are then also auto-deleted.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        user = request.user
        sole_admin_team_ids = _sole_admin_team_ids(user)
        affected_teammates = (
            Membership.objects.filter(team_id__in=sole_admin_team_ids)
            .exclude(user=user)
            .count()
        )
        return Response({
            "teams_member_count": Membership.objects.filter(user=user).count(),
            "teams_will_be_deleted": len(sole_admin_team_ids),
            "team_members_will_be_notified": affected_teammates,
            "calendars_count": Calendar.objects.filter(owner=user).count(),
            "cached_events_count": CalendarEvent.objects.filter(calendar__owner=user).count(),
            "notifications_count": Notification.objects.filter(recipient=user).count(),
        })

    def post(self, request: Request) -> Response:
        password = request.data.get("password", "")
        if not request.user.check_password(password):
            return Response({"password": "Incorrect password."}, status=400)

        user = request.user
        # Snapshot teams where this user is admin, before any deletion.
        sole_admin_team_ids = _sole_admin_team_ids(user)
        other_admin_team_ids = list(
            Membership.objects.filter(user=user, role=Membership.Role.ADMIN)
            .exclude(team_id__in=sole_admin_team_ids)
            .values_list("team_id", flat=True),
        )

        with transaction.atomic():
            # Step 1: delete sole-admin teams up front (notify members).
            for team in Team.objects.filter(pk__in=sole_admin_team_ids):
                _delete_team_with_notifications(team, exclude_user_id=user.pk)

            # Step 2: delete the user. CASCADE removes their other memberships,
            # calendars (and their events), notifications, EmailAddress rows, etc.
            user_pk = user.pk
            user.delete()

            # Step 3: any "co-admin" teams that lost their last admin via the
            # cascade now have zero admins → auto-delete per PRD §5.3.
            for team in Team.objects.filter(pk__in=other_admin_team_ids):
                if team.admin_count == 0:
                    _delete_team_with_notifications(team, exclude_user_id=user_pk)

        # Invalidate the session cookie. Doing this after delete is fine —
        # logout() just clears the session, not the user.
        logout(request)
        return Response(status=204)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sole_admin_team_ids(user) -> list[int]:
    """Teams where `user` is admin and the only admin."""
    return list(
        Team.objects.filter(memberships__user=user, memberships__role=Membership.Role.ADMIN)
        .annotate(
            admin_count_calc=models.Count(
                "memberships",
                filter=models.Q(memberships__role=Membership.Role.ADMIN),
            ),
        )
        .filter(admin_count_calc=1)
        .values_list("pk", flat=True),
    )


class TeammatesIndexView(APIView):
    """
    GET /api/users — the caller's people graph in one payload:

      {
        "people":   [<accepted: group-mates ∪ connections>],
        "incoming": [<pending Connection rows where caller is the receiver>],
        "outgoing": [<pending Connection rows the caller sent>]
      }

    `people[*]` carries `shared_team_names` (may be empty) and
    `connection_id` (null when visibility comes only from a shared
    group). UI uses this to badge each row and offer the right
    actions (View profile / Disconnect / Leave group).

    Replaces the old flat-array shape after Connections + People were
    merged into a single /people screen.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        from apps.connections.models import Connection
        from apps.connections.serializers import ConnectionSerializer

        # 1. Group-mates: anyone the caller shares a Team with.
        my_teams = Team.objects.filter(memberships__user=request.user).values_list("pk", flat=True)
        rows = (
            Membership.objects.filter(team_id__in=my_teams)
            .exclude(user=request.user)
            .select_related("user", "team")
            .values("user__id", "user__email", "user__first_name", "user__last_name", "team__name")
        )
        bucket: dict[int, dict] = {}
        for r in rows:
            uid = r["user__id"]
            entry = bucket.setdefault(
                uid,
                {
                    "id": uid,
                    "email": r["user__email"],
                    "first_name": r["user__first_name"],
                    "last_name": r["user__last_name"],
                    "shared_team_names": [],
                    "connection_id": None,
                },
            )
            entry["shared_team_names"].append(r["team__name"])

        # 2. Accepted connections: union into the same bucket. A peer who
        #    is *also* a group-mate keeps both signals; one who is only
        #    connected appears here for the first time.
        accepted_qs = Connection.objects.filter(
            status=Connection.Status.ACCEPTED,
        ).filter(models.Q(user_low=request.user) | models.Q(user_high=request.user))
        for conn in accepted_qs.select_related("user_low", "user_high"):
            peer = conn.user_high if conn.user_low_id == request.user.pk else conn.user_low
            entry = bucket.setdefault(
                peer.pk,
                {
                    "id": peer.pk,
                    "email": peer.email,
                    "first_name": peer.first_name,
                    "last_name": peer.last_name,
                    "shared_team_names": [],
                    "connection_id": None,
                },
            )
            entry["connection_id"] = conn.pk

        people = sorted(
            bucket.values(),
            key=lambda x: (
                (x["first_name"] + " " + x["last_name"]).strip().lower() or x["email"].lower(),
            ),
        )

        # 3. Pending requests, split by direction so the UI can render
        #    them in dedicated panels.
        pending_qs = Connection.objects.filter(
            status=Connection.Status.PENDING,
        ).filter(models.Q(user_low=request.user) | models.Q(user_high=request.user))
        ctx = {"request": request}
        incoming, outgoing = [], []
        for conn in pending_qs.select_related("user_low", "user_high", "requested_by"):
            data = ConnectionSerializer(conn, context=ctx).data
            (outgoing if conn.requested_by_id == request.user.pk else incoming).append(data)

        return Response({"people": people, "incoming": incoming, "outgoing": outgoing})


class TeammateView(APIView):
    """
    GET /api/users/<id> — public profile of a teammate.

    Visible only when the caller and the target share at least one team
    (PRD §5.5). The caller can also fetch their own profile this way.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, pk: int) -> Response:
        from apps.connections.models import Connection as _Connection
        target = get_object_or_404(User, pk=pk)
        shared = list(
            Team.objects.filter(memberships__user=request.user)
            .filter(memberships__user=target)
            .distinct()
            .values_list("pk", flat=True),
        )
        connected = _Connection.are_connected(request.user.pk, target.pk)
        if not shared and not connected and target.pk != request.user.pk:
            return Response(
                {
                    "detail": (
                        "You're not connected to this user and don't share any group with them."
                    ),
                },
                status=403,
            )
        return Response(
            TeammateSerializer(
                target,
                context={"shared_team_ids": shared},
            ).data,
        )


def _delete_team_with_notifications(team: Team, *, exclude_user_id: int | None = None) -> None:
    """Notify all current members (except `exclude_user_id`) and delete the team."""
    members = list(team.memberships.select_related("user"))
    team_id, team_name = team.pk, team.name
    team.delete()
    for m in members:
        if m.user_id == exclude_user_id:
            continue
        notify(
            m.user,
            Notification.Type.TEAM_DELETED,
            {"team_id": team_id, "team_name": team_name},
        )
