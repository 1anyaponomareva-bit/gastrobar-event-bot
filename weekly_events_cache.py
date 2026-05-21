"""

Weekly Radar — источник правды для Daily Post.

Память + SQLite (radar_snapshots mode=weekly_events_cache).

"""



from __future__ import annotations



import logging

import re

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from typing import Any



from database import get_radar_snapshot, save_radar_snapshot



log = logging.getLogger(__name__)



WEEKLY_CACHE_MODE = "weekly_events_cache"

WEEKLY_CACHE_SCHEMA = 2



_memory_events: list[dict[str, Any]] = []





def _utc_now_iso() -> str:

    return datetime.now(timezone.utc).isoformat()





def _event_dedupe_key(e: dict[str, Any]) -> tuple[str, str, str]:

    from radar_dedupe import radar_dedupe_key



    return radar_dedupe_key(e)





def is_digest_football_row(e: dict[str, Any]) -> bool:

    """Старые строки «Premier League Final Day» — не показывать."""

    sub = f"{e.get('subtitle', '')} {e.get('league', '')}".lower()

    return "final day" in sub





def cache_row_looks_stale(e: dict[str, Any]) -> bool:

    """Устаревший кэш: EPL в 22:00 VN без исходной зоны (типичная ошибка Gemini)."""

    if is_digest_football_row(e):

        return True

    blob = " ".join(

        str(e.get(k, ""))

        for k in ("title", "subtitle", "league", "category", "why")

    ).lower()

    if not re.search(r"premier\s+league|\bepl\b", blob):

        return False

    vn = str(e.get("local_time") or e.get("time", "")).strip()

    if vn.startswith("22:"):

        orig = str(e.get("original_timezone") or e.get("source_timezone", "")).strip()

        if not orig or orig in ("Asia/Ho_Chi_Minh", "UTC"):

            return True

    src = str(e.get("original_timezone") or e.get("source_timezone", "")).lower()
    if "istanbul" in src and re.search(r"europa|uel|ucl|premier", blob):
        return True

    return False





def refresh_cache_event(e: dict[str, Any]) -> dict[str, Any] | None:
    from locked_time import (
        has_locked_schedule,
        lock_event_schedule,
        reapply_local_from_utc,
    )

    if is_digest_football_row(e):
        return None

    ev = dict(e)
    ev["cache_schema"] = WEEKLY_CACHE_SCHEMA

    if str(ev.get("utc_datetime", "")).strip():
        fixed = reapply_local_from_utc(ev)
        if fixed:
            if cache_row_looks_stale(fixed):
                return None
            return fixed

    if has_locked_schedule(ev) and not cache_row_looks_stale(ev):
        return ev

    if cache_row_looks_stale(ev):
        return None

    if ev.get("original_date") and ev.get("original_time"):
        locked = lock_event_schedule(ev, phase="cache_refresh")
        if locked:
            return locked

    locked = lock_event_schedule(ev, phase="cache_refresh_fallback")
    return locked if locked else ev





def sanitize_weekly_cache(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from radar_dedupe import dedupe_events

    out: list[dict[str, Any]] = []

    for raw in events:

        ev = refresh_cache_event(raw)

        if ev:

            out.append(ev)

    out = dedupe_events(out, log_prefix="weekly_cache_sanitize")

    from event_radar_pipeline import (
        RadarPipelineStats,
        filter_low_quality_only,
        in_time_window,
        normalize_radar_event,
        sort_events_chronological,
    )

    before_cw = len(out)
    stats = RadarPipelineStats(label="weekly_cache")
    kept: list[dict[str, Any]] = []
    for ev in out:
        ne = normalize_radar_event(ev)
        if ne is None:
            stats.drop("bad_datetime", event=ev)
            continue
        if not in_time_window(ne, "next72"):
            stats.drop("outside_window", event=ne)
            continue
        kept.append(ne)
    out = sort_events_chronological(filter_low_quality_only(kept, stats))
    if len(out) < before_cw:
        log.info(
            "weekly cache rule filter: kept=%s dropped=%s",
            len(out),
            before_cw - len(out),
        )

    if len(out) < len(events):

        log.info(

            "weekly cache sanitized: kept=%s dropped=%s",

            len(out),

            len(events) - len(out),

        )

    return out





def weekly_cache_is_usable(events: list[dict[str, Any]]) -> bool:

    if not events:

        return False

    stale = sum(1 for e in events if is_digest_football_row(e))

    if stale >= len(events):

        log.warning("weekly cache unusable: all rows are digest placeholders")

        return False

    return True





def event_to_cache_record(e: dict[str, Any], *, source: str = "weekly_radar") -> dict[str, Any]:

    from event_participants import extract_participants

    from locked_time import has_locked_schedule, lock_event_schedule



    full = dict(e)

    full["cache_schema"] = WEEKLY_CACHE_SCHEMA

    if str(full.get("utc_datetime", "")).strip():
        from locked_time import reapply_local_from_utc

        fixed = reapply_local_from_utc(full)
        if fixed:
            full = fixed
    elif not has_locked_schedule(full):
        applied = lock_event_schedule(full, phase="weekly_cache_save")
        if applied:
            full = applied

    if "display_time" not in full:

        full["display_time"] = (

            str(full.get("time_display") or full.get("local_time") or full.get("time", "")).strip()

        )

    return {

        "cache_schema": WEEKLY_CACHE_SCHEMA,

        "title": str(full.get("title", "")).strip(),

        "category": str(full.get("category", "")).strip(),

        "league": str(full.get("league", full.get("subtitle", ""))).strip(),

        "date": str(full.get("local_date") or full.get("date", "")).strip(),

        "time": str(full.get("local_time") or full.get("time", "")).strip(),

        "weekday": str(full.get("local_weekday") or full.get("weekday", "")).strip(),

        "utc_datetime": str(full.get("utc_datetime", "")).strip(),

        "local_datetime": str(full.get("local_datetime", "")).strip(),

        "timezone": str(full.get("timezone") or "Asia/Ho_Chi_Minh").strip(),

        "confidence": str(full.get("confidence", "medium")).strip(),

        "source": str(full.get("cache_source") or source).strip(),

        "participants": extract_participants(full),

        "created_at": _utc_now_iso(),

        "_event": full,

    }





def record_to_event(record: dict[str, Any]) -> dict[str, Any]:
    from locked_time import has_locked_schedule, lock_event_schedule, reapply_local_from_utc

    if isinstance(record.get("_event"), dict):
        ev = dict(record["_event"])
        ev.setdefault("cache_schema", record.get("cache_schema", 0))
        ev.setdefault("cache_source", record.get("source", "weekly_radar"))
        if record.get("utc_datetime") and not ev.get("utc_datetime"):
            ev["utc_datetime"] = record["utc_datetime"]
        if record.get("local_datetime") and not ev.get("local_datetime"):
            ev["local_datetime"] = record["local_datetime"]
        if str(ev.get("utc_datetime", "")).strip():
            fixed = reapply_local_from_utc(ev)
            if fixed:
                return fixed
        if has_locked_schedule(ev):
            return ev
        applied = lock_event_schedule(ev, phase="weekly_cache_load")
        return applied if applied else ev

    return dict(record)





async def save_weekly_events_cache(

    events: list[dict[str, Any]],

    *,

    source: str = "weekly_radar",

) -> None:

    global _memory_events

    records = [event_to_cache_record(e, source=source) for e in events]

    _memory_events = sanitize_weekly_cache([record_to_event(r) for r in records])

    await save_radar_snapshot(

        WEEKLY_CACHE_MODE,

        records,

        {

            "count": len(records),

            "updated": _utc_now_iso(),

            "cache_schema": WEEKLY_CACHE_SCHEMA,

        },

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

    restored = sanitize_weekly_cache(restored)

    if restored and not weekly_cache_is_usable(restored):
        log.warning("weekly cache in db is stale — clearing")
        await clear_weekly_events_cache()
        return []

    _memory_events = restored

    if restored:
        log.info("weekly cache loaded from db: %s events", len(restored))

    return list(_memory_events)





async def get_weekly_events_cache() -> list[dict[str, Any]]:

    return await load_weekly_events_cache()





async def get_weekly_events_cache_for_display() -> list[dict[str, Any]]:
    """Кэш для афиши: без digest, UTC→VN без London/BST, топ-футбол из API-SPORTS."""
    events = await load_weekly_events_cache()
    if not weekly_cache_is_usable(events):
        return []
    from weekly_football_times import enrich_weekly_football_times

    return await enrich_weekly_football_times(events)


async def weekly_cache_updated_today_vn() -> bool:
    """Кэш weekly_events_cache обновлялся сегодня (календарный день Asia/Ho_Chi_Minh)."""
    from database import get_radar_snapshot_meta

    meta = await get_radar_snapshot_meta(WEEKLY_CACHE_MODE)
    updated = str(meta.get("updated", "")).strip()
    if not updated:
        return False
    try:
        s = updated.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        vn = ZoneInfo("Asia/Ho_Chi_Minh")
        return dt.astimezone(vn).date() == datetime.now(vn).date()
    except ValueError:
        return False





async def clear_weekly_events_cache() -> None:

    global _memory_events

    _memory_events = []

    await save_radar_snapshot(WEEKLY_CACHE_MODE, [], {"cleared": _utc_now_iso()})

    log.info("weekly cache cleared")





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


