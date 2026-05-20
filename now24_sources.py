"""
Источники событий для режима «ближайшие 24 часа» (API-first, без Gemini).
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
from radar_sports_convert import lock_api_sports_program_item
from sports_events import (
    is_weekly_radar_api_worthy,
    raw_event_to_radar_program_item,
)

log = logging.getLogger(__name__)


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
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in raw:
        if not is_weekly_radar_api_worthy(e):
            continue
        item = raw_event_to_radar_program_item(e)
        locked = lock_api_sports_program_item(item, phase=phase)
        if not locked:
            continue
        if not is_in_next24_window(locked, log_checks=False):
            continue
        if min_watchability is not None:
            from event_radar import _prepare_for_afisha_selection

            prep = _prepare_for_afisha_selection(locked)
            if int(prep.get("watchability_score", 0)) < min_watchability:
                continue
        out.append(_prepare_now24_event(locked, item))
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

    football_raw = await _now24_from_sport_fetch(
        get_football_events_next_days_vn(days_ahead=2),
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

    for item in football_raw:
        if not is_eligible_football_league_now24(item):
            continue
        locked = lock_api_sports_program_item(
            raw_event_to_radar_program_item(item), phase="now24_api_football"
        )
        if not locked or not is_in_next24_window(locked, log_checks=False):
            continue
        if not passes_now24_football_threshold(
            item, locked, min_score=NOW24_FOOTBALL_MIN_WATCHABILITY
        ):
            continue
        out.append(_prepare_now24_event(locked, item))

    out.extend(
        await _now24_filter_pool(
            hockey_raw,
            phase="now24_api_hockey",
            min_watchability=32,
        )
    )
    out.extend(
        await _now24_filter_pool(
            basketball_raw,
            phase="now24_api_basketball",
            min_watchability=32,
        )
    )
    out.extend(
        await _now24_filter_pool(
            f1_raw,
            phase="now24_api_f1",
            min_watchability=38,
        )
    )
    out.extend(
        await _now24_filter_pool(
            esports_raw,
            phase="now24_api_esports",
            min_watchability=32,
        )
    )

    from radar_dedupe import dedupe_events

    out = dedupe_events(out, log_prefix="now24_api_multi", exact=True)

    from datetime import datetime
    from zoneinfo import ZoneInfo

    from next24 import resolve_event_local_datetime_vn

    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    out.sort(
        key=lambda x: resolve_event_local_datetime_vn(x)
        or datetime.max.replace(tzinfo=tz),
    )
    log.info("NOW24_API AFTER_DEDUPE=%s (exact keys, time-sorted)", len(out))
    return out
