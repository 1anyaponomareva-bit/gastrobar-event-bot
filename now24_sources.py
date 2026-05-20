"""
Источники событий для режима «ближайшие 24 часа» (без Gemini).
"""

from __future__ import annotations

import logging
from typing import Any

from config import NOW24_FOOTBALL_MIN_WATCHABILITY, SPORTS_API_KEY
from football_watchability import (
    football_watchability_score,
    is_eligible_football_league_now24,
    passes_now24_football_threshold,
)
from next24 import is_in_next24_window, log_next24_window_header
from radar_sports_convert import lock_football_fixture_event

log = logging.getLogger(__name__)


async def fetch_now24_from_api_sports() -> list[dict[str, Any]]:
    """
    Футбол API-SPORTS: сегодня+завтра (VN). Топ-лиги + watchability >= порог.
    """
    if not SPORTS_API_KEY:
        log.info("now24 api: no SPORTS_API_KEY")
        return []

    from sports_events import get_football_events_next_days_vn

    log_next24_window_header()
    raw = await get_football_events_next_days_vn(days_ahead=2)
    log.info("now24 api: football raw=%s", len(raw))

    out: list[dict[str, Any]] = []
    stats = {"eligible": 0, "locked": 0, "in24h": 0, "scored": 0}

    for item in raw:
        if not is_eligible_football_league_now24(item):
            continue
        stats["eligible"] += 1

        locked = lock_football_fixture_event(item, phase="now24_api_sports")
        if not locked:
            log.info("now24 api: lock failed title=%r", item.get("title"))
            continue
        stats["locked"] += 1

        if not is_in_next24_window(locked, log_checks=False):
            continue
        stats["in24h"] += 1

        fb_score, fb_reason = football_watchability_score(item, locked)
        if fb_score < NOW24_FOOTBALL_MIN_WATCHABILITY:
            log.info(
                "now24 api: low football_watchability title=%r score=%s min=%s %s",
                locked.get("title"),
                fb_score,
                NOW24_FOOTBALL_MIN_WATCHABILITY,
                fb_reason,
            )
            continue
        stats["scored"] += 1

        from event_radar import _prepare_for_afisha_selection

        prepared = _prepare_for_afisha_selection(locked)
        prepared["football_watchability_score"] = fb_score
        prepared["football_watchability_reason"] = fb_reason
        prepared["watchability_score"] = max(
            int(prepared.get("watchability_score", 0)), fb_score
        )
        out.append(prepared)
        log.info(
            "now24 api: kept title=%r fb_score=%s vn=%s %s %s",
            locked.get("title"),
            fb_score,
            locked.get("local_weekday"),
            locked.get("local_time"),
            fb_reason,
        )

    out.sort(
        key=lambda x: (
            -int(x.get("football_watchability_score", 0)),
            -int(x.get("watchability_score", 0)),
            str(x.get("local_date") or x.get("date", "")),
            str(x.get("local_time") or x.get("time", "")),
        )
    )
    log.info(
        "now24 api: raw=%s eligible=%s locked=%s in24h=%s passed_score=%s final=%s",
        len(raw),
        stats["eligible"],
        stats["locked"],
        stats["in24h"],
        stats["scored"],
        len(out),
    )
    return out
