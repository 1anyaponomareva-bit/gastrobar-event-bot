"""API-SPORTS: не более 1 запроса/сек (Free plan / rateLimit)."""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)

_MIN_INTERVAL_SEC = 1.5
_last_request_ts: float = 0.0
_lock = asyncio.Lock()


async def throttle_before_request() -> None:
    """Ждать до следующего слота (глобально для всех sports endpoints)."""
    global _last_request_ts
    async with _lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL_SEC - (now - _last_request_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_ts = time.monotonic()


def is_rate_limit_error(err: object) -> bool:
    s = str(err).lower()
    return "ratelimit" in s or "rate limit" in s or "too many requests" in s
