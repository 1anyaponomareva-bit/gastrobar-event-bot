"""
Кэш последней успешной выборки BetBoom (общий пул до 3 дней).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from database import get_radar_snapshot, save_radar_snapshot

log = logging.getLogger(__name__)

BETBOOM_CACHE_MODE = "betboom_line_3d"
_MEMORY: list[dict[str, Any]] = []
_MEMORY_TS: float = 0.0
_TTL_SEC = 300.0


async def save_betboom_cache(events: list[dict[str, Any]], *, meta: dict[str, Any] | None = None) -> None:
    global _MEMORY, _MEMORY_TS
    _MEMORY = list(events)
    _MEMORY_TS = time.monotonic()
    payload = {
        "count": len(events),
        "meta": meta or {},
    }
    await save_radar_snapshot(BETBOOM_CACHE_MODE, events, payload)
    log.info("BETBOOM cache saved: %s events", len(events))


async def load_betboom_cache(*, allow_stale: bool = True) -> list[dict[str, Any]]:
    global _MEMORY, _MEMORY_TS
    if _MEMORY and (time.monotonic() - _MEMORY_TS) < _TTL_SEC:
        return list(_MEMORY)
    raw = await get_radar_snapshot(BETBOOM_CACHE_MODE)
    if not raw:
        return []
    restored = [x for x in raw if isinstance(x, dict)]
    if restored:
        _MEMORY = restored
        _MEMORY_TS = time.monotonic()
        log.info("BETBOOM_CACHE_USED from db: %s events", len(restored))
    return restored if allow_stale else ([] if not restored else restored)
