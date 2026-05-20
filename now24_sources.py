"""
Источники событий для режима «ближайшие 24 часа» (API-first, без Gemini).
"""

from __future__ import annotations

import logging
from typing import Any

from config import SPORTS_API_KEY
from football_watchability import (
    football_watchability_score,
    is_eligible_football_league_now24,
    passes_gastrobar_football_threshold,
)
from gastrobar_event_filter import gastrobar_football_min_watchability
from next24 import is_in_next24_window, log_next24_window_header, resolve_event_local_datetime_vn
from radar_sports_convert import lock_api_sports_program_item
from sports_events import (
    is_gastrobar_api_sport_worthy,
    raw_event_to_radar_program_item,
)

log = logging.getLogger(__name__)


def is_now24_api_sport_worthy(e: dict[str, Any]) -> bool:
    """Алиас: тот же фильтр, что и для weekly API-пула."""
    return is_gastrobar_api_sport_worthy(e)


def _prepare_now24_event(locked: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    from event_radar import _prepare_for_afisha_selection

    prepared = _prepare_for_afisha_selection(locked)
    sport = str(item.get("sport", "")).lower()
    if sport == "football":
        fb_score, fb_reason = football_watchability_score(item, locked)
        prepared["football_watchability_score"] = fb_score
        prepared["football_watchability_reason"] = fb_reason
        prepared["watchability_score"] = max(
            int(prepared.get("watchability_score", 0)), fb_score
        )
    return prepared


async def _now24_from_sport_fetch(
    fetch_coro,
    *,
    label: str,
) -> list[dict[str, Any]]:
    try:
        raw = await fetch_coro
    except Exception as e:
        log.error("now24 %s fetch failed: %s", label, e)
        return []
    if not isinstance(raw, list):
        return []
    log.info("now24 %s: raw=%s", label, len(raw))
    return raw


async def _now24_filter_pool(
    raw: list[dict[str, Any]],
    *,
    phase: str,
    min_watchability: int | None = None,
    stats: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in raw:
        if stats is not None:
            stats["raw"] = stats.get("raw", 0) + 1
        if not is_now24_api_sport_worthy(e):
            if stats is not None:
                stats["drop_worthy"] = stats.get("drop_worthy", 0) + 1
            continue
        item = raw_event_to_radar_program_item(e)
        locked = lock_api_sports_program_item(item, phase=phase)
        if not locked:
            if stats is not None:
                stats["drop_lock"] = stats.get("drop_lock", 0) + 1
            continue
        if not is_in_next24_window(locked, log_checks=False):
            if stats is not None:
                stats["drop_window"] = stats.get("drop_window", 0) + 1
            continue
        if min_watchability is not None:
            from event_radar import _prepare_for_afisha_selection

            prep = _prepare_for_afisha_selection(locked)
            if int(prep.get("watchability_score", 0)) < min_watchability:
                if stats is not None:
                    stats["drop_score"] = stats.get("drop_score", 0) + 1
                continue
        out.append(_prepare_now24_event(locked, item))
        if stats is not None:
            stats["kept"] = stats.get("kept", 0) + 1
    return out


async def fetch_now24_from_api_sports() -> list[dict[str, Any]]:
    """
    API-SPORTS: футбол, хоккей, NBA, F1 в окне 24 ч (VN).
    """
    if not SPORTS_API_KEY:
        log.info("now24 api: no SPORTS_API_KEY")
        return []

    from sports_events import (
        get_basketball_events,
        get_esports_events,
        get_football_events_next_days_vn,
        get_formula_events,
        get_hockey_events,
    )

    log_next24_window_header()

    # Сегодня + завтра + послезавтра (VN) — чтобы не пропустить вечерние матчи
    football_raw = await _now24_from_sport_fetch(
        get_football_events_next_days_vn(days_ahead=3),
        label="football",
    )
    hockey_raw = await _now24_from_sport_fetch(get_hockey_events(), label="hockey")
    basketball_raw = await _now24_from_sport_fetch(
        get_basketball_events(), label="basketball"
    )
    f1_raw = await _now24_from_sport_fetch(get_formula_events(), label="f1")
    esports_raw = await _now24_from_sport_fetch(get_esports_events(), label="esports")

    log.info(
        "NOW24_API RAW COUNTS football=%s hockey=%s basketball=%s f1_rows=%s esports=%s",
        len(football_raw),
        len(hockey_raw),
        len(basketball_raw),
        len(f1_raw),
        len(esports_raw),
    )

    out: list[dict[str, Any]] = []
    fb_stats: dict[str, int] = {}

    for item in football_raw:
        fb_stats["raw"] = fb_stats.get("raw", 0) + 1
        if not is_eligible_football_league_now24(item):
            fb_stats["drop_league"] = fb_stats.get("drop_league", 0) + 1
            continue
        locked = lock_api_sports_program_item(
            raw_event_to_radar_program_item(item), phase="now24_api_football"
        )
        if not locked:
            fb_stats["drop_lock"] = fb_stats.get("drop_lock", 0) + 1
            continue
        if not is_in_next24_window(locked, log_checks=False):
            fb_stats["drop_window"] = fb_stats.get("drop_window", 0) + 1
            continue
        if not passes_gastrobar_football_threshold(
            item, locked, min_score=gastrobar_football_min_watchability()
        ):
            fb_stats["drop_score"] = fb_stats.get("drop_score", 0) + 1
            continue
        out.append(_prepare_now24_event(locked, item))
        fb_stats["kept"] = fb_stats.get("kept", 0) + 1

    hk_stats: dict[str, int] = {}
    out.extend(
        await _now24_filter_pool(
            hockey_raw,
            phase="now24_api_hockey",
            min_watchability=24,
            stats=hk_stats,
        )
    )
    bb_stats: dict[str, int] = {}
    out.extend(
        await _now24_filter_pool(
            basketball_raw,
            phase="now24_api_basketball",
            min_watchability=24,
            stats=bb_stats,
        )
    )
    f1_stats: dict[str, int] = {}
    out.extend(
        await _now24_filter_pool(
            f1_raw,
            phase="now24_api_f1",
            min_watchability=28,
            stats=f1_stats,
        )
    )
    es_stats: dict[str, int] = {}
    out.extend(
        await _now24_filter_pool(
            esports_raw,
            phase="now24_api_esports",
            min_watchability=24,
            stats=es_stats,
        )
    )

    log.info(
        "NOW24_FILTER football=%s hockey=%s basketball=%s f1=%s esports=%s",
        fb_stats,
        hk_stats,
        bb_stats,
        f1_stats,
        es_stats,
    )

    from radar_dedupe import dedupe_events

    out = dedupe_events(out, log_prefix="now24_api_multi", exact=True)

    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    out.sort(
        key=lambda x: resolve_event_local_datetime_vn(x)
        or datetime.max.replace(tzinfo=tz),
    )
    log.info("NOW24_API AFTER_DEDUPE=%s (exact keys, time-sorted)", len(out))
    for e in out[:12]:
        dt = resolve_event_local_datetime_vn(e)
        log.info(
            "NOW24 KEPT: %s | %s",
            dt.isoformat() if dt else "?",
            e.get("title"),
        )
    return out
