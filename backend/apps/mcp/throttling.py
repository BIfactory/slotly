from __future__ import annotations

"""MCP per-token rate limiting via DRF BaseThrottle.

Implementation choice: **fixed-window via django.core.cache** (the project's
`bridge:rl:*` pattern).  django_redis is not in requirements.txt, so the
sorted-set approach would need an extra dependency.  The fixed-window approach
is explicitly acceptable per the task brief and is production-ready for single-
worker deployments.  In multi-worker production you should run with a Redis
cache backend (already configured) which automatically coordinates across
workers.

Key format: ``mcp:rl:<token_pk>:<window_label>:<bucket>``
  where ``bucket = now // window_seconds`` makes each key cover exactly one
  fixed window.

Limits enforced:
  - 60 requests per 60 s  (minute window)
  - 1 000 requests per 3 600 s (hour window)
"""

import time

from django.core.cache import cache
from rest_framework.throttling import BaseThrottle

from .models import APIToken


class MCPTokenThrottle(BaseThrottle):
    """Enforces 60 req/min AND 1 000 req/hour per APIToken."""

    # (window_label, max_requests, window_seconds)
    LIMITS = (
        ("1m", 60, 60),
        ("1h", 1000, 3600),
    )

    def __init__(self):
        self._retry_after: int = 0

    def allow_request(self, request, view) -> bool:  # type: ignore[override]
        token = getattr(request, "auth", None)
        if not isinstance(token, APIToken):
            # Not an MCP token — let auth handle the rejection.
            return True

        now = int(time.time())
        self._retry_after = 0

        for label, limit, window in self.LIMITS:
            bucket = now // window
            key = f"mcp:rl:{token.pk}:{label}:{bucket}"

            count = cache.get(key, 0)
            if count >= limit:
                # Reject: compute seconds until this window expires.
                self._retry_after = window - (now % window)
                return False

            # Increment the counter; set TTL slightly longer than the window so
            # the key is cleaned up automatically even if the window boundary
            # falls between the get and the set.
            try:
                cache.set(key, count + 1, timeout=window + 5)
            except Exception:
                # Cache unavailable — fail open to avoid blocking legitimate
                # requests due to infrastructure issues.
                pass

        return True

    def wait(self) -> int:  # type: ignore[override]
        return self._retry_after
