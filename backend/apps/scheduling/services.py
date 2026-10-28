"""
apps/scheduling/services.py — pure-Python service layer for booking operations.

Four entry points usable from both HTTP views and MCP write handlers:
  create_booking   — host books a meeting with Slotly-user attendees
  cancel_booking   — host cancels a confirmed booking
  update_booking   — host changes start/end/title of a confirmed booking
  create_event     — host creates a bare calendar event (no Booking row)

Each function takes typed Python args and returns a plain dict; no DRF
Request/Response objects cross this boundary. User-facing problems raise DRF
exceptions (ValidationError, PermissionDenied) so callers can translate them
to the right HTTP status codes.

Helpers that only views needed previously are pulled in here too — the helper
section at the bottom mirrors what was in views.py so both callers can import
from a single place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone as djtz
from rest_framework.exceptions import PermissionDenied, ValidationError

from . import graph_client as _graph
from .google_client import (
    GoogleApiError,
    GoogleOAuthError,
    create_calendar_event,
    delete_calendar_event,
)
from .models import Booking, MeetingType

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# Provider abstraction
# ────────────────────────────────────────────────────────────────────────────


class _WriteProvider:
    """Uniform handle over Google vs. Microsoft. Booking code calls
    ``provider.create_event(user, ...)`` and ``provider.delete_event``
    without caring which backend it dispatches to.

    create_event/delete_event are resolved lazily through their source
    *module attribute* at the time of each call. This means unittest
    patches on ``apps.scheduling.google_client.create_calendar_event`` (or
    on ``apps.scheduling.views.create_calendar_event``, which views.py
    imports and re-exports) take effect for tests of views that use the
    old views._pick_write_provider.  For services-layer calls, patch
    ``apps.scheduling.google_client.create_calendar_event`` directly.
    """

    def __init__(self, name: str, write_calendar_id: str, _backend: str) -> None:
        self.name = name
        self.write_calendar_id = write_calendar_id
        self._backend = _backend  # "google" or "microsoft"

    def create_event(self, *args: Any, **kwargs: Any) -> Any:
        if self._backend == "google":
            from . import google_client as _gc
            return _gc.create_calendar_event(*args, **kwargs)
        from . import graph_client as _ms
        return _ms.create_calendar_event(*args, **kwargs)

    def delete_event(self, *args: Any, **kwargs: Any) -> Any:
        if self._backend == "google":
            from . import google_client as _gc
            return _gc.delete_calendar_event(*args, **kwargs)
        from . import graph_client as _ms
        return _ms.delete_calendar_event(*args, **kwargs)


def _pick_write_provider(user) -> _WriteProvider | None:
    """Choose which OAuth-connected provider should host the new event.
    Prefers Google when both are connected — arbitrary but stable, and
    the user can effectively override by disconnecting the one they don't
    want us writing into. Returns None when neither is connected."""
    from .models import GoogleAccount, MicrosoftAccount

    g = GoogleAccount.objects.filter(user=user).only("write_calendar_id").first()
    if g is not None:
        return _WriteProvider(
            name="google",
            write_calendar_id=g.write_calendar_id or "primary",
            _backend="google",
        )
    m = MicrosoftAccount.objects.filter(user=user).only("write_calendar_id").first()
    if m is not None:
        return _WriteProvider(
            name="microsoft",
            write_calendar_id=m.write_calendar_id,  # empty = primary
            _backend="microsoft",
        )
    return None


# ────────────────────────────────────────────────────────────────────────────
# Service functions
# ────────────────────────────────────────────────────────────────────────────


def create_booking(
    host_user,
    *,
    start: datetime,
    end: datetime,
    attendee_user_ids: list[int],
    title: str = "",
    notes: str = "",
    meeting_type_id: int | None = None,
) -> dict[str, Any]:
    """Create a booking between a host and one or more Slotly users.

    Validates inputs, checks visibility and availability, creates the
    calendar event, and persists a Booking row.

    Raises:
        ValidationError  — bad inputs, missing users, or slot conflict.
        PermissionDenied — host cannot see one of the attendees.
        GoogleOAuthError / MicrosoftOAuthError — provider auth broken (→ 503).
        GoogleApiError   / MicrosoftApiError   — provider rejected event (→ 502).
    """
    from django.contrib.auth import get_user_model
    _User = get_user_model()

    # ── duration validation ──────────────────────────────────────────────────
    if end <= start:
        raise ValidationError({"detail": "end must be after start."})
    duration = end - start
    if duration < timedelta(minutes=5):
        raise ValidationError({"detail": "Meeting must be at least 5 minutes."})
    if duration > timedelta(hours=12):
        raise ValidationError({"detail": "Meeting longer than 12 hours refused."})

    # ── attendee validation ──────────────────────────────────────────────────
    if not attendee_user_ids:
        raise ValidationError({"attendee_user_ids": "Required list of user ids (at least one)."})
    if len(attendee_user_ids) > 100:
        raise ValidationError({"attendee_user_ids": "Too many attendees (max 100)."})

    # Strip duplicates and the host itself.
    unique_ids = list({int(pk) for pk in attendee_user_ids if int(pk) != host_user.pk})
    if not unique_ids:
        raise ValidationError({"attendee_user_ids": "Pick at least one attendee other than yourself."})

    attendees = list(_User.objects.filter(pk__in=unique_ids))
    found_ids = {u.pk for u in attendees}
    missing = [pk for pk in unique_ids if pk not in found_ids]
    if missing:
        raise ValidationError({"attendee_user_ids": f"Unknown user(s): {missing}"})

    # ── visibility check ─────────────────────────────────────────────────────
    for u in attendees:
        if not _can_view_peer(host_user, u):
            raise PermissionDenied({"detail": f"You're not connected to user {u.pk}."})

    # ── provider ─────────────────────────────────────────────────────────────
    provider = _pick_write_provider(host_user)
    if provider is None:
        raise ValidationError({"detail": "Connect a calendar in /settings/calendars first."})

    # ── slot conflict ────────────────────────────────────────────────────────
    conflict_user = _first_busy_user([host_user, *attendees], start, end)
    if conflict_user is not None:
        who = (
            "You are"
            if conflict_user.pk == host_user.pk
            else f"{conflict_user.first_name or 'One of the attendees'} is"
        )
        raise ValidationError({
            "detail": f"{who} no longer free at that time — please pick another slot.",
            "code": "slot_conflict",
        })

    # ── meeting type ─────────────────────────────────────────────────────────
    if meeting_type_id is not None:
        try:
            mt = MeetingType.objects.get(pk=meeting_type_id, host=host_user, is_active=True)
            resolved_title = mt.name
        except MeetingType.DoesNotExist:
            raise ValidationError({
                "meeting_type_id": "Unknown or inactive meeting type.",
                "code": "meeting_type_inactive",
            })
    else:
        mt = None
        resolved_title = _default_title(attendees[0]) if len(attendees) == 1 else "Group meeting"

    final_title = (title.strip()[:200] if title and title.strip() else resolved_title)
    final_notes = (notes.strip()[:2000] if notes and notes.strip() else "")

    # ── create Booking row (before calendar call so manage URL is real) ──────
    booking = Booking(
        host=host_user,
        provider=provider.name,
        calendar_id=provider.write_calendar_id,
        visitor_name=(attendees[0].first_name or "").strip(),
        visitor_email=attendees[0].email,
        attendee_emails=[u.email for u in attendees],
        kind=Booking.Kind.ONLINE,
        title=final_title,
        notes=final_notes,
        start_at=start,
        end_at=end,
    )
    description = _description_with_manage_link(final_notes, booking)

    event = provider.create_event(
        host_user,
        calendar_id=provider.write_calendar_id,
        summary=final_title,
        description=description,
        start_iso=start.isoformat(),
        end_iso=end.isoformat(),
        time_zone=settings.TIME_ZONE,
        attendees=[u.email for u in attendees],
    )

    booking.event_id = event.get("id", "") or ""
    booking.save()

    return {
        "booking_uuid": str(booking.uuid),
        "status": "ok",
        "event": {
            "id": event.get("id"),
            "html_link": event.get("htmlLink") or event.get("webLink"),
            "meet_link": _extract_meet_link(event),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "provider": provider.name,
        },
        "invited_emails": [u.email for u in attendees],
        "manage_url": _manage_url(booking),
    }


def cancel_booking(
    host_user,
    booking_uuid,
    *,
    reason: str = "",
) -> dict[str, Any]:
    """Host-initiated cancel of a confirmed booking.

    Idempotent: a second call on an already-cancelled booking returns current
    state without re-emailing the visitor.

    Raises:
        ValidationError — booking not found, or already ended.
    """
    booking = Booking.objects.filter(uuid=booking_uuid, host=host_user).first()
    if booking is None:
        raise ValidationError({"booking_uuid": "not found"})

    # Idempotent — already cancelled → return current state.
    if booking.status == Booking.Status.CANCELLED:
        return {
            "booking_uuid": str(booking.uuid),
            "status": "cancelled",
            "cancelled_at": booking.cancelled_at.isoformat() if booking.cancelled_at else None,
            "reason": booking.cancellation_reason,
        }

    if booking.end_at < djtz.now():
        raise ValidationError({
            "detail": "booking already ended",
            "code": "already_ended",
        })

    # ── delete provider event (swallow OAuth error — mark cancelled anyway) ──
    try:
        _create_fn, delete_fn = _provider_for(booking.provider)
        if booking.event_id:
            delete_fn(
                booking.host,
                calendar_id=booking.calendar_id,
                event_id=booking.event_id,
            )
    except (GoogleOAuthError, _graph.MicrosoftOAuthError):
        logger.info(
            "cancel booking %s: host %s provider grant broken; marking cancelled anyway",
            booking.uuid,
            booking.host_id,
        )
    except (GoogleApiError, _graph.MicrosoftApiError) as exc:
        logger.info("cancel booking %s: provider refused (%s); marking cancelled", booking.uuid, exc)
    except ValueError as exc:
        logger.warning("cancel booking %s: unknown provider: %s", booking.uuid, exc)

    booking.status = Booking.Status.CANCELLED
    booking.cancelled_at = djtz.now()
    booking.cancellation_reason = reason
    booking.cancelled_by_visitor = False
    booking.save(update_fields=[
        "status", "cancelled_at", "cancellation_reason", "cancelled_by_visitor",
    ])

    _mail_visitor_host_cancellation(booking, reason)

    return {
        "booking_uuid": str(booking.uuid),
        "status": "cancelled",
        "cancelled_at": booking.cancelled_at.isoformat(),
        "reason": reason,
    }


def update_booking(
    host_user,
    booking_uuid,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Update start/end/title of a confirmed booking.

    At least one field must be provided. If start or end changes, the old
    event is deleted from the provider and a new one is created. The Booking
    row is updated in-place (uuid unchanged, manage URL stays valid).

    Raises:
        ValidationError — booking not found, nothing to update, bad times, or
                          slot conflict.
    """
    from django.contrib.auth import get_user_model
    _User = get_user_model()

    booking = Booking.objects.filter(uuid=booking_uuid, host=host_user).select_related("host").first()
    if booking is None:
        raise ValidationError({"booking_uuid": "not found"})

    if start is None and end is None and title is None:
        raise ValidationError({"detail": "nothing to update"})

    new_start = start if start is not None else booking.start_at
    new_end = end if end is not None else booking.end_at
    new_title = title.strip()[:200] if title is not None else booking.title

    time_changed = (new_start != booking.start_at) or (new_end != booking.end_at)

    if time_changed:
        if new_end <= new_start:
            raise ValidationError({"detail": "end must be after start."})
        duration = new_end - new_start
        if duration < timedelta(minutes=5):
            raise ValidationError({"detail": "Meeting must be at least 5 minutes."})
        if duration > timedelta(hours=12):
            raise ValidationError({"detail": "Meeting longer than 12 hours refused."})

        # Resolve attendee users from email list for conflict check.
        attendee_emails = list(booking.attendee_emails or [])
        attendee_users = list(_User.objects.filter(email__in=attendee_emails))
        # Attendees who are not Slotly users get skipped — we only have emails.

        conflict_user = _first_busy_user(
            [host_user, *attendee_users],
            new_start,
            new_end,
            exclude_event_id=booking.event_id,
        )
        if conflict_user is not None:
            who = (
                "You are"
                if conflict_user.pk == host_user.pk
                else f"{conflict_user.first_name or 'One of the attendees'} is"
            )
            raise ValidationError({
                "detail": f"{who} no longer free at that time — please pick another slot.",
                "code": "slot_conflict",
            })

        provider = _pick_write_provider(host_user)
        if provider is None:
            raise ValidationError({"detail": "Connect a calendar in /settings/calendars first."})

        # Delete old event from provider.
        if booking.event_id:
            try:
                provider.delete_event(
                    host_user,
                    calendar_id=booking.calendar_id,
                    event_id=booking.event_id,
                )
            except (GoogleApiError, _graph.MicrosoftApiError) as exc:
                logger.info("update_booking delete failed for %s: %s", booking.uuid, exc)

        # Create replacement event.
        description = _description_with_manage_link(booking.notes or "", booking)
        event = provider.create_event(
            host_user,
            calendar_id=provider.write_calendar_id,
            summary=new_title,
            description=description,
            start_iso=new_start.isoformat(),
            end_iso=new_end.isoformat(),
            time_zone=settings.TIME_ZONE,
            attendees=attendee_emails,
        )

        old_start = booking.start_at
        booking.event_id = event.get("id", "") or booking.event_id
        booking.start_at = new_start
        booking.end_at = new_end
        booking.title = new_title
        booking.reminded_at = None
        booking.save(update_fields=["event_id", "start_at", "end_at", "title", "reminded_at"])

        # NOTE: host-initiated reschedule — intentionally does NOT fire
        # _notify_booking_rescheduled (which is written for the visitor-initiated
        # case and would send "the visitor moved your booking" to the host).
        # A proper BOOKING_UPDATED_BY_HOST notification type is a follow-up task.

        return {
            "booking_uuid": str(booking.uuid),
            "status": "updated",
            "event": {
                "id": event.get("id"),
                "html_link": event.get("htmlLink") or event.get("webLink"),
                "meet_link": _extract_meet_link(event),
                "start": new_start.isoformat(),
                "end": new_end.isoformat(),
                "provider": provider.name,
            },
            "start": booking.start_at.isoformat(),
            "end": booking.end_at.isoformat(),
            "title": booking.title,
        }
    else:
        # Title-only change — no provider interaction needed.
        booking.title = new_title
        # Title-only edit doesn't move the time → no reminder reset. (The
        # time-change path above sets reminded_at=None.)
        booking.save(update_fields=["title"])

        return {
            "booking_uuid": str(booking.uuid),
            "status": "updated",
            "event": None,
            "start": booking.start_at.isoformat(),
            "end": booking.end_at.isoformat(),
            "title": booking.title,
        }


def create_event(
    host_user,
    *,
    start: datetime,
    end: datetime,
    title: str,
    calendar_id: str | None = None,
    attendee_emails: list[str] | None = None,
) -> dict[str, Any]:
    """Create a bare calendar event on the host's calendar (no Booking row).

    No slot conflict check — bare events may overlap existing busy time
    (the host explicitly chose this time). Max 50 attendee emails. Duration
    up to 24 h (longer than booking allows, since this is a direct calendar
    write).

    Raises:
        ValidationError — bad inputs.
        GoogleOAuthError / MicrosoftOAuthError — provider auth broken (→ 503).
        GoogleApiError   / MicrosoftApiError   — provider rejected event (→ 502).
    """
    # ── validation ───────────────────────────────────────────────────────────
    if not title or not title.strip():
        raise ValidationError({"title": "Title is required."})
    final_title = title.strip()[:200]
    if len(title.strip()) > 200:
        raise ValidationError({"title": "Title must be 200 characters or fewer."})

    if end <= start:
        raise ValidationError({"detail": "end must be after start."})
    duration = end - start
    if duration < timedelta(minutes=5):
        raise ValidationError({"detail": "Event must be at least 5 minutes."})
    if duration > timedelta(hours=24):
        raise ValidationError({"detail": "Event longer than 24 hours refused."})

    emails = attendee_emails or []
    if len(emails) > 50:
        raise ValidationError({"attendee_emails": "Too many attendees (max 50)."})

    # ── provider ─────────────────────────────────────────────────────────────
    provider = _pick_write_provider(host_user)
    if provider is None:
        raise ValidationError({"detail": "Connect a calendar in /settings/calendars first."})

    used_calendar_id = calendar_id or provider.write_calendar_id

    event = provider.create_event(
        host_user,
        calendar_id=used_calendar_id,
        summary=final_title,
        description="",
        start_iso=start.isoformat(),
        end_iso=end.isoformat(),
        time_zone=settings.TIME_ZONE,
        attendees=emails,
    )

    return {
        "status": "ok",
        "event": {
            "id": event.get("id"),
            "html_link": event.get("htmlLink") or event.get("webLink"),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "provider": provider.name,
            "calendar_id": used_calendar_id,
        },
        "invited_emails": emails,
    }


# ────────────────────────────────────────────────────────────────────────────
# Private helpers (moved from views.py; importable here by views too)
# ────────────────────────────────────────────────────────────────────────────


def _can_view_peer(caller, peer) -> bool:
    """Same visibility rule as /api/me/peer-availability: shared team OR
    accepted connection OR peer is publicly bookable."""
    from apps.connections.models import Connection as _Connection
    from apps.teams.models import Team as _Team

    if peer.pk == caller.pk:
        return True
    if _Team.objects.filter(memberships__user=caller).filter(memberships__user=peer).exists():
        return True
    if _Connection.are_connected(caller.pk, peer.pk):
        return True
    return bool(peer.share_enabled)


def _first_busy_user(users, start_dt, end_dt, *, exclude_event_id: str = ""):
    """Return the first user in ``users`` whose calendar or unavailability
    overlaps [start_dt, end_dt), or None if all are free. Mirrors the busy
    aggregation used by the availability views.

    ``exclude_event_id`` skips a specific provider event id — used by
    reschedule so the booking being moved doesn't block itself. The check
    is by provider `uid` (matches how apps.calendars stores it), so this
    only helps when the ICS sync has pulled the event's uid; otherwise
    the exclusion is a no-op (rare, tolerable).
    """
    from django.db.models import Q as _Q
    from apps.availability.models import Unavailability as _Un
    from apps.calendars.models import CalendarEvent as _CE

    ids = [u.pk for u in users]
    busy_ids = set()
    events_qs = _CE.objects.filter(
        _Q(transp=_CE.Transparency.OPAQUE) | _Q(is_all_day=True),
        calendar__owner_id__in=ids,
        calendar__include_in_busy=True,
        dtstart__lt=end_dt,
        dtend__gt=start_dt,
    ).exclude(status=_CE.Status.CANCELLED)
    if exclude_event_id:
        events_qs = events_qs.exclude(uid=exclude_event_id)
    for ev in events_qs.values("calendar__owner_id")[:1]:
        busy_ids.add(ev["calendar__owner_id"])
    if len(busy_ids) < len(ids):
        for u in _Un.objects.filter(
            user_id__in=ids,
            starts_at__lt=end_dt,
            ends_at__gt=start_dt,
        ).values("user_id")[:1]:
            busy_ids.add(u["user_id"])
    for u in users:
        if u.pk in busy_ids:
            return u
    return None


def _default_title(peer) -> str:
    name = f"{peer.first_name} {peer.last_name}".strip() or peer.email
    return f"Meeting with {name}"


def _manage_url(booking: Booking) -> str:
    return f"{settings.FRONTEND_BASE_URL.rstrip('/')}/b/{booking.uuid}"


def _description_with_manage_link(base: str, booking: Booking) -> str:
    """Append a "Manage / Cancel this booking" footer to the visitor-
    facing event description. Kept as the last block so provider clients
    that truncate long descriptions still show the meeting context first."""
    footer = f"\n\n— Manage or cancel this booking: {_manage_url(booking)}"
    return (base or "") + footer


def _extract_meet_link(event: dict) -> str:
    """Pull the video-call URL out of a provider event dict, or "".
    - Google: top-level `hangoutLink`; fallback to conferenceData
      entryPoints where entryPointType == "video".
    - Microsoft Graph: `onlineMeeting.joinUrl`.
    """
    link = event.get("hangoutLink")
    if link:
        return link
    conf = event.get("conferenceData") or {}
    for ep in conf.get("entryPoints", []) or []:
        if ep.get("entryPointType") == "video" and ep.get("uri"):
            return ep["uri"]
    online = event.get("onlineMeeting") or {}
    if online.get("joinUrl"):
        return online["joinUrl"]
    return ""


def _notify_booking_rescheduled(booking: Booking, old_start) -> None:
    """In-app + email the host that the visitor moved their booking."""
    from apps.notifications.dispatch import notify as _notify
    from apps.notifications.models import Notification as _N

    _notify(
        booking.host,
        _N.Type.BOOKING_RESCHEDULED_BY_VISITOR,
        {
            "visitor_name": booking.visitor_name,
            "visitor_email": booking.visitor_email,
            "from_when": old_start.strftime("%a %d %b %H:%M"),
            "to_when": booking.start_at.strftime("%a %d %b %H:%M"),
        },
    )


def _provider_for(name: str):
    """Look up create/delete helpers by provider name — used from the
    cancel endpoint which knows the provider from the Booking row.
    Returns a (create_fn, delete_fn) tuple resolved lazily through module
    attributes so patches on views.create_calendar_event still work."""
    if name == "google":
        from . import google_client as _gc
        return _gc.create_calendar_event, _gc.delete_calendar_event
    if name == "microsoft":
        from . import graph_client as _ms
        return _ms.create_calendar_event, _ms.delete_calendar_event
    raise ValueError(f"Unknown provider: {name}")


def _mail_visitor_host_cancellation(booking: Booking, reason: str) -> None:
    """Branded HTML/text email to the visitor when the host cancels."""
    from django.conf import settings as _settings
    from django.core.mail import EmailMultiAlternatives

    from apps.notifications.emails import blockquote, html_shell, kv_rows, paragraph

    when = booking.start_at.strftime("%A %d %B, %H:%M")
    host_name = f"{booking.host.first_name} {booking.host.last_name}".strip() or booking.host.email
    subject = f"{host_name} cancelled your meeting on {when}"

    text_body = (
        f"Hi {booking.visitor_name or 'there'},\n\n"
        f"{host_name} had to cancel your meeting on {when}."
    )
    if reason:
        text_body += f"\n\nNote from {host_name}:\n{reason}"

    intro = paragraph(f"Hi {booking.visitor_name or 'there'},")
    intro += paragraph(f"{host_name} had to cancel your meeting.")
    intro += kv_rows([("When", when), ("Where", booking.location)])
    if reason:
        intro += paragraph(f"Note from {host_name}:")
        intro += blockquote(reason)

    html_body = html_shell(
        title=f"{host_name} cancelled your meeting",
        intro_html=intro,
        cta_label="Pick another slot",
        cta_url=f"{_settings.FRONTEND_BASE_URL.rstrip('/')}/u/{booking.host.share_token}",
    )

    try:
        msg = EmailMultiAlternatives(
            subject=f"[Slotly] {subject}",
            body=text_body,
            from_email=_settings.DEFAULT_FROM_EMAIL,
            to=[booking.visitor_email],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("host-cancel mail to visitor failed for %s: %s", booking.uuid, exc)
