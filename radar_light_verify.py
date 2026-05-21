"""
Лёгкая проверка событий из Gemini Search (scout layer).

Проверяет только: title, datetime, окно недели, не историческое, категория узнаваема.
Без API-Sports exact match и без strict radar gate.
"""

from __future__ import annotations

import logging
from typing import Any

from event_verifier import gastrobar_hard_reject

log = logging.getLogger(__name__)


def _discovery_category_ok(e: dict[str, Any]) -> bool:
    from radar_current_week import allows_gemini_discovery_only
    from radar_rules import detect_sport, rule_watchability_tier

    if allows_gemini_discovery_only(e):
        return True
    sport = detect_sport(e)
    if sport and sport != "other":
        return rule_watchability_tier(e) != "skip"
    return False


def light_verify_discovery_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """
    Python verify для Gemini discovery — не редактор, только гигиена полей.
    """
    from radar_current_week import detect_historical_hallucination
    from radar_rules import detect_sport, emoji_for_sport
    from event_verifier import event_from_search_candidate

    title = str(event.get("title", "")).strip()
    if not title or len(title) < 3:
        return None
    if gastrobar_hard_reject(event):
        return None

    hist = detect_historical_hallucination(event)
    if hist:
        log.info("light_verify drop: %s title=%r", hist, title[:80])
        return None

    out = event_from_search_candidate(
        event,
        confidence=str(event.get("confidence", "medium")).lower() or "medium",
        verified_via=str(event.get("verified_via", "Gemini Search")),
        verification_reason="gemini_discovery_light",
    )
    if out is None:
        from radar_recall import is_major_search_candidate, soft_lock_search_candidate

        if is_major_search_candidate(event):
            out = soft_lock_search_candidate(event, phase="light_verify_major")
        if out is None:
            return None

    if not _discovery_category_ok(out):
        log.info("light_verify drop: category_unrecognized title=%r", title[:80])
        return None

    sport = detect_sport(out)
    out["sport"] = sport
    out["emoji"] = emoji_for_sport(sport, out)
    out["discovery_layer"] = "gemini_search"
    out.setdefault("verified_via", "Gemini Search")
    if not str(out.get("why", "")).strip():
        out["why"] = str(event.get("why", "")).strip() or "Gemini Search discovery"

    log.info(
        "light_verify ok: title=%r sport=%s via=%s local=%s",
        title[:60],
        sport,
        out.get("verified_via"),
        out.get("local_datetime"),
    )
    return out
