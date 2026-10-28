from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone

from .models import APIToken, IdempotencyRecord

TTL_HOURS = 24


def lookup(token: APIToken, key: str) -> dict[str, Any] | None:
    """Return the cached response for (token, key) if it exists and is
    within TTL; else None. Keys older than TTL are treated as absent
    (the Celery beat task eventually deletes them)."""
    cutoff = timezone.now() - timedelta(hours=TTL_HOURS)
    rec = (
        IdempotencyRecord.objects
        .filter(token=token, key=key, created_at__gte=cutoff)
        .first()
    )
    return rec.response_json if rec else None


def store(
    token: APIToken,
    key: str,
    capability: str,
    response: dict[str, Any],
    *,
    booking_uuid: str | None = None,
) -> None:
    """Persist a successful response under (token, key). If a row already
    exists for this (token, key) — i.e. a race between two concurrent
    lookups — IntegrityError is swallowed. The first write wins."""
    from django.db import IntegrityError, transaction

    try:
        with transaction.atomic():
            IdempotencyRecord.objects.create(
                token=token,
                key=key,
                capability=capability,
                response_json=response,
                booking_uuid=booking_uuid,
            )
    except IntegrityError:
        # Concurrent write race — the other request already stored a
        # response for this key. Caller's response is dropped; next
        # lookup will serve the stored one.
        pass
