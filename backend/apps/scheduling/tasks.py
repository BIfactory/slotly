"""Celery tasks for OAuth-driven busy-interval sync.

Every connected Google/Microsoft account is polled every 5 minutes (matching
the ICS cadence). For each account, all calendars are listed via calendarList /
/me/calendars, freebusy intervals are fetched for the [now-7d, now+60d] window,
and all OAuthBusyWindow rows for that account are replaced atomically.

Privacy: OAuthBusyWindow stores (starts_at, ends_at) only — no titles,
descriptions, locations, or attendees (PRD §5.2, F15-R9).

Broken tokens (401 / OAuth revoked externally): the task logs a warning,
skips the account, and does NOT delete existing OAuthBusyWindow rows. Stale
data is better than an empty busy list while the token is being repaired.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone as djtz

from .models import GoogleAccount, MicrosoftAccount, OAuthBusyWindow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _window_start():
    return djtz.now() - timedelta(days=7)


def _window_end():
    return djtz.now() + timedelta(days=60)


def _jitter() -> int:
    """0–30s of jitter to spread provider load."""
    return random.randint(0, 30)


# ---------------------------------------------------------------------------
# Per-account sync
# ---------------------------------------------------------------------------

@shared_task(name="scheduling.sync_one_oauth_account")
def sync_one_oauth_account(provider: str, account_pk: int) -> dict:
    """For a single connected OAuth account, list all calendars + fetch busy
    for the search window, then replace all OAuthBusyWindow rows for this account.

    On any auth error (revoked token, 401): logs + returns error dict.
    Prior OAuthBusyWindow rows are preserved — stale is safer than empty.

    New-calendar-mid-cycle: calendarList is re-fetched on every invocation
    (no caching), so newly added calendars appear within one poll cycle.
    """
    if provider == "google":
        return _sync_google(account_pk)
    elif provider == "microsoft":
        return _sync_microsoft(account_pk)
    else:
        logger.error("sync_one_oauth_account: unknown provider %r", provider)
        return {"error": f"unknown provider: {provider}"}


def _sync_google(account_pk: int) -> dict:
    from .google_client import (
        GoogleApiError,
        GoogleOAuthError,
        fetch_freebusy,
        list_all_calendars,
    )

    account = GoogleAccount.objects.filter(pk=account_pk).first()
    if account is None:
        return {"skipped": True, "reason": "account not found"}

    t_start = time.monotonic()
    calendar_ids: list[str] = []
    try:
        calendars = list_all_calendars(account)
        calendar_ids = [c["id"] for c in calendars]
        if not calendar_ids:
            # Account connected but no calendars visible — clear old data.
            OAuthBusyWindow.objects.filter(google_account=account).delete()
            duration_ms = int((time.monotonic() - t_start) * 1000)
            logger.info(
                "event=oauth_busy_sync provider=google account_id=%s"
                " calendars=0 windows_written=0 duration_ms=%s note=no_calendars",
                account_pk, duration_ms,
            )
            return {"account_id": account_pk, "written": 0, "note": "no calendars"}

        # Google allows 50 items per freebusy request — batch if needed.
        busy: list[tuple[str, object, object]] = []
        batch_size = 50
        for i in range(0, len(calendar_ids), batch_size):
            batch = calendar_ids[i : i + batch_size]
            busy.extend(fetch_freebusy(account, batch, _window_start(), _window_end()))

    except (GoogleOAuthError, GoogleApiError) as exc:
        duration_ms = int((time.monotonic() - t_start) * 1000)
        logger.warning(
            "event=oauth_busy_sync provider=google account_id=%s"
            " calendars=%s windows_written=0 duration_ms=%s error=%r",
            account_pk, len(calendar_ids), duration_ms, str(exc)[:200],
        )
        return {"error": str(exc)[:200]}

    with transaction.atomic():
        # select_for_update() serializes concurrent task runs for the same
        # account: without it, two workers could both delete then both
        # bulk_create, producing duplicate rows.
        account = GoogleAccount.objects.select_for_update().get(pk=account_pk)
        OAuthBusyWindow.objects.filter(google_account=account).delete()
        OAuthBusyWindow.objects.bulk_create([
            OAuthBusyWindow(
                user=account.user,
                provider="google",
                google_account=account,
                calendar_id=cid,
                starts_at=s,
                ends_at=e,
            )
            for (cid, s, e) in busy
        ])

    duration_ms = int((time.monotonic() - t_start) * 1000)
    logger.info(
        "event=oauth_busy_sync provider=google account_id=%s"
        " calendars=%s windows_written=%s duration_ms=%s",
        account_pk, len(calendar_ids), len(busy), duration_ms,
    )
    return {"account_id": account_pk, "written": len(busy)}


def _sync_microsoft(account_pk: int) -> dict:
    from .graph_client import (
        MicrosoftApiError,
        MicrosoftOAuthError,
        fetch_freebusy,
        list_all_calendars,
    )

    account = MicrosoftAccount.objects.filter(pk=account_pk).first()
    if account is None:
        return {"skipped": True, "reason": "account not found"}

    t_start = time.monotonic()
    calendar_ids: list[str] = []
    try:
        calendars = list_all_calendars(account)
        calendar_ids = [c["id"] for c in calendars]
        if not calendar_ids:
            OAuthBusyWindow.objects.filter(microsoft_account=account).delete()
            duration_ms = int((time.monotonic() - t_start) * 1000)
            logger.info(
                "event=oauth_busy_sync provider=microsoft account_id=%s"
                " calendars=0 windows_written=0 duration_ms=%s note=no_calendars",
                account_pk, duration_ms,
            )
            return {"account_id": account_pk, "written": 0, "note": "no calendars"}

        busy: list[tuple[str, object, object]] = []
        # MS Graph getSchedule allows up to 20 schedules per request.
        batch_size = 20
        for i in range(0, len(calendar_ids), batch_size):
            batch = calendar_ids[i : i + batch_size]
            busy.extend(fetch_freebusy(account, batch, _window_start(), _window_end()))

    except (MicrosoftOAuthError, MicrosoftApiError) as exc:
        duration_ms = int((time.monotonic() - t_start) * 1000)
        logger.warning(
            "event=oauth_busy_sync provider=microsoft account_id=%s"
            " calendars=%s windows_written=0 duration_ms=%s error=%r",
            account_pk, len(calendar_ids), duration_ms, str(exc)[:200],
        )
        return {"error": str(exc)[:200]}

    with transaction.atomic():
        # select_for_update() serializes concurrent task runs for the same
        # account: without it, two workers could both delete then both
        # bulk_create, producing duplicate rows.
        account = MicrosoftAccount.objects.select_for_update().get(pk=account_pk)
        OAuthBusyWindow.objects.filter(microsoft_account=account).delete()
        OAuthBusyWindow.objects.bulk_create([
            OAuthBusyWindow(
                user=account.user,
                provider="microsoft",
                microsoft_account=account,
                calendar_id=cid,
                starts_at=s,
                ends_at=e,
            )
            for (cid, s, e) in busy
        ])

    duration_ms = int((time.monotonic() - t_start) * 1000)
    logger.info(
        "event=oauth_busy_sync provider=microsoft account_id=%s"
        " calendars=%s windows_written=%s duration_ms=%s",
        account_pk, len(calendar_ids), len(busy), duration_ms,
    )
    return {"account_id": account_pk, "written": len(busy)}


# ---------------------------------------------------------------------------
# Fan-out periodic
# ---------------------------------------------------------------------------

@shared_task(name="scheduling.sync_all_oauth_accounts")
def sync_all_oauth_accounts() -> dict:
    """Enqueue one sync_one_oauth_account per connected OAuth account.

    Iterates all GoogleAccount + MicrosoftAccount rows. No revoked_at filter
    exists — tokens that are broken will be caught and logged at sync time
    without deleting existing OAuthBusyWindow rows.
    """
    queued = 0
    for pk in GoogleAccount.objects.values_list("pk", flat=True):
        sync_one_oauth_account.apply_async(args=["google", pk], countdown=_jitter())
        queued += 1
    for pk in MicrosoftAccount.objects.values_list("pk", flat=True):
        sync_one_oauth_account.apply_async(args=["microsoft", pk], countdown=_jitter())
        queued += 1
    return {"queued": queued}
