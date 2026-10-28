"""
Thin wrapper around Google's OAuth + Calendar API.

We avoid the heavy `google-auth` / `google-api-python-client` stack — the
calls we need (token exchange, token refresh, events.insert, freeBusy) are
straightforward HTTPS POSTs to documented endpoints. Keeping it as httpx
saves ~30MB of dependencies and surface area.

Public entry points:
  - exchange_code(code, redirect_uri) -> dict          (M18a callback)
  - refresh_access_token(refresh_token) -> dict        (M18a, called by helper)
  - get_credentials(user) -> _Credentials              (M18b — picks up DB row,
                                                        refreshes if expired,
                                                        persists new token)
  - bearer_session(creds) -> httpx.Client              (M18b)

The module is self-contained and importable without Google secrets being
configured; configuration is only checked at call time, so unit tests that
mock the wrapper functions don't need env to be set.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from django.conf import settings
from django.utils import timezone as djtz

from .models import GoogleAccount
from .security import decrypt, encrypt

logger = logging.getLogger(__name__)


GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class GoogleNotConfigured(RuntimeError):
    """GOOGLE_OAUTH_CLIENT_ID/SECRET are blank — connect/booking endpoints
    must return a clear 503 rather than letting the request reach Google
    and fail opaquely."""


class GoogleOAuthError(RuntimeError):
    """Token exchange or refresh failed — typically a revoked grant. The
    caller is expected to delete the GoogleAccount row and prompt the user
    to reconnect."""


def _require_client() -> tuple[str, str]:
    cid = settings.GOOGLE_OAUTH_CLIENT_ID
    csecret = settings.GOOGLE_OAUTH_CLIENT_SECRET
    if not cid or not csecret:
        raise GoogleNotConfigured(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set"
        )
    return cid, csecret


def authorize_url(state: str) -> str:
    """Build the Google consent URL the user is redirected to from /start."""
    cid, _ = _require_client()
    from urllib.parse import urlencode

    params = {
        "client_id": cid,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(settings.GOOGLE_OAUTH_SCOPES),
        # offline + prompt=consent guarantee a refresh_token on first connect
        # and on reconnect after a revoke.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict:
    """
    Trade an authorization code for tokens. Returns the raw Google JSON:
      {access_token, refresh_token, expires_in, scope, token_type, id_token}
    Caller is responsible for fetching userinfo for the email.
    """
    cid, csecret = _require_client()
    payload = {
        "code": code,
        "client_id": cid,
        "client_secret": csecret,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    with httpx.Client(timeout=20.0) as client:
        r = client.post(GOOGLE_TOKEN_URL, data=payload)
    if r.status_code != 200:
        logger.info("Google token exchange failed: %s %s", r.status_code, r.text[:200])
        raise GoogleOAuthError(f"token exchange returned {r.status_code}")
    return r.json()


def refresh_access_token(refresh_token: str) -> dict:
    """Get a fresh access_token. Returns Google JSON (no refresh_token unless
    Google decides to rotate it). Raises if the grant was revoked."""
    cid, csecret = _require_client()
    payload = {
        "client_id": cid,
        "client_secret": csecret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    with httpx.Client(timeout=20.0) as client:
        r = client.post(GOOGLE_TOKEN_URL, data=payload)
    if r.status_code == 400:
        # Most common payload here is {"error":"invalid_grant"} → revoked.
        raise GoogleOAuthError(f"refresh denied: {r.text[:200]}")
    if r.status_code != 200:
        raise GoogleOAuthError(f"refresh returned {r.status_code}: {r.text[:200]}")
    return r.json()


def fetch_userinfo(access_token: str) -> dict:
    """Pull the user's profile (specifically `email`) using a freshly-issued
    access token. Used by the OAuth callback to label the GoogleAccount."""
    with httpx.Client(timeout=20.0) as client:
        r = client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if r.status_code != 200:
        raise GoogleOAuthError(f"userinfo returned {r.status_code}")
    return r.json()


@dataclass(slots=True)
class _Credentials:
    """In-memory snapshot of a usable Google access token."""

    access_token: str
    expires_at: datetime


def get_credentials(
    user,
    account_id: int | None = None,
    *,
    refresh_grace: timedelta = timedelta(seconds=60),
) -> _Credentials:
    """
    Resolve a usable access token for ``user``. Refreshes the stored token
    via Google when within ``refresh_grace`` of expiry, persisting the new
    access_token + expires_at in the DB.

    If ``account_id`` is given, loads that specific GoogleAccount (must
    belong to ``user``).  If ``None``, reads the user's WriteCalendarDefault;
    if none exists or default is not Google, raises GoogleOAuthError (same
    shape as the old GoogleAccount.DoesNotExist path so existing callers'
    error handling still fires).
    """
    from .models import WriteCalendarDefault  # local import — avoids circular

    if account_id is not None:
        try:
            account = GoogleAccount.objects.get(pk=account_id, user=user)
        except GoogleAccount.DoesNotExist as exc:
            raise GoogleOAuthError("Google account not found") from exc
    else:
        default = (
            WriteCalendarDefault.objects
            .select_related("google_account")
            .filter(user=user)
            .first()
        )
        if default is not None and default.google_account_id is not None:
            account = default.google_account
        else:
            # Fall back to any connected account so the existing single-account
            # path still works for users who have one account but no explicit default.
            account = GoogleAccount.objects.filter(user=user).first()
            if account is None:
                raise GoogleOAuthError("user has no connected Google account")

    if account.expires_at > djtz.now() + refresh_grace:
        return _Credentials(
            access_token=decrypt(account.access_token_encrypted),
            expires_at=account.expires_at,
        )

    # Token is expired or about to be — exchange the refresh token.
    refresh_plain = decrypt(account.refresh_token_encrypted)
    payload = refresh_access_token(refresh_plain)
    new_access = payload["access_token"]
    expires_in = int(payload.get("expires_in", 3600))
    new_expires_at = djtz.now() + timedelta(seconds=expires_in)
    # Google may rotate the refresh token (rare, but possible).
    new_refresh = payload.get("refresh_token", refresh_plain)

    GoogleAccount.objects.filter(pk=account.pk).update(
        access_token_encrypted=encrypt(new_access),
        refresh_token_encrypted=encrypt(new_refresh),
        expires_at=new_expires_at,
    )
    return _Credentials(access_token=new_access, expires_at=new_expires_at)


def bearer_session(creds: _Credentials) -> httpx.Client:
    """A short-lived httpx client preconfigured with the user's bearer token.
    The caller is responsible for closing it (``with`` block recommended)."""
    return httpx.Client(
        base_url="https://www.googleapis.com",
        headers={"Authorization": f"Bearer {creds.access_token}"},
        timeout=30.0,
    )


class GoogleApiError(RuntimeError):
    """A Calendar API call returned non-2xx. `status` mirrors the HTTP code
    so callers can 502/503/etc. distinguish auth from user error."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def list_writable_calendars(user) -> list[dict]:
    """
    Returns the user's calendarList entries filtered to ones we can write
    events into (accessRole owner or writer). Each item: {id, summary,
    primary, accessRole, timeZone}. Empty list is a valid result — the user
    is connected but has no writable calendars, so booking should be
    disabled until they grant access to one.
    """
    creds = get_credentials(user)
    with bearer_session(creds) as client:
        r = client.get("/calendar/v3/users/me/calendarList")
    if r.status_code != 200:
        raise GoogleApiError(r.status_code, f"calendarList: {r.text[:200]}")
    items = r.json().get("items", [])
    return [
        {
            "id": it["id"],
            "summary": it.get("summary", ""),
            "primary": bool(it.get("primary", False)),
            "access_role": it.get("accessRole", ""),
            "time_zone": it.get("timeZone", ""),
        }
        for it in items
        if it.get("accessRole") in ("owner", "writer")
    ]


def create_calendar_event(
    user,
    *,
    calendar_id: str,
    summary: str,
    description: str,
    start_iso: str,
    end_iso: str,
    time_zone: str,
    attendees: list[str],
    include_online_meeting: bool = True,
    location: str = "",
) -> dict:
    """
    Create an event on ``calendar_id`` on behalf of ``user`` and email an
    invite to ``attendees``. Returns the parsed event JSON (Google fills id,
    htmlLink, hangoutLink, iCalUID).

    When ``include_online_meeting`` is True (default — "always online" per
    the product default), a Google Meet link is generated and appears both
    in the invite email and on ``event.hangoutLink`` in the response. The
    ``conferenceDataVersion=1`` query param is required for Google to
    honour the ``conferenceData.createRequest`` field.

    Times must be ISO 8601 datetimes. ``time_zone`` is the IANA name that
    Google displays alongside — pass the user's app TZ for consistency.
    """
    import uuid as _uuid

    payload: dict = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_iso, "timeZone": time_zone},
        "end": {"dateTime": end_iso, "timeZone": time_zone},
        "attendees": [{"email": e} for e in attendees if e],
        "reminders": {"useDefault": True},
    }
    if location:
        # Google recognises this as a first-class field: Calendar UI shows a
        # "location" row with a Maps deep-link, and mobile clients (Google
        # Calendar iOS/Android, Apple Calendar) treat it as a taggable
        # address for navigation.
        payload["location"] = location
    params = {"sendUpdates": "all"}
    if include_online_meeting:
        payload["conferenceData"] = {
            "createRequest": {
                "requestId": _uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        params["conferenceDataVersion"] = "1"

    creds = get_credentials(user)
    with bearer_session(creds) as client:
        r = client.post(
            f"/calendar/v3/calendars/{calendar_id}/events",
            params=params,
            json=payload,
        )
    if r.status_code not in (200, 201):
        raise GoogleApiError(r.status_code, f"events.insert: {r.text[:300]}")
    return r.json()


def delete_calendar_event(user, *, calendar_id: str, event_id: str) -> None:
    """Delete an event and notify attendees. Treats 404/410 as success —
    the event was already gone (deleted from Google directly or expired),
    which is the state the caller wanted."""
    creds = get_credentials(user)
    with bearer_session(creds) as client:
        r = client.delete(
            f"/calendar/v3/calendars/{calendar_id}/events/{event_id}",
            params={"sendUpdates": "all"},
        )
    if r.status_code in (200, 204, 404, 410):
        return
    raise GoogleApiError(r.status_code, f"events.delete: {r.text[:200]}")
