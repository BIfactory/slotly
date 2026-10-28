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
    """Enforces per-token rate limits with two tiers:

    Read tier (default): 60 req/min, 1 000 req/hour.
    Write tier (write_external tools): 20 req/min, 100 req/hour.

    Write-tier Redis keys use a ``-w`` suffix so they don't collide with
    read-tier keys for the same token in the same window.
    """

    # (window_label, max_requests, window_seconds)
    LIMITS = (
        ("1m", 60, 60),
        ("1h", 1000, 3600),
    )
    # Tighter limits for write_external tool calls.
    WRITE_LIMITS = (
        ("1m-w", 20, 60),
        ("1h-w", 100, 3600),
    )

    def __init__(self):
        self._retry_after: int = 0

    def allow_request(self, request, view) -> bool:  # type: ignore[override]
        token = getattr(request, "auth", None)
        if not isinstance(token, APIToken):
            # Not an MCP token — let auth handle the rejection.
            return True

        # Determine tier from the incoming tool call. ToolsListView has no
        # body; tools/call has `name` in body.
        is_write = False
        if request.method == "POST" and request.path.rstrip("/").endswith("/tools/call"):
            tool_name = request.data.get("name") if isinstance(request.data, dict) else None
            if isinstance(tool_name, str):
                from .tools import get_tool
                tool = get_tool(tool_name)
                if tool is not None and tool.sensitivity == "write_external":
                    is_write = True

        limits = self.WRITE_LIMITS if is_write else self.LIMITS

        now = int(time.time())
        self._retry_after = 0

        for label, limit, window in limits:
            bucket = now // window
            key = f"mcp:rl:{token.pk}:{label}:{bucket}"

            # Atomic path: try to insert the key with value 1 (first request in
            # this window).  cache.add() is atomic on Redis, LocMemCache, and
            # Memcached — it only sets the key if it does not already exist.
            added = cache.add(key, 1, timeout=window + 5)
            if added:
                # Key was absent; this is the first request in the window.
                continue

            # Key already existed.  Atomically increment and get the new count.
            try:
                count = cache.incr(key)
            except Exception:
                # Cache unavailable (e.g. incr on a missing key after a race,
                # or a backend error) — fail open to avoid blocking legitimate
                # requests due to infrastructure issues.
                continue

            if count > limit:
                # Reject: compute seconds until this window expires.
                self._retry_after = window - (now % window)
                return False

        return True

    def wait(self) -> int:  # type: ignore[override]
        return self._retry_after
