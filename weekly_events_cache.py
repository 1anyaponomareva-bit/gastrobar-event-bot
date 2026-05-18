"""
Weekly Radar — источник правды для Daily Post.
Память + SQLite (radar_snapshots mode=weekly_events_cache).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from database import get_radar_snapshot, save_radar_snapshot

log = logging.getLogger(__name__)

WEEKLY_CACHE_MODE = "weekly_events_cache"

_memory_events: list[dict[str, Any]] = []


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_dedupe_key(e: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(e.get("date", "")).strip(),
        str(e.get("display_time") or e.get("time", "")).strip().lower(),
        str(e.get("title", "")).strip().lower()[:120],
    )


def event_to_cache_record(e: dict[str, Any], *, source: str = "weekly_radar") -> dict[str, Any]:
    from event_participants import extract_participants

    full = dict(e)
    if "display_time" not in full:
        full["display_time"] = (
            str(full.get("time_display") or full.get("time", "")).strip()
        )
    return {
        "title": str(full.get("title", "")).strip(),
        "category": str(full.get("category", "")).strip(),
        "league": str(full.get("league", full.get("subtitle", ""))).strip(),
        "date": str(full.get("date", "")).strip(),
        "time": str(full.get("time", "")).strip(),
        "weekday": str(full.get("weekday", "")).strip(),
        "timezone": str(
            full.get("source_timezone") or full.get("original_timezone") or ""
        ).strip(),
        "confidence": str(full.get("confidence", "medium")).strip(),
        "source": str(full.get("cache_source") or source).strip(),
        "participants": extract_participants(full),
        "created_at": _utc_now_iso(),
        "_event": full,
    }


def record_to_event(record: dict[str, Any]) -> dict[str, Any]:
    if isinstance(record.get("_event"), dict):
        ev = dict(record["_event"])
        ev.setdefault("cache_source", record.get("source", "weekly_radar"))
        return ev
    return dict(record)


async def save_weekly_events_cache(
    events: list[dict[str, Any]],
    *,
    source: str = "weekly_radar",
) -> None:
    global _memory_events
    records = [event_to_cache_record(e, source=source) for e in events]
    _memory_events = [record_to_event(r) for r in records]
    await save_radar_snapshot(
        WEEKLY_CACHE_MODE,
        records,
        {"count": len(records), "updated": _utc_now_iso()},
    )
    log.info("weekly cache saved: %s events", len(_memory_events))


async def load_weekly_events_cache() -> list[dict[str, Any]]:
    global _memory_events
    if _memory_events:
        return list(_memory_events)
    raw = await get_radar_snapshot(WEEKLY_CACHE_MODE)
    if not raw:
        return []
    restored: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            restored.append(record_to_event(item))
    _memory_events = restored
    if restored:
        log.info("weekly cache loaded from db: %s events", len(restored))
    return list(_memory_events)


async def get_weekly_events_cache() -> list[dict[str, Any]]:
    return await load_weekly_events_cache()


def weekly_cache_is_empty() -> bool:
    return not _memory_events


async def merge_events_into_weekly_cache(
    events: list[dict[str, Any]],
    *,
    source: str = "daily_fresh_search",
) -> list[dict[str, Any]]:
    """Добавить новые события в кэш (без дубликатов)."""
    current = await load_weekly_events_cache()
    seen = {_event_dedupe_key(e) for e in current}
    added = 0
    for e in events:
        ev = dict(e)
        ev["cache_source"] = source
        key = _event_dedupe_key(ev)
        if key in seen:
            continue
        seen.add(key)
        current.append(ev)
        added += 1
    if added:
        await save_weekly_events_cache(current, source="weekly_radar_merged")
        log.info("weekly cache merged: +%s events (total=%s)", added, len(current))
    return current
