"""
Единые фильтры событий Gastrobar для weekly, now24 и daily.
Различается только временное окно (неделя / 24 ч / campaign_post_date).
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def gastrobar_football_min_watchability() -> int:
    """Один порог футбола для weekly, now24 и daily."""
    from config import RADAR_MIN_WATCHABILITY

    return RADAR_MIN_WATCHABILITY


def apply_gastrobar_football_gate(ev: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """
    Лига + football_watchability_score. Обновляет ev при успехе.
    """
    from football_watchability import (
        football_watchability_score,
        is_eligible_football_league_now24,
        passes_gastrobar_football_threshold,
    )

    if str(ev.get("category", "")).upper() != "FOOTBALL" or ev.get("league_id") is None:
        return True, ev

    item = {
        "league_id": ev.get("league_id"),
        "league_country": ev.get("league_country", ""),
        "league": ev.get("league") or ev.get("subtitle", ""),
        "title": ev.get("title", ""),
    }
    fb_score = ev.get("football_watchability_score")
    if fb_score is None:
        if not is_eligible_football_league_now24(item):
            return False, ev
        fb_score, fb_reason = football_watchability_score(item, ev)
        ev = dict(ev)
        ev["football_watchability_score"] = fb_score
        ev["football_watchability_reason"] = fb_reason
    else:
        fb_score = int(fb_score)

    min_score = gastrobar_football_min_watchability()
    if not passes_gastrobar_football_threshold(item, ev, min_score=min_score):
        return False, ev

    ev = dict(ev)
    ev["watchability_score"] = max(int(ev.get("watchability_score", 0)), int(fb_score))
    return True, ev


def passes_gastrobar_participant_gate(ev: dict[str, Any]) -> bool:
    from event_participants import is_gastrobar_eligible, passes_participant_rules
    from event_verifier import gastrobar_hard_reject
    from locked_time import has_locked_schedule

    if gastrobar_hard_reject(ev):
        return False

    if has_locked_schedule(ev):
        ok_part, _ = passes_participant_rules(ev)
        return ok_part

    if str(ev.get("verified_via", "")).upper() == "API-SPORTS":
        if gastrobar_hard_reject(ev):
            return False
        ok_part, _ = passes_participant_rules(ev)
        return ok_part

    if int(ev.get("radar_tier", 99)) >= 99 and int(ev.get("watchability_score", 0)) < 52:
        return False
    return is_gastrobar_eligible(ev)


def passes_gastrobar_content_filters(
    e: dict[str, Any],
    *,
    enrich: bool = True,
) -> tuple[bool, dict[str, Any]]:
    """Отбраковка по качеству/участникам — одинакова для week и daily."""
    from event_verifier import gastrobar_hard_reject
    from watchability import enrich_watchability

    ev = enrich_watchability(dict(e)) if enrich else dict(e)
    if gastrobar_hard_reject(ev):
        return False, ev

    ok_fb, ev = apply_gastrobar_football_gate(ev)
    if not ok_fb:
        return False, ev

    if not passes_gastrobar_participant_gate(ev):
        return False, ev

    return True, ev


def passes_gastrobar_watchability_floor(e: dict[str, Any]) -> bool:
    """Порог watchability + major-event bypass (weekly и now24 после контент-фильтра)."""
    from config import RADAR_MIN_WATCHABILITY
    from watchability import is_major_weekly_event, min_watchability_for_event

    floor = min_watchability_for_event(e, default_min=RADAR_MIN_WATCHABILITY)
    score = int(e.get("watchability_score", 0))
    if score >= floor:
        return True
    if is_major_weekly_event(e) and score >= max(16, floor - 16):
        return True
    return False
