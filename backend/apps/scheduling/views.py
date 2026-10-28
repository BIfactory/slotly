"""
M18a — Google OAuth connect endpoints + status/disconnect API.

Flow:
  1. Frontend opens /api/oauth/google/start in a new tab.
  2. We mint a signed state (binds the user's PK so the callback knows whose
     row to write), and 302 the browser to Google's consent screen.
  3. Google bounces back to /api/oauth/google/callback with `code` + `state`.
  4. We verify the state, exchange the code for tokens, fetch userinfo,
     persist the GoogleAccount, then 302 the browser to the frontend with
     a status flag.

The status + disconnect endpoints are plain JSON, used by /settings/integrations.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils import timezone as djtz
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from . import graph_client as _graph
from .google_client import (
    GoogleApiError,
    GoogleNotConfigured,
    GoogleOAuthError,
    authorize_url,
    create_calendar_event,
    delete_calendar_event,
    exchange_code,
    fetch_userinfo,
    list_writable_calendars,
)
from .models import Booking, BookingRequest, GoogleAccount, MeetingType, MicrosoftAccount
from .security import encrypt
from . import services as _svc
from .services import (
    _can_view_peer,
    _description_with_manage_link,
    _default_title,
    _extract_meet_link,
    _first_busy_user,
    _manage_url,
    _notify_booking_rescheduled,
)

logger = logging.getLogger(__name__)

_STATE_SALT = "scheduling.google_oauth.state"
_STATE_MAX_AGE = 60 * 10  # 10 minutes is plenty for a consent screen round-trip


def _signer() -> TimestampSigner:
    return TimestampSigner(salt=_STATE_SALT)


def _frontend_return(status: str, provider: str = "google", **extra: str) -> HttpResponseRedirect:
    """Bounce back to the frontend after the OAuth round-trip. The `anon`
    flag in `extra` picks the return path: anon=1 → /profile (SSO users
    land on their fresh account), anon=0 or missing → /settings/integrations
    (users mid-config get their success banner). `provider` names the
    integration in the query string ("google" or "microsoft") so the
    frontend can render provider-specific banners."""
    from urllib.parse import urlencode

    qs = urlencode({provider: status, **extra})
    path = "/profile" if extra.get("anon") == "1" else settings.GOOGLE_OAUTH_FRONTEND_RETURN
    base = settings.FRONTEND_BASE_URL.rstrip("/") + path
    return HttpResponseRedirect(f"{base}?{qs}")


class OAuthStartView(APIView):
    """
    GET /api/oauth/google/start[?anon=1]

    Two modes packed into one endpoint:

    - Authenticated call → link Google to the current user (existing "connect
      Calendar" flow used from /settings/integrations).
    - Anonymous call with ?anon=1 → "Sign in with Google": the callback
      creates or finds the User by verified Google email, logs them in, and
      stores the GoogleAccount in the same round-trip. One consent screen,
      no password step, no separate connect step.

    The state string carries the flow discriminator so the callback can
    branch without needing another cookie: ``user:<pk>`` for link-mode,
    ``anon`` for SSO.
    """

    permission_classes = [AllowAny]

    def get(self, request: Request) -> Any:
        anon = request.query_params.get("anon") == "1"
        if not anon and not request.user.is_authenticated:
            # Non-anon caller must be logged in; otherwise the callback would
            # have no user to attach the GoogleAccount to.
            return Response(
                {"detail": "Sign in first, or use ?anon=1 for SSO signup."},
                status=401,
            )
        try:
            state = _signer().sign("anon" if anon else f"user:{request.user.pk}")
            url = authorize_url(state)
        except GoogleNotConfigured:
            return Response(
                {"detail": "Google Calendar is not configured on this server."},
                status=503,
            )
        return HttpResponseRedirect(url)


@api_view(["GET"])
@permission_classes([])  # Public — Google calls this on the user's behalf.
def oauth_callback(request: HttpRequest) -> HttpResponseRedirect:
    """
    GET /api/oauth/google/callback?code=...&state=...&error=...
    Unauthenticated (Google can't send our session cookie); the signed state
    carries the user id we want to write to.
    """
    err = request.GET.get("error")
    if err:
        # User declined consent, or Google rejected. Surface to frontend.
        return _frontend_return("error", reason=err[:64])

    code = request.GET.get("code", "")
    state = request.GET.get("state", "")
    if not code or not state:
        return _frontend_return("error", reason="missing_code_or_state")

    try:
        unsigned = _signer().unsign(state, max_age=_STATE_MAX_AGE)
    except SignatureExpired:
        return _frontend_return("error", reason="state_expired")
    except BadSignature:
        return _frontend_return("error", reason="state_invalid")

    from apps.accounts.models import User  # local import — circular-safe

    # Determine the flow BEFORE exchanging the code so a malformed state
    # doesn't burn the authorization code (single-use, expires quickly).
    if unsigned == "anon":
        anon = True
        user = None
    elif unsigned.startswith("user:"):
        anon = False
        try:
            user_pk = int(unsigned[5:])
            user = User.objects.get(pk=user_pk)
        except (ValueError, User.DoesNotExist):
            return _frontend_return("error", reason="user_missing")
    else:
        return _frontend_return("error", reason="state_malformed")

    try:
        token_payload = exchange_code(code)
        userinfo = fetch_userinfo(token_payload["access_token"])
    except GoogleNotConfigured:
        return _frontend_return("error", reason="not_configured")
    except (GoogleOAuthError, KeyError) as exc:
        logger.info("Google OAuth callback failed (anon=%s): %s", anon, exc)
        return _frontend_return("error", reason="exchange_failed")

    google_email = (userinfo.get("email") or "").strip().lower()
    if not google_email:
        return _frontend_return("error", reason="no_email")

    if anon:
        # SSO signup or signin. Look up (or create) the Slotly user by the
        # verified Google email, mark the allauth EmailAddress row verified,
        # log in via the normal Django session backend, then fall through to
        # the shared GoogleAccount upsert below.
        from django.contrib.auth import login as _django_login
        from django.db import transaction

        with transaction.atomic():
            user, created = User.objects.get_or_create(
                email=google_email,
                defaults={
                    "first_name": (userinfo.get("given_name") or "")[:80],
                    "last_name": (userinfo.get("family_name") or "")[:80],
                },
            )
            if created:
                # SSO users don't have a Slotly password — they always come
                # back through Google. They can still set one via /auth/forgot
                # if they want a password-based fallback.
                user.set_unusable_password()
                user.save(update_fields=["password"])
            # allauth gates a lot of behavior on EmailAddress.verified; Google
            # already verified the mailbox for us, so mirror that in the row.
            try:
                from allauth.account.models import EmailAddress as _EA
                _EA.objects.update_or_create(
                    user=user,
                    email=google_email,
                    defaults={"verified": True, "primary": True},
                )
            except ImportError:  # allauth not installed — safe to skip
                pass
        # Attach the ModelBackend so django.contrib.auth.login doesn't need to
        # guess between our two configured auth backends.
        user.backend = "django.contrib.auth.backends.ModelBackend"
        _django_login(request, user)

    access_token = token_payload["access_token"]
    refresh_token = token_payload.get("refresh_token")
    if not refresh_token:
        # Without offline+prompt=consent we wouldn't get one. Defensive: if a
        # second connect somehow lacks it, keep the previously stored refresh
        # token so we don't lock the user out.
        existing = GoogleAccount.objects.filter(user=user).only("refresh_token_encrypted").first()
        if existing is None:
            return _frontend_return("error", reason="no_refresh_token")
        refresh_encrypted = existing.refresh_token_encrypted
    else:
        refresh_encrypted = encrypt(refresh_token)

    expires_in = int(token_payload.get("expires_in", 3600))
    expires_at = djtz.now() + timedelta(seconds=expires_in)

    GoogleAccount.objects.update_or_create(
        user=user,
        defaults={
            "google_email": google_email[:254],
            "access_token_encrypted": encrypt(access_token),
            "refresh_token_encrypted": refresh_encrypted,
            "expires_at": expires_at,
            "scope": token_payload.get("scope", "")[:500],
        },
    )
    # Anon flows go to /profile (welcome / next-step) rather than
    # /settings/integrations, which is meant for users who were mid-config.
    return _frontend_return("connected", email=google_email, anon="1" if anon else "0")


class GoogleAccountStatusView(APIView):
    """
    GET    /api/google-account   — returns {connected, google_email?, write_calendar_id?}
    PATCH  /api/google-account   — body {write_calendar_id} — set the write target
    DELETE /api/google-account   — disconnects (deletes the row; doesn't
                                    revoke the grant Google-side, user can
                                    do that from myaccount.google.com)
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        try:
            account = GoogleAccount.objects.get(user=request.user)
        except GoogleAccount.DoesNotExist:
            return Response({"connected": False})
        return Response({
            "connected": True,
            "google_email": account.google_email,
            "write_calendar_id": account.write_calendar_id,
        })

    def patch(self, request: Request) -> Response:
        try:
            account = GoogleAccount.objects.get(user=request.user)
        except GoogleAccount.DoesNotExist:
            return Response({"detail": "Google Calendar is not connected."}, status=404)
        raw = request.data.get("write_calendar_id")
        if not isinstance(raw, str) or not raw.strip():
            return Response({"write_calendar_id": "Required non-empty string."}, status=400)
        # Validate that the chosen ID is one of the user's writable calendars —
        # otherwise events.insert would 404 later with a confusing message.
        try:
            calendars = list_writable_calendars(request.user)
        except (GoogleOAuthError, GoogleApiError) as exc:
            return Response({"detail": f"Google refused: {exc}"}, status=502)
        allowed = {c["id"] for c in calendars}
        if raw not in allowed:
            return Response(
                {"write_calendar_id": "Pick a calendar you have writer access to."},
                status=400,
            )
        account.write_calendar_id = raw
        account.save(update_fields=["write_calendar_id", "updated_at"])
        return Response({
            "connected": True,
            "google_email": account.google_email,
            "write_calendar_id": account.write_calendar_id,
        })

    def delete(self, request: Request) -> Response:
        GoogleAccount.objects.filter(user=request.user).delete()
        return Response(status=204)


class WritableCalendarsView(APIView):
    """
    GET /api/google-account/writable-calendars — the connected user's
    calendars the OAuth grant can write into (owner + writer). Feeds the
    settings dropdown for picking the write target.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        try:
            GoogleAccount.objects.get(user=request.user)
        except GoogleAccount.DoesNotExist:
            return Response({"detail": "Google Calendar is not connected."}, status=404)
        try:
            items = list_writable_calendars(request.user)
        except GoogleOAuthError as exc:
            return Response({"detail": f"Reconnect needed: {exc}"}, status=401)
        except GoogleApiError as exc:
            # 403 here means the granted scopes don't cover calendarList
            # (typically an older grant made before we added calendar.readonly).
            # Surface a specific error the frontend can act on — offer a
            # reconnect button rather than a generic "Google refused".
            if exc.status == 403:
                return Response(
                    {
                        "detail": "Reconnect Google — the app now needs permission to list your calendars.",
                        "reconnect_required": True,
                    },
                    status=401,
                )
            return Response({"detail": f"Google refused: {exc}"}, status=502)
        return Response({"calendars": items})


# ---------------------------------------------------------------------------
# Microsoft 365 / Outlook — mirror of the Google endpoints above.
# ---------------------------------------------------------------------------


_MS_STATE_SALT = "scheduling.microsoft_oauth.state"


def _ms_signer() -> TimestampSigner:
    return TimestampSigner(salt=_MS_STATE_SALT)


class MicrosoftOAuthStartView(APIView):
    """GET /api/oauth/microsoft/start[?anon=1] — see the Google twin for the
    two-mode explanation. Anon flow uses Microsoft's userinfo email to
    create / find the Slotly user."""

    permission_classes = [AllowAny]

    def get(self, request: Request) -> Any:
        anon = request.query_params.get("anon") == "1"
        if not anon and not request.user.is_authenticated:
            return Response(
                {"detail": "Sign in first, or use ?anon=1 for SSO signup."},
                status=401,
            )
        try:
            state = _ms_signer().sign("anon" if anon else f"user:{request.user.pk}")
            url = _graph.authorize_url(state)
        except _graph.MicrosoftNotConfigured:
            return Response(
                {"detail": "Microsoft 365 is not configured on this server."},
                status=503,
            )
        return HttpResponseRedirect(url)


@api_view(["GET"])
@permission_classes([])
def microsoft_oauth_callback(request: HttpRequest) -> HttpResponseRedirect:
    err = request.GET.get("error")
    if err:
        return _frontend_return("error", provider="microsoft", reason=err[:64])

    code = request.GET.get("code", "")
    state = request.GET.get("state", "")
    if not code or not state:
        return _frontend_return("error", provider="microsoft", reason="missing_code_or_state")

    try:
        unsigned = _ms_signer().unsign(state, max_age=_STATE_MAX_AGE)
    except SignatureExpired:
        return _frontend_return("error", provider="microsoft", reason="state_expired")
    except BadSignature:
        return _frontend_return("error", provider="microsoft", reason="state_invalid")

    from apps.accounts.models import User

    if unsigned == "anon":
        anon = True
        user = None
    elif unsigned.startswith("user:"):
        anon = False
        try:
            user_pk = int(unsigned[5:])
            user = User.objects.get(pk=user_pk)
        except (ValueError, User.DoesNotExist):
            return _frontend_return("error", provider="microsoft", reason="user_missing")
    else:
        return _frontend_return("error", provider="microsoft", reason="state_malformed")

    try:
        token_payload = _graph.exchange_code(code)
        userinfo = _graph.fetch_userinfo(token_payload["access_token"])
    except _graph.MicrosoftNotConfigured:
        return _frontend_return("error", provider="microsoft", reason="not_configured")
    except (_graph.MicrosoftOAuthError, KeyError) as exc:
        logger.info("MS OAuth callback failed (anon=%s): %s", anon, exc)
        return _frontend_return("error", provider="microsoft", reason="exchange_failed")

    ms_email = ((userinfo.get("mail") or userinfo.get("userPrincipalName") or "").strip().lower())
    if not ms_email:
        return _frontend_return("error", provider="microsoft", reason="no_email")

    if anon:
        from django.contrib.auth import login as _django_login
        from django.db import transaction

        with transaction.atomic():
            user, created = User.objects.get_or_create(
                email=ms_email,
                defaults={
                    "first_name": (userinfo.get("givenName") or "")[:80],
                    "last_name": (userinfo.get("surname") or "")[:80],
                },
            )
            if created:
                user.set_unusable_password()
                user.save(update_fields=["password"])
            try:
                from allauth.account.models import EmailAddress as _EA
                _EA.objects.update_or_create(
                    user=user,
                    email=ms_email,
                    defaults={"verified": True, "primary": True},
                )
            except ImportError:
                pass
        user.backend = "django.contrib.auth.backends.ModelBackend"
        _django_login(request, user)

    access_token = token_payload["access_token"]
    refresh_token = token_payload.get("refresh_token")
    if not refresh_token:
        existing = MicrosoftAccount.objects.filter(user=user).only("refresh_token_encrypted").first()
        if existing is None:
            return _frontend_return("error", provider="microsoft", reason="no_refresh_token")
        refresh_encrypted = existing.refresh_token_encrypted
    else:
        refresh_encrypted = encrypt(refresh_token)

    expires_in = int(token_payload.get("expires_in", 3600))
    expires_at = djtz.now() + timedelta(seconds=expires_in)

    MicrosoftAccount.objects.update_or_create(
        user=user,
        defaults={
            "microsoft_email": ms_email[:254],
            "access_token_encrypted": encrypt(access_token),
            "refresh_token_encrypted": refresh_encrypted,
            "expires_at": expires_at,
            "scope": token_payload.get("scope", "")[:500],
        },
    )
    return _frontend_return(
        "connected",
        provider="microsoft",
        email=ms_email,
        anon="1" if anon else "0",
    )


class MicrosoftAccountStatusView(APIView):
    """GET/PATCH/DELETE /api/microsoft-account — parallel of GoogleAccountStatusView."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        try:
            account = MicrosoftAccount.objects.get(user=request.user)
        except MicrosoftAccount.DoesNotExist:
            return Response({"connected": False})
        return Response({
            "connected": True,
            "microsoft_email": account.microsoft_email,
            "write_calendar_id": account.write_calendar_id,
        })

    def patch(self, request: Request) -> Response:
        try:
            account = MicrosoftAccount.objects.get(user=request.user)
        except MicrosoftAccount.DoesNotExist:
            return Response({"detail": "Microsoft is not connected."}, status=404)
        raw = request.data.get("write_calendar_id")
        # Empty string is valid — Graph treats /me/events as "primary calendar".
        if not isinstance(raw, str):
            return Response({"write_calendar_id": "Required string."}, status=400)
        try:
            calendars = _graph.list_writable_calendars(request.user)
        except (_graph.MicrosoftOAuthError, _graph.MicrosoftApiError) as exc:
            return Response({"detail": f"Microsoft refused: {exc}"}, status=502)
        allowed = {c["id"] for c in calendars}
        if raw and raw not in allowed:
            return Response(
                {"write_calendar_id": "Pick a calendar you have edit access to."},
                status=400,
            )
        account.write_calendar_id = raw
        account.save(update_fields=["write_calendar_id", "updated_at"])
        return Response({
            "connected": True,
            "microsoft_email": account.microsoft_email,
            "write_calendar_id": account.write_calendar_id,
        })

    def delete(self, request: Request) -> Response:
        MicrosoftAccount.objects.filter(user=request.user).delete()
        return Response(status=204)


class MicrosoftWritableCalendarsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        try:
            MicrosoftAccount.objects.get(user=request.user)
        except MicrosoftAccount.DoesNotExist:
            return Response({"detail": "Microsoft is not connected."}, status=404)
        try:
            items = _graph.list_writable_calendars(request.user)
        except _graph.MicrosoftOAuthError as exc:
            return Response({"detail": f"Reconnect needed: {exc}"}, status=401)
        except _graph.MicrosoftApiError as exc:
            return Response({"detail": f"Microsoft refused: {exc}"}, status=502)
        return Response({"calendars": items})


# ---------------------------------------------------------------------------
# Meeting creation
# ---------------------------------------------------------------------------


class MeetingCreateView(APIView):
    """
    POST /api/meetings
    body: {attendee_user_ids: [int, ...], start, end, title?, notes?}

    Creates an event on the caller's write_calendar_id (default: primary)
    and invites everyone in ``attendee_user_ids`` by email. One attendee is
    the /people/<id> peer flow; many attendees is the /search group flow.
    Legacy ``peer_user_id`` (single integer) is still accepted so an in-
    flight call from an older frontend build doesn't hard-fail.

    Before insert we re-check that *everyone* — the host + all attendees —
    is still free in [start, end]. A slot that looked shared could have
    been consumed by an ICS sync between the last search and this click.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        from django.conf import settings as _settings

        body = request.data or {}

        # Accept the new plural or the legacy singular field. Normalise
        # to a list of ints; deduplicate; strip self if present.
        raw_ids = body.get("attendee_user_ids")
        if raw_ids is None and "peer_user_id" in body:
            raw_ids = [body["peer_user_id"]]
        if not isinstance(raw_ids, list) or not raw_ids:
            return Response(
                {"attendee_user_ids": "Required list of user ids (at least one)."},
                status=400,
            )
        try:
            attendee_ids = list({int(x) for x in raw_ids if int(x) != request.user.pk})
        except (TypeError, ValueError):
            return Response(
                {"attendee_user_ids": "Must contain integer user ids."},
                status=400,
            )
        if not attendee_ids:
            return Response(
                {"attendee_user_ids": "Pick at least one attendee other than yourself."},
                status=400,
            )
        if len(attendee_ids) > 100:
            return Response(
                {"attendee_user_ids": "Too many attendees (max 100)."},
                status=400,
            )

        try:
            start_dt = _parse_iso_dt(body.get("start", ""))
            end_dt = _parse_iso_dt(body.get("end", ""))
        except ValueError:
            return Response({"detail": "start/end must be ISO 8601 datetimes."}, status=400)
        if end_dt <= start_dt:
            return Response({"detail": "end must be after start."}, status=400)
        if (end_dt - start_dt) > timedelta(hours=12):
            return Response({"detail": "Meeting longer than 12 hours refused."}, status=400)

        from apps.accounts.models import User as _User
        attendees = list(_User.objects.filter(pk__in=attendee_ids))
        found_ids = {u.pk for u in attendees}
        missing = [pk for pk in attendee_ids if pk not in found_ids]
        if missing:
            return Response(
                {"attendee_user_ids": f"Unknown user(s): {missing}"},
                status=404,
            )

        # Visibility check — caller must be able to see each attendee
        # (shared team, connection, or public share). Fail closed if any
        # attendee is out of reach.
        for u in attendees:
            if not _can_view_peer(request.user, u):
                return Response(
                    {"detail": f"You're not connected to user {u.pk}."},
                    status=403,
                )

        provider = _pick_write_provider(request.user)
        if provider is None:
            return Response(
                {"detail": "Connect a calendar in /settings/calendars first."},
                status=409,
            )

        # Re-check availability for everyone — one last sweep before insert.
        conflict_user = _first_busy_user([request.user, *attendees], start_dt, end_dt)
        if conflict_user is not None:
            who = (
                "You are"
                if conflict_user.pk == request.user.pk
                else f"{conflict_user.first_name or 'One of the attendees'} is"
            )
            return Response(
                {"detail": f"{who} no longer free at that time — please pick another slot."},
                status=409,
            )

        default_title = _default_title(attendees[0]) if len(attendees) == 1 else "Group meeting"
        title = _clean_text(body.get("title"), fallback=default_title, maxlen=200)
        notes = _clean_text(body.get("notes"), fallback="", maxlen=2000)

        # Create the Booking row up front — same pattern as public flow —
        # so the manage-URL footer we put in the event description points
        # at a real row, and so this booking shows up on the host's
        # /bookings Confirmed tab alongside public bookings.
        booking = Booking(
            host=request.user,
            provider=provider.name,
            calendar_id=provider.write_calendar_id,
            visitor_name=(attendees[0].first_name or "").strip(),
            visitor_email=attendees[0].email,
            attendee_emails=[u.email for u in attendees],
            kind=Booking.Kind.ONLINE,
            title=title,
            notes=notes,
            start_at=start_dt,
            end_at=end_dt,
        )
        description = _description_with_manage_link(notes, booking)

        try:
            event = provider.create_event(
                request.user,
                calendar_id=provider.write_calendar_id,
                summary=title,
                description=description,
                start_iso=start_dt.isoformat(),
                end_iso=end_dt.isoformat(),
                time_zone=_settings.TIME_ZONE,
                attendees=[u.email for u in attendees],
            )
        except (GoogleOAuthError, _graph.MicrosoftOAuthError) as exc:
            return Response({"detail": f"Reconnect calendar: {exc}"}, status=401)
        except (GoogleApiError, _graph.MicrosoftApiError) as exc:
            return Response({"detail": f"Calendar refused: {exc}"}, status=502)

        booking.event_id = event.get("id", "") or ""
        booking.save()

        return Response({
            "ok": True,
            "event": {
                "id": event.get("id"),
                # htmlLink for Google, webLink for Graph — surface whichever
                # the provider returned so the frontend can link to the event.
                "html_link": event.get("htmlLink") or event.get("webLink"),
                "meet_link": _extract_meet_link(event),
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "provider": provider.name,
            },
            "manage_url": _manage_url(booking),
        }, status=201)


class PublicMeetingCreateView(APIView):
    """
    POST /api/public/meetings/<token>
    body: {visitor_name, visitor_email, start, end, title?, notes?, hp?}

    Unauthenticated: someone with the public share link books a slot in the
    host's calendar. The host must have Google connected. Rate-limited per
    token and per IP. A honeypot field (`hp`) must be empty — filled means
    a bot filled every text input and we drop silently.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request: Request, token) -> Response:
        from django.conf import settings as _settings
        from apps.accounts.models import User as _User

        body = request.data or {}

        # Honeypot: bots typically fill every input. Return 204 so scripts
        # can't distinguish "spam blocked" from "success" and keep guessing.
        if (body.get("hp") or "").strip():
            return Response(status=204)

        user = _User.objects.filter(share_token=token, share_enabled=True).first()
        if user is None:
            return Response(status=404)

        # Rate limit — burst protection. Two axes:
        #   token bucket: caps abuse of one link (host's problem)
        #   IP bucket:    caps a single client hammering many links
        # Redis-backed cache (see settings.CACHES).
        from django.core.cache import cache as _cache
        minute = int(djtz.now().timestamp() // 60)
        ip = _client_ip(request)
        for key, limit in (
            (f"pubmeet:t:{token}:{minute}", 10),
            (f"pubmeet:ip:{ip}:{minute}", 20),
        ):
            count = _cache.get(key) or 0
            if count >= limit:
                return Response({"detail": "Too many requests. Please wait a minute."}, status=429)
            _cache.set(key, count + 1, timeout=120)

        visitor_name = _clean_text(body.get("visitor_name"), fallback="", maxlen=120)
        visitor_email = (body.get("visitor_email") or "").strip().lower()
        if not visitor_name:
            return Response({"visitor_name": "Please enter your name."}, status=400)
        if not _looks_like_email(visitor_email):
            return Response({"visitor_email": "Please enter a valid email."}, status=400)

        # If the visitor booked through a MeetingType card, the type
        # dictates duration + kind + default location. This locks the
        # server-authoritative version so a manipulated client can't
        # sneak in a longer slot or the wrong kind.
        meeting_type: MeetingType | None = None
        custom_answers: dict = {}
        type_slug = body.get("type_slug")
        if type_slug:
            meeting_type = MeetingType.objects.filter(
                host=user, slug=type_slug, is_active=True,
            ).first()
            if meeting_type is None:
                return Response({"type_slug": "Unknown or inactive meeting type."}, status=400)
            kind = meeting_type.kind
            location = _clean_text(body.get("location"), fallback=meeting_type.location, maxlen=300)

            # Validate custom answers against the type's question schema.
            raw_answers = body.get("custom_answers") or {}
            if not isinstance(raw_answers, dict):
                return Response({"custom_answers": "Must be an object."}, status=400)
            for q in meeting_type.questions or []:
                qid = q.get("id")
                if not qid:
                    continue
                ans = str(raw_answers.get(qid) or "").strip()
                if q.get("required") and not ans:
                    return Response(
                        {"custom_answers": f"'{q.get('label', 'field')}' is required."},
                        status=400,
                    )
                if ans:
                    if q.get("kind") == "select" and ans not in (q.get("options") or []):
                        return Response(
                            {"custom_answers": f"'{q.get('label', 'field')}': invalid option."},
                            status=400,
                        )
                    custom_answers[qid] = ans[:2000]
        else:
            # `kind` splits the flow. Default "online" preserves the old
            # behavior. "physical" opens a BookingRequest for the host to
            # approve — the visitor gets a "waiting for approval" screen and
            # no calendar event is created yet.
            kind = (body.get("kind") or "online").strip().lower()
            if kind not in ("online", "physical"):
                return Response({"kind": "Must be 'online' or 'physical'."}, status=400)
            location = _clean_text(body.get("location"), fallback="", maxlen=300)
        if kind == "physical" and not location:
            return Response({"location": "Please tell the host where to meet."}, status=400)

        try:
            start_dt = _parse_iso_dt(body.get("start", ""))
            end_dt = _parse_iso_dt(body.get("end", ""))
        except ValueError:
            return Response({"detail": "start/end must be ISO 8601 datetimes."}, status=400)
        if end_dt <= start_dt:
            return Response({"detail": "end must be after start."}, status=400)
        # If a meeting type is in play, force the end to match its duration —
        # visitor's client shouldn't be allowed to stretch a "15-min chat"
        # into an hour.
        if meeting_type is not None:
            end_dt = start_dt + timedelta(minutes=meeting_type.duration_min)
        if (end_dt - start_dt) > timedelta(hours=12):
            return Response({"detail": "Meeting longer than 12 hours refused."}, status=400)
        # Don't book into the past (with 2-min grace for clock skew).
        if end_dt < djtz.now() - timedelta(minutes=2):
            return Response({"detail": "Cannot book a slot in the past."}, status=400)

        # Physical meetings never touch the calendar directly — they go into
        # a pending BookingRequest. Host approves later, which triggers the
        # calendar event insertion (and shifts the visitor's slot to busy).
        if kind == "physical":
            default_title = f"Meeting with {visitor_name}"
            title = _clean_text(body.get("title"), fallback=default_title, maxlen=200)
            notes = _clean_text(body.get("notes"), fallback="", maxlen=2000)
            req = BookingRequest.objects.create(
                host=user,
                visitor_name=visitor_name,
                visitor_email=visitor_email,
                kind=BookingRequest.Kind.PHYSICAL,
                start_at=start_dt,
                end_at=end_dt,
                title=title,
                notes=notes,
                location=location,
            )
            _notify_booking_request(user, req)
            return Response({
                "ok": True,
                "pending": True,
                "request_id": req.pk,
            }, status=202)

        provider = _pick_write_provider(user)
        if provider is None:
            return Response(
                {"detail": "The host hasn't set up direct booking yet."},
                status=409,
            )

        conflict_user = _first_busy_user([user], start_dt, end_dt)
        if conflict_user is not None:
            return Response(
                {"detail": "That slot is no longer free — please pick another time."},
                status=409,
            )

        default_title = (
            meeting_type.name
            if meeting_type is not None
            else f"Meeting with {visitor_name}"
        )
        title = _clean_text(body.get("title"), fallback=default_title, maxlen=200)
        notes_from_visitor = _clean_text(body.get("notes"), fallback="", maxlen=2000)
        base_description = f"Booked via your Slotly public link by {visitor_name} <{visitor_email}>."
        if meeting_type is not None and meeting_type.description:
            base_description += f"\n\n{meeting_type.description}"
        if custom_answers and meeting_type is not None:
            # Include Q&A in the calendar event body so the host sees the
            # answers alongside the meeting details.
            answered_block = _render_answers_block(meeting_type.questions or [], custom_answers)
            if answered_block:
                base_description += f"\n\n{answered_block}"
        if notes_from_visitor:
            base_description += f"\n\nNote:\n{notes_from_visitor}"

        # Create the Booking row up front (uuid is auto-generated) so the
        # manage-URL footer we add to the event description points at a
        # real row. Event id is filled after the calendar insert succeeds.
        booking = Booking(
            host=user,
            provider=provider.name,
            calendar_id=provider.write_calendar_id,
            visitor_name=visitor_name,
            visitor_email=visitor_email,
            attendee_emails=[visitor_email],
            kind=Booking.Kind.ONLINE if (meeting_type is None or meeting_type.kind == MeetingType.Kind.ONLINE) else Booking.Kind.PHYSICAL,
            title=title,
            location=location if kind == "physical" else "",
            notes=notes_from_visitor,
            custom_answers=custom_answers,
            start_at=start_dt,
            end_at=end_dt,
        )
        description = _description_with_manage_link(base_description, booking)

        try:
            event = provider.create_event(
                user,
                calendar_id=provider.write_calendar_id,
                summary=title,
                description=description,
                start_iso=start_dt.isoformat(),
                end_iso=end_dt.isoformat(),
                time_zone=_settings.TIME_ZONE,
                attendees=[visitor_email],
            )
        except (GoogleOAuthError, _graph.MicrosoftOAuthError):
            return Response(
                {"detail": "The host's calendar connection needs a refresh — try again later."},
                status=502,
            )
        except (GoogleApiError, _graph.MicrosoftApiError) as exc:
            logger.info("public booking failed for user %s (%s): %s", user.pk, provider.name, exc)
            return Response(
                {"detail": "Couldn't create the event. Please try again."},
                status=502,
            )

        booking.event_id = event.get("id", "") or ""
        booking.save()

        return Response({
            "ok": True,
            "event": {
                "id": event.get("id"),
                "meet_link": _extract_meet_link(event),
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "provider": provider.name,
            },
            "manage_url": _manage_url(booking),
            # Empty string when the picked meeting type has no redirect
            # configured; frontend falls back to the inline success block.
            "redirect_url": (meeting_type.redirect_url if meeting_type else ""),
        }, status=201)


# ---------------------------------------------------------------------------
# Visitor-side booking management — /b/<uuid> lets whoever booked a meeting
# view it and cancel it without needing a Slotly account. Access control is
# by unguessable uuid (128 bits of entropy); no signature needed since a
# leaked link only exposes one visitor's own booking.
# ---------------------------------------------------------------------------


class PublicBookingManageView(APIView):
    """
    GET  /api/public/bookings/<uuid> — booking details + host availability
         (so the manage page can render a Reschedule calendar without a
         second round-trip).
    POST /api/public/bookings/<uuid> body: {reason?} — cancel it.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request: Request, uuid_) -> Response:
        booking = _get_active_booking(uuid_)
        if booking is None:
            return Response(status=404)
        data = _serialize_booking(booking)
        # Availability payload matches /api/public/profile/<token> shape so
        # the frontend can reuse computeFreeSlots + SlotsCalendar unchanged.
        data["availability"] = _host_availability_for_reschedule(booking.host)
        return Response(data)

    def post(self, request: Request, uuid_) -> Response:
        booking = _get_active_booking(uuid_)
        if booking is None:
            return Response(status=404)
        if booking.status == Booking.Status.CANCELLED:
            return Response(_serialize_booking(booking))
        if booking.end_at < djtz.now():
            return Response(
                {"detail": "This booking already ended — nothing to cancel."},
                status=409,
            )
        reason = _clean_text(request.data.get("reason"), fallback="", maxlen=500)

        # Delete the calendar event via the provider that created it.
        # A missing / already-deleted event on the provider side isn't fatal
        # — we still mark our Booking row cancelled (the caller sees success).
        try:
            _create_fn, delete_fn = _provider_for(booking.provider)
        except ValueError:
            return Response({"detail": "Unknown provider."}, status=500)
        try:
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

        booking.status = Booking.Status.CANCELLED
        booking.cancelled_at = djtz.now()
        booking.cancellation_reason = reason
        booking.cancelled_by_visitor = True
        booking.save(update_fields=[
            "status", "cancelled_at", "cancellation_reason", "cancelled_by_visitor",
        ])

        _notify_booking_cancelled(booking)
        return Response(_serialize_booking(booking))


def _get_active_booking(uuid_) -> Booking | None:
    return Booking.objects.filter(uuid=uuid_).select_related("host").first()


def _serialize_booking(b: Booking) -> dict:
    host_name = f"{b.host.first_name} {b.host.last_name}".strip() or b.host.email
    return {
        "uuid": str(b.uuid),
        "host_name": host_name,
        "kind": b.kind,
        "status": b.status,
        "start": b.start_at.isoformat(),
        "end": b.end_at.isoformat(),
        "title": b.title,
        "location": b.location,
        "visitor_name": b.visitor_name,
        "visitor_email": b.visitor_email,
        "cancelled_at": b.cancelled_at.isoformat() if b.cancelled_at else None,
    }


def _host_availability_for_reschedule(host) -> dict:
    """Same shape as /api/public/profile/<token>'s availability payload,
    but sourced from the visitor's Booking (no share_token needed). This
    lets the visitor reschedule even if the host has since turned off
    their public booking link."""
    from datetime import timedelta as _td
    from django.db.models import Q as _Q
    from apps.availability.models import Unavailability as _Un
    from apps.calendars.models import CalendarEvent as _CE

    from apps.accounts.views import _holidays_in_range, _inflate_busy

    now = djtz.now()
    window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_end = window_start + _td(days=56)

    busy: list[tuple] = []
    for ev in _CE.objects.filter(
        _Q(transp=_CE.Transparency.OPAQUE) | _Q(is_all_day=True),
        calendar__owner_id=host.pk,
        calendar__include_in_busy=True,
        dtstart__lt=window_end,
        dtend__gt=window_start,
    ).exclude(status=_CE.Status.CANCELLED).values("dtstart", "dtend"):
        busy.append((ev["dtstart"], ev["dtend"]))
    for u in _Un.objects.filter(
        user_id=host.pk,
        starts_at__lt=window_end,
        ends_at__gt=window_start,
    ).values("starts_at", "ends_at"):
        busy.append((u["starts_at"], u["ends_at"]))
    busy = _inflate_busy(busy, host.buffer_before_min, host.buffer_after_min)

    return {
        "working_hours": host.working_hours,
        "country": host.country,
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "busy": [{"start": s.isoformat(), "end": e.isoformat()} for s, e in busy],
        "holidays": _holidays_in_range(host.country, window_start.date(), window_end.date()),
    }


class PublicBookingRescheduleView(APIView):
    """POST /api/public/bookings/<uuid>/reschedule  body: {start, end}

    Visitor picks a new slot. We delete the old provider event, insert a
    new one with the same attendees/description (same manage URL — the
    Booking uuid is unchanged), and update the Booking row's time +
    event_id. If the delete succeeds but the create fails, we leave the
    Booking cancelled — the visitor can retry, but their old slot is
    already gone. That's the safer failure mode than a double-booking.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request: Request, uuid_) -> Response:
        from django.conf import settings as _settings

        booking = _get_active_booking(uuid_)
        if booking is None:
            return Response(status=404)
        if booking.status != Booking.Status.CONFIRMED:
            return Response({"detail": "Only confirmed bookings can be rescheduled."}, status=409)
        if booking.end_at < djtz.now():
            return Response({"detail": "This booking already ended — book a new one."}, status=409)

        try:
            new_start = _parse_iso_dt(request.data.get("start", ""))
            new_end = _parse_iso_dt(request.data.get("end", ""))
        except ValueError:
            return Response({"detail": "start/end must be ISO 8601 datetimes."}, status=400)
        if new_end <= new_start:
            return Response({"detail": "end must be after start."}, status=400)
        if (new_end - new_start) > timedelta(hours=12):
            return Response({"detail": "Meeting longer than 12 hours refused."}, status=400)
        if new_end < djtz.now() - timedelta(minutes=2):
            return Response({"detail": "Cannot reschedule to a slot in the past."}, status=400)
        if new_start == booking.start_at and new_end == booking.end_at:
            # No-op — return current state so the frontend doesn't error out.
            return Response(_serialize_booking(booking))

        # Re-check host availability at the new slot. Old booking still on
        # the calendar right now — exclude it from the conflict check by
        # temporarily marking it non-blocking (we're about to delete it).
        conflict_user = _first_busy_user(
            [booking.host],
            new_start,
            new_end,
            exclude_event_id=booking.event_id,
        )
        if conflict_user is not None:
            return Response(
                {"detail": "The host is no longer free at that time — pick another slot."},
                status=409,
            )

        try:
            create_fn, delete_fn = _provider_for(booking.provider)
        except ValueError:
            return Response({"detail": "Unknown provider."}, status=500)

        # Delete old event. Failure here isn't fatal — we still try to
        # create the new one. If both fail we haven't broken the booking.
        try:
            if booking.event_id:
                delete_fn(
                    booking.host,
                    calendar_id=booking.calendar_id,
                    event_id=booking.event_id,
                )
        except (GoogleOAuthError, _graph.MicrosoftOAuthError):
            return Response(
                {"detail": "Reconnect needed on the host's side — try again later."},
                status=502,
            )
        except (GoogleApiError, _graph.MicrosoftApiError) as exc:
            logger.info("reschedule delete failed for booking %s: %s", booking.uuid, exc)

        # Rebuild description with the same manage URL (uuid unchanged).
        base_description = (
            f"Rescheduled via your Slotly manage link by "
            f"{booking.visitor_name or booking.visitor_email}."
        )
        if booking.notes:
            base_description += f"\n\nOriginal note:\n{booking.notes}"
        description = _description_with_manage_link(base_description, booking)

        try:
            event = create_fn(
                booking.host,
                calendar_id=booking.calendar_id,
                summary=booking.title or f"Meeting with {booking.visitor_name}",
                description=description,
                start_iso=new_start.isoformat(),
                end_iso=new_end.isoformat(),
                time_zone=_settings.TIME_ZONE,
                attendees=[booking.visitor_email],
                include_online_meeting=(booking.kind == Booking.Kind.ONLINE),
                location=booking.location,
            )
        except (GoogleOAuthError, _graph.MicrosoftOAuthError):
            return Response(
                {"detail": "Reconnect needed on the host's side — try again later."},
                status=502,
            )
        except (GoogleApiError, _graph.MicrosoftApiError) as exc:
            logger.info("reschedule insert failed for booking %s: %s", booking.uuid, exc)
            return Response(
                {"detail": "Couldn't create the new event. Please try again."},
                status=502,
            )

        old_start = booking.start_at
        booking.start_at = new_start
        booking.end_at = new_end
        booking.event_id = event.get("id", "") or booking.event_id
        # Any T-24h reminder we may have already sent no longer applies —
        # reset so a future run mails for the new time.
        booking.reminded_at = None
        booking.save(update_fields=["start_at", "end_at", "event_id", "reminded_at"])

        _notify_booking_rescheduled(booking, old_start)
        return Response(_serialize_booking(booking))


def _notify_booking_cancelled(booking: Booking) -> None:
    """Host in-app notification + email when a visitor cancels."""
    from apps.notifications.dispatch import notify as _notify
    from apps.notifications.models import Notification as _N

    when = booking.start_at.strftime("%a %d %b %H:%M")
    _notify(
        booking.host,
        _N.Type.BOOKING_CANCELLED_BY_VISITOR,
        {
            "visitor_name": booking.visitor_name,
            "visitor_email": booking.visitor_email,
            "when": when,
            "reason": booking.cancellation_reason,
        },
    )


# ---------------------------------------------------------------------------
# Booking requests — host-side approval flow for physical (in-person)
# meetings. Online bookings never touch this model; they hit the calendar
# API directly. Physical bookings live here until the host clicks approve
# in the UI, at which point we create the calendar event.
# ---------------------------------------------------------------------------


class MeetingTypeListView(APIView):
    """GET  /api/meeting-types — host's own meeting types (all, incl. inactive).
    POST /api/meeting-types  body: {name, duration_min, kind, description?,
    location?, color?, is_active?} — create new one, slug auto-derived."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        rows = MeetingType.objects.filter(host=request.user).order_by("display_order", "id")
        return Response({"types": [_serialize_meeting_type(t) for t in rows]})

    def post(self, request: Request) -> Response:
        cleaned, err = _validate_meeting_type_payload(request.data, host=request.user, existing=None)
        if err is not None:
            return Response(err, status=400)
        t = MeetingType.objects.create(host=request.user, **cleaned)
        return Response(_serialize_meeting_type(t), status=201)


class MeetingTypeDetailView(APIView):
    """PATCH /api/meeting-types/<id>  body: any subset of the create fields.
    DELETE /api/meeting-types/<id>."""

    permission_classes = [IsAuthenticated]

    def patch(self, request: Request, pk: int) -> Response:
        t = get_object_or_404(MeetingType, pk=pk, host=request.user)
        cleaned, err = _validate_meeting_type_payload(request.data, host=request.user, existing=t)
        if err is not None:
            return Response(err, status=400)
        for field, value in cleaned.items():
            setattr(t, field, value)
        t.save()
        return Response(_serialize_meeting_type(t))

    def delete(self, request: Request, pk: int) -> Response:
        deleted, _ = MeetingType.objects.filter(pk=pk, host=request.user).delete()
        if deleted == 0:
            return Response(status=404)
        return Response(status=204)


def _serialize_meeting_type(t: MeetingType) -> dict:
    return {
        "id": t.pk,
        "name": t.name,
        "slug": t.slug,
        "description": t.description,
        "duration_min": t.duration_min,
        "kind": t.kind,
        "location": t.location,
        "color": t.color,
        "is_active": t.is_active,
        "display_order": t.display_order,
        "questions": list(t.questions or []),
        "redirect_url": t.redirect_url,
    }


_ALLOWED_DURATIONS = {15, 30, 45, 60, 90, 120, 180, 240}
_HEX_COLOUR_RE = None  # lazily compiled


def _validate_meeting_type_payload(
    body: dict,
    *,
    host,
    existing: MeetingType | None,
) -> tuple[dict, dict | None]:
    """Return (cleaned_fields, error_response_or_none). ``existing`` is the
    row being patched; None for create. Slug auto-derives from name on
    create if not supplied and dedupes against the host's other types."""
    global _HEX_COLOUR_RE
    if _HEX_COLOUR_RE is None:
        import re as _re
        _HEX_COLOUR_RE = _re.compile(r"^#[0-9a-fA-F]{6}$")

    cleaned: dict = {}

    def _get(key: str):
        return body.get(key) if isinstance(body, dict) else None

    name = _get("name")
    if name is not None:
        name = str(name).strip()[:80]
        if not name:
            return {}, {"name": "Required non-empty string."}
        cleaned["name"] = name

    slug = _get("slug")
    if slug is not None:
        from django.utils.text import slugify
        slug = slugify(str(slug))[:60]
        if not slug:
            return {}, {"slug": "Slug must contain URL-friendly characters."}
        cleaned["slug"] = slug
    elif existing is None:
        # Auto-derive on create.
        from django.utils.text import slugify
        base = slugify(cleaned.get("name") or "")
        if not base:
            return {}, {"slug": "Give the type a name so we can generate a slug."}
        slug = base
        n = 2
        # Dedupe within this host's slugs.
        existing_slugs = set(
            MeetingType.objects.filter(host=host).values_list("slug", flat=True),
        )
        while slug in existing_slugs:
            slug = f"{base}-{n}"
            n += 1
        cleaned["slug"] = slug

    if "description" in body:
        cleaned["description"] = str(_get("description") or "").strip()[:1000]

    if "duration_min" in body:
        try:
            d = int(_get("duration_min"))
        except (TypeError, ValueError):
            return {}, {"duration_min": "Must be an integer."}
        if d not in _ALLOWED_DURATIONS:
            return {}, {"duration_min": f"Must be one of {sorted(_ALLOWED_DURATIONS)}."}
        cleaned["duration_min"] = d

    if "kind" in body:
        k = str(_get("kind") or "").lower()
        if k not in ("online", "physical"):
            return {}, {"kind": "Must be 'online' or 'physical'."}
        cleaned["kind"] = k

    if "location" in body:
        cleaned["location"] = str(_get("location") or "").strip()[:300]

    if "color" in body:
        col = str(_get("color") or "").strip()
        if not _HEX_COLOUR_RE.match(col):
            return {}, {"color": "Must be a hex colour like #4f46e5."}
        cleaned["color"] = col

    if "is_active" in body:
        cleaned["is_active"] = bool(_get("is_active"))

    if "display_order" in body:
        try:
            cleaned["display_order"] = max(0, int(_get("display_order")))
        except (TypeError, ValueError):
            return {}, {"display_order": "Must be an integer."}

    if "redirect_url" in body:
        url = str(_get("redirect_url") or "").strip()[:2000]
        if url and not (url.startswith("https://") or url.startswith("http://")):
            return {}, {"redirect_url": "Must start with http:// or https://."}
        cleaned["redirect_url"] = url

    if "questions" in body:
        raw_questions = _get("questions") or []
        if not isinstance(raw_questions, list):
            return {}, {"questions": "Must be a list."}
        cleaned_qs: list[dict] = []
        seen_ids: set = set()
        for i, q in enumerate(raw_questions):
            if not isinstance(q, dict):
                return {}, {"questions": f"Item #{i} must be an object."}
            label = str(q.get("label") or "").strip()[:120]
            if not label:
                return {}, {"questions": f"Item #{i}: label is required."}
            kind = str(q.get("kind") or "text").lower()
            if kind not in ("text", "textarea", "select"):
                return {}, {"questions": f"Item #{i}: kind must be text/textarea/select."}
            required = bool(q.get("required", False))
            qid = str(q.get("id") or "").strip()
            if not qid:
                import uuid as _uuid
                qid = _uuid.uuid4().hex
            if qid in seen_ids:
                return {}, {"questions": f"Item #{i}: duplicate id."}
            seen_ids.add(qid)
            item: dict = {"id": qid, "label": label, "kind": kind, "required": required}
            if kind == "select":
                opts_raw = q.get("options") or []
                if not isinstance(opts_raw, list) or not opts_raw:
                    return {}, {"questions": f"Item #{i}: select needs at least one option."}
                opts = [str(o).strip() for o in opts_raw if str(o).strip()][:20]
                if not opts:
                    return {}, {"questions": f"Item #{i}: select options can't all be blank."}
                item["options"] = opts
            cleaned_qs.append(item)
        cleaned["questions"] = cleaned_qs

    # Uniqueness: slug per host.
    if "slug" in cleaned:
        qs = MeetingType.objects.filter(host=host, slug=cleaned["slug"])
        if existing is not None:
            qs = qs.exclude(pk=existing.pk)
        if qs.exists():
            return {}, {"slug": "You already have a meeting type with that slug."}

    if existing is None and "duration_min" not in cleaned:
        return {}, {"duration_min": "Required on create."}
    if existing is None and "name" not in cleaned:
        return {}, {"name": "Required on create."}

    return cleaned, None


class HostBookingsIcsExportView(APIView):
    """GET /api/host-bookings/export.ics[?status=upcoming|past|all]

    Streams the host's bookings as a valid iCalendar (RFC 5545) feed —
    handy for backup, importing into another calendar tool, or attaching
    to invoices. Each Booking becomes one VEVENT with the visitor as an
    ATTENDEE. Cancelled bookings emit STATUS:CANCELLED.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Any:
        from django.http import HttpResponse

        status_filter = request.query_params.get("status", "upcoming")
        qs = Booking.objects.filter(host=request.user).order_by("start_at")
        now = djtz.now()
        if status_filter == "upcoming":
            qs = qs.filter(end_at__gte=now)
        elif status_filter == "past":
            qs = qs.filter(end_at__lt=now)
        # "all" → no extra filter

        body = _render_ics(list(qs[:1000]), request.user)
        resp = HttpResponse(body, content_type="text/calendar; charset=utf-8")
        resp["Content-Disposition"] = (
            f'attachment; filename="slotly-bookings-{status_filter}.ics"'
        )
        return resp


def _render_ics(bookings: list, host) -> str:
    """Hand-craft a minimal-but-compliant iCalendar feed. Avoided the
    `ics` / `icalendar` deps — one VCALENDAR + N VEVENTs is straightforward
    and keeps our wheel diet small. RFC 5545 folding is not applied (most
    consumers tolerate long lines; we cap description at 400 chars anyway).
    """
    from html import escape as _escape  # noqa: F401 — reserved for future needs

    from datetime import timezone as _dt_tz

    def _fmt_dt(dt) -> str:
        # UTC form: 20260910T100000Z
        return dt.astimezone(_dt_tz.utc).strftime("%Y%m%dT%H%M%SZ")

    def _esc(v: str) -> str:
        # RFC 5545 escaping: commas, semicolons, backslashes, newlines.
        return (
            v.replace("\\", "\\\\")
            .replace(",", "\\,")
            .replace(";", "\\;")
            .replace("\n", "\\n")
            .replace("\r", "")
        )

    host_email = host.email
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Slotly//Bookings Export//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    stamp = _fmt_dt(djtz.now())
    for b in bookings:
        title = (b.title or "Slotly booking").strip()
        summary = _esc(title)
        # Include visitor name and location context in the description.
        desc_parts = []
        if b.visitor_name or b.visitor_email:
            who = f"{b.visitor_name or ''} <{b.visitor_email or ''}>".strip()
            desc_parts.append(f"With: {who}")
        if b.location:
            desc_parts.append(f"Where: {b.location}")
        if b.attendee_emails and len(b.attendee_emails) > 1:
            desc_parts.append(f"Attendees: {', '.join(b.attendee_emails)}")
        description = _esc("\n".join(desc_parts))[:400]

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:slotly-{b.uuid}@slotly.team",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{_fmt_dt(b.start_at)}",
            f"DTEND:{_fmt_dt(b.end_at)}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{description}",
            f"ORGANIZER;CN={_esc(host.first_name or host_email)}:mailto:{host_email}",
        ])
        if b.location:
            lines.append(f"LOCATION:{_esc(b.location)}")
        if b.status == Booking.Status.CANCELLED:
            lines.append("STATUS:CANCELLED")
        else:
            lines.append("STATUS:CONFIRMED")
        for email in (b.attendee_emails or [b.visitor_email]):
            if email:
                lines.append(
                    f"ATTENDEE;CN={_esc(email)};RSVP=TRUE:mailto:{email}"
                )
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    # iCalendar convention is CRLF-terminated lines.
    return "\r\n".join(lines) + "\r\n"


class HostBookingCancelView(APIView):
    """POST /api/host-bookings/<uuid>/cancel  body: {reason?}

    Host-initiated cancel. Mirrors the visitor-side flow: deletes the
    calendar event on the provider, marks the Booking row cancelled, and
    e-mails the visitor a branded confirmation with reason (if given).
    Idempotent — a second call on an already-cancelled row is a no-op
    that returns the current state."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, uuid_) -> Response:
        booking = Booking.objects.filter(uuid=uuid_, host=request.user).first()
        if booking is None:
            return Response(status=404)
        if booking.status == Booking.Status.CANCELLED:
            return Response(_serialize_host_booking(booking))
        if booking.end_at < djtz.now():
            return Response(
                {"detail": "This booking already ended — nothing to cancel."},
                status=409,
            )
        reason = _clean_text(request.data.get("reason"), fallback="", maxlen=500)

        try:
            _create_fn, delete_fn = _provider_for(booking.provider)
        except ValueError:
            return Response({"detail": "Unknown provider."}, status=500)
        try:
            if booking.event_id:
                delete_fn(booking.host, calendar_id=booking.calendar_id, event_id=booking.event_id)
        except (GoogleOAuthError, _graph.MicrosoftOAuthError):
            logger.info(
                "host cancel booking %s: grant broken; marking cancelled anyway",
                booking.uuid,
            )
        except (GoogleApiError, _graph.MicrosoftApiError) as exc:
            logger.info("host cancel booking %s: provider refused (%s)", booking.uuid, exc)

        booking.status = Booking.Status.CANCELLED
        booking.cancelled_at = djtz.now()
        booking.cancellation_reason = reason
        booking.cancelled_by_visitor = False
        booking.save(update_fields=[
            "status", "cancelled_at", "cancellation_reason", "cancelled_by_visitor",
        ])

        _mail_visitor_host_cancellation(booking, reason)
        return Response(_serialize_host_booking(booking))


def _mail_visitor_host_cancellation(booking: Booking, reason: str) -> None:
    """Branded HTML/text email to the visitor when the host cancels.
    Duplicated in services.py for use by cancel_booking service function."""
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


class HostBookingListView(APIView):
    """GET /api/host-bookings[?status=upcoming|past|cancelled|all]

    Confirmed bookings from the host's point of view — powers the
    /bookings "Confirmed" tab. Complements BookingRequestListView, which
    handles the pre-approval physical queue only. Returns Booking rows
    (public-facing bookings, both online and approved physical)."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        status_filter = request.query_params.get("status", "upcoming")
        qs = Booking.objects.filter(host=request.user).order_by("-start_at")
        now = djtz.now()
        if status_filter == "upcoming":
            qs = qs.filter(status=Booking.Status.CONFIRMED, end_at__gte=now)
        elif status_filter == "past":
            qs = qs.filter(status=Booking.Status.CONFIRMED, end_at__lt=now)
        elif status_filter == "cancelled":
            qs = qs.filter(status=Booking.Status.CANCELLED)
        # "all" → no extra filter
        rows = [_serialize_host_booking(b) for b in qs[:200]]
        return Response({"bookings": rows})


def _serialize_host_booking(b: Booking) -> dict:
    return {
        "uuid": str(b.uuid),
        "kind": b.kind,
        "status": b.status,
        "start": b.start_at.isoformat(),
        "end": b.end_at.isoformat(),
        "title": b.title,
        "location": b.location,
        "visitor_name": b.visitor_name,
        "visitor_email": b.visitor_email,
        "attendee_emails": list(b.attendee_emails or []),
        "custom_answers": dict(b.custom_answers or {}),
        "cancelled_at": b.cancelled_at.isoformat() if b.cancelled_at else None,
        "cancelled_by_visitor": b.cancelled_by_visitor,
        "created_at": b.created_at.isoformat(),
    }


class BookingRequestListView(APIView):
    """GET /api/booking-requests[?status=pending|all]

    Returns the host's booking requests, newest first. Powers the /bookings
    page where the host sees pending physical-meeting requests and can
    approve or reject."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        status_filter = request.query_params.get("status", "pending")
        qs = BookingRequest.objects.filter(host=request.user).order_by("-created_at")
        if status_filter != "all":
            qs = qs.filter(status=status_filter)
        rows = [_serialize_booking_request(r) for r in qs[:100]]
        return Response({"requests": rows})


class BookingRequestDecideView(APIView):
    """POST /api/booking-requests/<id>/decide  body: {decision, note?}

    `decision` is "approve" or "reject". Approving a request creates the
    corresponding calendar event and mails the visitor a confirmation.
    Rejecting simply marks the row and emails the visitor a decline note.
    Idempotent on second call for the same terminal status — no-op."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, pk: int) -> Response:
        req = get_object_or_404(BookingRequest, pk=pk, host=request.user)
        decision = (request.data.get("decision") or "").strip().lower()
        note = _clean_text(request.data.get("note"), fallback="", maxlen=500)

        if decision not in ("approve", "reject"):
            return Response({"decision": "Must be 'approve' or 'reject'."}, status=400)
        if req.status != BookingRequest.Status.PENDING:
            # Return the current state rather than 409 — makes the UI's
            # double-click safe.
            return Response(_serialize_booking_request(req))

        from django.conf import settings as _settings

        if decision == "approve":
            provider = _pick_write_provider(req.host)
            if provider is None:
                return Response(
                    {"detail": "Connect a calendar first — the request needs a home."},
                    status=409,
                )
            conflict_user = _first_busy_user([req.host], req.start_at, req.end_at)
            if conflict_user is not None:
                return Response(
                    {"detail": "You're no longer free at that time — reject and ask them to pick another slot."},
                    status=409,
                )
            description_parts = [
                f"Booked via your Slotly public link by {req.visitor_name} <{req.visitor_email}>.",
            ]
            if req.notes:
                description_parts.append(f"\nVisitor note:\n{req.notes}")
            if note:
                description_parts.append(f"\nYour note:\n{note}")
            base_description = "\n".join(description_parts)
            # Manage-URL footer so the visitor can cancel the confirmed
            # meeting from the same /b/<uuid> page online bookings use.
            booking = Booking(
                host=req.host,
                provider=provider.name,
                calendar_id=provider.write_calendar_id,
                visitor_name=req.visitor_name,
                visitor_email=req.visitor_email,
                attendee_emails=[req.visitor_email],
                kind=Booking.Kind.PHYSICAL,
                title=req.title or f"Meeting with {req.visitor_name}",
                location=req.location,
                notes=req.notes,
                start_at=req.start_at,
                end_at=req.end_at,
            )
            description = _description_with_manage_link(base_description, booking)
            try:
                event = provider.create_event(
                    req.host,
                    calendar_id=provider.write_calendar_id,
                    summary=req.title or f"Meeting with {req.visitor_name}",
                    description=description,
                    start_iso=req.start_at.isoformat(),
                    end_iso=req.end_at.isoformat(),
                    time_zone=_settings.TIME_ZONE,
                    attendees=[req.visitor_email],
                    include_online_meeting=False,
                    location=req.location,
                )
            except (GoogleOAuthError, _graph.MicrosoftOAuthError):
                return Response(
                    {"detail": "Reconnect your calendar and try again."},
                    status=502,
                )
            except (GoogleApiError, _graph.MicrosoftApiError) as exc:
                logger.info("approve booking failed for req %s: %s", req.pk, exc)
                return Response(
                    {"detail": "Calendar refused the event. Try again."},
                    status=502,
                )
            booking.event_id = event.get("id", "") or event.get("iCalUId", "") or ""
            booking.save()
            req.status = BookingRequest.Status.APPROVED
            req.decided_at = djtz.now()
            req.decision_note = note
            req.event_id = booking.event_id
            req.save(update_fields=["status", "decided_at", "decision_note", "event_id"])
        else:  # reject
            req.status = BookingRequest.Status.REJECTED
            req.decided_at = djtz.now()
            req.decision_note = note
            req.save(update_fields=["status", "decided_at", "decision_note"])
            _mail_visitor_rejection(req, note)

        return Response(_serialize_booking_request(req))


def _serialize_booking_request(r) -> dict:
    return {
        "id": r.pk,
        "kind": r.kind,
        "status": r.status,
        "start": r.start_at.isoformat(),
        "end": r.end_at.isoformat(),
        "title": r.title,
        "notes": r.notes,
        "location": r.location,
        "visitor_name": r.visitor_name,
        "visitor_email": r.visitor_email,
        "decision_note": r.decision_note,
        "created_at": r.created_at.isoformat(),
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
    }


def _notify_booking_request(host, req) -> None:
    from apps.notifications.dispatch import notify as _notify
    from apps.notifications.models import Notification as _N

    when = req.start_at.strftime("%a %d %b %H:%M")
    _notify(
        host,
        _N.Type.BOOKING_REQUEST_RECEIVED,
        {
            "request_id": req.pk,
            "visitor_name": req.visitor_name,
            "visitor_email": req.visitor_email,
            "when": when,
            "location": req.location,
        },
    )


def _mail_visitor_rejection(req, note: str) -> None:
    """Send the visitor a "can't make it" email in both plain text and
    HTML. Best-effort — approval must not fail because SMTP was flaky."""
    from django.conf import settings as _settings
    from django.core.mail import EmailMultiAlternatives

    from apps.notifications.emails import blockquote, html_shell, kv_rows, paragraph

    when = req.start_at.strftime("%A %d %B, %H:%M")
    host_name = f"{req.host.first_name} {req.host.last_name}".strip() or req.host.email
    subject = f"Your Slotly meeting request on {when} was declined"

    text_body = f"Hi {req.visitor_name},\n\n{host_name} isn't able to meet {when}."
    if note:
        text_body += f"\n\nNote from {host_name}:\n{note}"
    text_body += "\n\nFeel free to pick another slot from their booking page."

    intro = paragraph(f"Hi {req.visitor_name},")
    intro += paragraph(f"{host_name} isn't able to meet on {when}.")
    intro += kv_rows([
        ("When", when),
        ("Where", req.location),
    ])
    if note:
        intro += paragraph(f"Note from {host_name}:")
        intro += blockquote(note)
    intro += paragraph(
        "Feel free to pick another slot from their booking page — no hard feelings.",
        muted=True,
    )

    share_link = f"{_settings.FRONTEND_BASE_URL.rstrip('/')}/u/{req.host.share_token}"
    html_body = html_shell(
        title=f"{host_name} can't make that time",
        intro_html=intro,
        cta_label="Pick another slot",
        cta_url=share_link,
    )

    try:
        msg = EmailMultiAlternatives(
            subject=f"[Slotly] {subject}",
            body=text_body,
            from_email=_settings.DEFAULT_FROM_EMAIL,
            to=[req.visitor_email],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("visitor rejection mail failed for req %s: %s", req.pk, exc)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
# Business-logic helpers (_can_view_peer, _first_busy_user, _default_title,
# _manage_url, _description_with_manage_link, _extract_meet_link,
# _notify_booking_rescheduled) now live in services.py and are imported at
# the top of this file.  Provider-plumbing helpers (_WriteProvider,
# _pick_write_provider, _provider_for) are duplicated here so that unittest
# patches on apps.scheduling.views.create_calendar_event keep working for the
# existing public-booking tests (they patch the module-level name, which means
# they must patch the same namespace that _pick_write_provider reads from).


from dataclasses import dataclass as _dataclass


@_dataclass(slots=True)
class _WriteProvider:
    """Uniform handle over Google vs. Microsoft for view-layer code.
    See services._WriteProvider for the services-layer counterpart."""

    name: str
    write_calendar_id: str
    create_event: Any
    delete_event: Any


def _pick_write_provider(user) -> _WriteProvider | None:
    """Choose which OAuth-connected provider should host the new event."""
    g = GoogleAccount.objects.filter(user=user).only("write_calendar_id").first()
    if g is not None:
        return _WriteProvider(
            name="google",
            write_calendar_id=g.write_calendar_id or "primary",
            create_event=create_calendar_event,
            delete_event=delete_calendar_event,
        )
    m = MicrosoftAccount.objects.filter(user=user).only("write_calendar_id").first()
    if m is not None:
        return _WriteProvider(
            name="microsoft",
            write_calendar_id=m.write_calendar_id,
            create_event=_graph.create_calendar_event,
            delete_event=_graph.delete_calendar_event,
        )
    return None


def _provider_for(name: str) -> Any:
    """Look up create/delete helpers by provider name."""
    if name == "google":
        return create_calendar_event, delete_calendar_event
    if name == "microsoft":
        return _graph.create_calendar_event, _graph.delete_calendar_event
    raise ValueError(f"Unknown provider: {name}")


def _parse_iso_dt(value):
    """Accept an ISO 8601 datetime (with tz) or reject."""
    from datetime import datetime as _dt
    if not isinstance(value, str) or not value:
        raise ValueError("empty")
    parsed = _dt.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # Treat naive as app-local so the frontend can send local ISO without tz.
        from django.conf import settings as _settings
        from zoneinfo import ZoneInfo
        parsed = parsed.replace(tzinfo=ZoneInfo(_settings.TIME_ZONE))
    return parsed


def _clean_text(value, *, fallback: str, maxlen: int) -> str:
    if not isinstance(value, str):
        return fallback
    v = value.strip()
    if not v:
        return fallback
    return v[:maxlen]


def _render_answers_block(questions: list, answers: dict) -> str:
    """Format custom Q&A as a plain-text block for calendar descriptions.
    Skips questions the visitor left blank (unanswered optionals)."""
    lines: list[str] = []
    for q in questions:
        qid = q.get("id")
        ans = answers.get(qid) if qid else None
        if not ans:
            continue
        lines.append(f"{q.get('label', 'Q')}: {ans}")
    if not lines:
        return ""
    return "Additional info:\n" + "\n".join(lines)


def _looks_like_email(s: str) -> bool:
    # Rough — Django's EmailValidator would raise on invalid, but that pulls
    # in the whole validators module for a single call. Enough to reject
    # empty / malformed inputs; Google Calendar will further reject nonsense.
    import re as _re
    return bool(s) and bool(_re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s))


def _client_ip(request) -> str:
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return (request.META.get("REMOTE_ADDR") or "unknown")[:64]


