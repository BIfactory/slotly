from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.utils import timezone


@shared_task(name="mcp.purge_old_idempotency")
def purge_old_idempotency() -> int:
    """Delete IdempotencyRecord rows older than 48h. TTL at the row level
    is 24h; this cleans the trailing 24h-48h window so the table doesn't
    grow forever. Returns the count deleted (useful for monitoring)."""
    from .models import IdempotencyRecord

    cutoff = timezone.now() - timedelta(hours=48)
    deleted, _ = IdempotencyRecord.objects.filter(created_at__lt=cutoff).delete()
    return deleted
