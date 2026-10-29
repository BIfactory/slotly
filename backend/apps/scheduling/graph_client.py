"""
Thin wrapper around Microsoft's OAuth 2.0 + Graph Calendar API.

Mirrors the shape of ``google_client.py`` — same public entry points, same
lazy-config check, same encrypted-token round-trip — so booking code can
dispatch to either provider through a common interface.

Public entry points:
  - authorize_url(state) -> str
  - exchange_code(code) -> dict
  - refresh_access_token(refresh_token) -> dict
  - fetch_userinfo(access_token) -> dict     (Graph /me)
  - get_credentials(user) -> _Credentials
  - list_writable_calendars(user) -> list[dict]
  - create_calendar_event(...) -> dict

We use the ``common`` tenant so both work-and-school accounts (Microsoft
365 corporate) and personal Outlook.com accounts can sign in. If a
customer's corporate tenant requires admin consent, that shows on the
consent screen — nothing to configure here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from django.conf import settings
from django.utils import timezone as djtz

from .models import MicrosoftAccount
from .security import decrypt, encrypt

logger = logging.getLogger(__name__)


MS_AUTHORIZE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
MS_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class MicrosoftNotConfigured(RuntimeError):
    """Env is missing MICROSOFT_OAUTH_CLIENT_ID / SECRET; the connect
    endpoint surfaces a clean 503 rather than letting the request fall
    through to Microsoft with garbage credentials."""


class MicrosoftOAuthError(RuntimeError):
    """Token exchange or refresh failed. Typically a revoked grant — the
    caller should drop the MicrosoftAccount row and prompt reconnect."""


class MicrosoftApiError(RuntimeError):
    """A Graph API call returned non-2xx. `status` is the HTTP code so
    upstream callers can 502/503/etc. appropriately."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _require_client() -> tuple[str, str]:
    cid = getattr(settings, "MICROSOFT_OAUTH_CLIENT_ID", "")
    csecret = getattr(settings, "MICROSOFT_OAUTH_CLIENT_SECRET", "")
    if not cid or not csecret:
        raise MicrosoftNotConfigured(
            "MICROSOFT_OAUTH_CLIENT_ID / MICROSOFT_OAUTH_CLIENT_SECRET are not set",
        )
    return cid, csecret


def authorize_url(state: str) -> str:
    """Consent URL. Requests scopes that grant identity + calendar write.
    `offline_access` is what earns us a refresh token."""
    cid, _ = _require_client()
    from urllib.parse import urlencode

    scopes = getattr(
        settings,
        "MICROSOFT_OAUTH_SCOPES",
        [
            "openid",
            "email",
            "profile",
            "offline_access",
            "Calendars.ReadWrite",
        ],
    )
    params = {
        "client_id": cid,
        "redirect_uri": settings.MICROSOFT_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "response_mode": "query",
        "scope": " ".join(scopes),
        "state": state,
        # prompt=select_account forces the account picker so users adding a
        # second account are never silently re-linked to the existing one.
        # consent is kept (space-separated multi-value) to guarantee
        # offline_access grants a fresh refresh token every time. Corporate
        # tenants that require admin consent will still gate on their own
        # policy.
        "prompt": "select_account consent",
    }
    return f"{MS_AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict:
    cid, csecret = _require_client()
    payload = {
        "code": code,
        "client_id": cid,
        "client_secret": csecret,
        "redirect_uri": settings.MICROSOFT_OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    with httpx.Client(timeout=20.0) as client:
        r = client.post(MS_TOKEN_URL, data=payload)
    if r.status_code != 200:
        logger.info("MS token exchange failed: %s %s", r.status_code, r.text[:200])
        raise MicrosoftOAuthError(f"token exchange returned {r.status_code}")
    return r.json()


def refresh_access_token(refresh_token: str) -> dict:
    cid, csecret = _require_client()
    payload = {
        "client_id": cid,
        "client_secret": csecret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    with httpx.Client(timeout=20.0) as client:
        r = client.post(MS_TOKEN_URL, data=payload)
    if r.status_code == 400:
        raise MicrosoftOAuthError(f"refresh denied: {r.text[:200]}")
    if r.status_code != 200:
        raise MicrosoftOAuthError(f"refresh returned {r.status_code}: {r.text[:200]}")
    return r.json()


def fetch_userinfo(access_token: str) -> dict:
    """Graph /me — returns id, displayName, givenName, surname, mail,
    userPrincipalName. `mail` may be null on personal accounts (fall back
    to `userPrincipalName` which is always the login email)."""
    with httpx.Client(timeout=20.0) as client:
        r = client.get(
            f"{GRAPH_BASE}/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if r.status_code != 200:
        raise MicrosoftOAuthError(f"/me returned {r.status_code}")
    return r.json()


@dataclass(slots=True)
class _Credentials:
    access_token: str
    expires_at: datetime


def get_credentials(
    user,
    account_id: int | None = None,
    *,
    refresh_grace: timedelta = timedelta(seconds=60),
) -> _Credentials:
    """
    Resolve a usable Microsoft access token for ``user``.

    If ``account_id`` is given, loads that specific MicrosoftAccount.
    If ``None``, reads WriteCalendarDefault; falls back to any connected
    account so existing single-account callers still work.
    """
    from .models import WriteCalendarDefault  # local import — avoids circular

    if account_id is not None:
        try:
            account = MicrosoftAccount.objects.get(pk=account_id, user=user)
        except MicrosoftAccount.DoesNotExist as exc:
            raise MicrosoftOAuthError("Microsoft account not found") from exc
    else:
        default = (
            WriteCalendarDefault.objects
            .select_related("microsoft_account")
            .filter(user=user)
            .first()
        )
        if default is not None and default.microsoft_account_id is not None:
            account = default.microsoft_account
        else:
            account = MicrosoftAccount.objects.filter(user=user).first()
            if account is None:
                raise MicrosoftOAuthError("user has no connected Microsoft account")

    if account.expires_at > djtz.now() + refresh_grace:
        return _Credentials(
            access_token=decrypt(account.access_token_encrypted),
            expires_at=account.expires_at,
        )

    refresh_plain = decrypt(account.refresh_token_encrypted)
    payload = refresh_access_token(refresh_plain)
    new_access = payload["access_token"]
    expires_in = int(payload.get("expires_in", 3600))
    new_expires_at = djtz.now() + timedelta(seconds=expires_in)
    new_refresh = payload.get("refresh_token", refresh_plain)

    MicrosoftAccount.objects.filter(pk=account.pk).update(
        access_token_encrypted=encrypt(new_access),
        refresh_token_encrypted=encrypt(new_refresh),
        expires_at=new_expires_at,
    )
    return _Credentials(access_token=new_access, expires_at=new_expires_at)


def _bearer(creds: _Credentials) -> httpx.Client:
    return httpx.Client(
        base_url=GRAPH_BASE,
        headers={
            "Authorization": f"Bearer {creds.access_token}",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )


def list_writable_calendars(user, account: "MicrosoftAccount | None" = None) -> list[dict]:
    """Return the user's calendars we can write to (canEdit=true). Each
    item mirrors the Google shape so the settings dropdown treats both
    the same: {id, summary, primary, access_role, time_zone}.

    When ``account`` is given, uses that specific MicrosoftAccount's
    credentials. Otherwise falls back to the default/first account (legacy
    behaviour).
    """
    creds = get_credentials(user, account.id if account is not None else None)
    with _bearer(creds) as client:
        r = client.get("/me/calendars")
    if r.status_code != 200:
        raise MicrosoftApiError(r.status_code, f"/me/calendars: {r.text[:200]}")
    items = r.json().get("value", [])
    out = []
    for it in items:
        if not it.get("canEdit"):
            continue
        out.append({
            "id": it.get("id", ""),
            "summary": it.get("name", ""),
            "primary": bool(it.get("isDefaultCalendar", False)),
            "access_role": "owner" if it.get("owner") else "writer",
            "time_zone": "",
        })
    return out


def list_all_calendars(account: "MicrosoftAccount") -> list[dict]:
    """Return all calendars for this Microsoft OAuth account (read-side — not
    filtered to writable). Each item: {id, summary, primary, access_role}.
    Used by the freebusy sync to discover every calendar the account can see.
    """
    creds = get_credentials(account.user, account.id)
    with _bearer(creds) as client:
        r = client.get("/me/calendars")
    if r.status_code != 200:
        raise MicrosoftApiError(r.status_code, f"/me/calendars: {r.text[:200]}")
    items = r.json().get("value", [])
    return [
        {
            "id": it.get("id", ""),
            "summary": it.get("name", ""),
            "primary": bool(it.get("isDefaultCalendar", False)),
            "access_role": "owner" if it.get("owner") else "reader",
        }
        for it in items
    ]


def fetch_freebusy(
    account: "MicrosoftAccount",
    calendar_ids: list[str],
    start: datetime,
    end: datetime,
) -> list[tuple[str, datetime, datetime]]:
    """POST /me/calendar/getSchedule for the given calendar IDs (as SMTP
    addresses or calendar IDs). Microsoft Graph accepts up to 20 schedules
    per request.

    Returns a list of (calendar_id, starts_at, ends_at) for every busy
    interval. Microsoft's getSchedule only returns busy/tentative/oof items;
    free items are excluded.

    Note: MS Graph getSchedule uses email addresses as schedule IDs. We pass
    the calendar IDs from the calendarList — for personal accounts these are
    UUIDs. The Graph freebusy endpoint keyed by calendarId is not directly
    supported; instead we use the account's primary email + the calendars.
    For multi-calendar support, we iterate per calendar using the direct
    /me/calendars/{id}/calendarView approach as a fallback.
    """
    # MS Graph getSchedule expects email addresses, not calendar UUIDs.
    # For the freebusy use case we use calendarView per calendar instead,
    # which gives us actual busy intervals regardless of transparency:
    # GET /me/calendars/{id}/calendarView?startDateTime=...&endDateTime=...
    # Only return events where showAs is busy/tentative/oof (not free/workingElsewhere).
    BUSY_SHOW_AS = {"busy", "tentative", "oof"}
    start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end.strftime("%Y-%m-%dT%H:%M:%SZ")

    creds = get_credentials(account.user, account.id)
    out: list[tuple[str, datetime, datetime]] = []

    with _bearer(creds) as client:
        for cal_id in calendar_ids:
            url = f"/me/calendars/{cal_id}/calendarView"
            r = client.get(url, params={
                "startDateTime": start_str,
                "endDateTime": end_str,
                "$select": "start,end,showAs",
                "$top": 1000,
            }, headers={"Prefer": 'outlook.timezone="UTC"'})
            if r.status_code != 200:
                # Log and skip this calendar (e.g. permission denied)
                logger.warning(
                    "fetch_freebusy MS calendarView %s returned %s", cal_id, r.status_code
                )
                continue
            for event in r.json().get("value", []):
                if event.get("showAs", "busy").lower() not in BUSY_SHOW_AS:
                    continue
                try:
                    s = _parse_ms_datetime(event["start"]["dateTime"])
                    e = _parse_ms_datetime(event["end"]["dateTime"])
                    out.append((cal_id, s, e))
                except (KeyError, ValueError) as exc:
                    logger.warning("fetch_freebusy MS parse error: %s", exc)
    return out


def _parse_ms_datetime(s: str) -> datetime:
    """Parse MS Graph datetime string (ISO 8601, may lack TZ — Graph returns UTC)."""
    from datetime import timezone as _tz
    dt = datetime.fromisoformat(s.rstrip("Z"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)
    return dt


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
    account_id: int | None = None,
) -> dict:
    """Create an event on ``calendar_id`` (empty = primary calendar) and
    invite ``attendees``. When ``include_online_meeting`` is True (the
    "always online" default), Graph auto-generates a Teams meeting link.
    Graph sends the invite email automatically for events with attendees —
    no ``sendUpdates`` equivalent needed.

    ``account_id`` selects a specific MicrosoftAccount for multi-account users.
    """
    payload: dict = {
        "subject": summary,
        "body": {
            "contentType": "HTML",
            "content": description.replace("\n", "<br>"),
        },
        # Graph time zones follow IANA names when we set the TZ header, and
        # ISO datetimes should be timezone-naive with a separate `timeZone`
        # field. Passing a full ISO with offset works too — Graph converts.
        "start": {"dateTime": start_iso, "timeZone": time_zone},
        "end": {"dateTime": end_iso, "timeZone": time_zone},
        "attendees": [
            {
                "emailAddress": {"address": e},
                "type": "required",
            }
            for e in attendees
            if e
        ],
    }
    if location:
        # Graph's location is a nested object; displayName is what shows up
        # in Outlook and in mobile invites' "location" row.
        payload["location"] = {"displayName": location}
    if include_online_meeting:
        payload["isOnlineMeeting"] = True
        payload["onlineMeetingProvider"] = "teamsForBusiness"

    endpoint = "/me/events" if not calendar_id else f"/me/calendars/{calendar_id}/events"
    creds = get_credentials(user, account_id)
    with _bearer(creds) as client:
        r = client.post(endpoint, json=payload)
    if r.status_code not in (200, 201):
        raise MicrosoftApiError(r.status_code, f"events.create: {r.text[:300]}")
    return r.json()


def delete_calendar_event(
    user,
    *,
    calendar_id: str,
    event_id: str,
    account_id: int | None = None,
) -> None:
    """Delete an event via Graph. Graph auto-emails a cancellation to
    attendees. 404 is treated as success (event already gone).

    ``account_id`` selects a specific MicrosoftAccount for multi-account users.
    """
    endpoint = f"/me/events/{event_id}"  # /me/events works even for non-primary calendars
    creds = get_credentials(user, account_id)
    with _bearer(creds) as client:
        r = client.delete(endpoint)
    if r.status_code in (200, 204, 404):
        return
    raise MicrosoftApiError(r.status_code, f"events.delete: {r.text[:200]}")
