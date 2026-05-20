"""
Логирование и мягкий recall для weekly radar (без отката timezone).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from event_participants import has_matchup_in_title
from event_verifier import bar_event_blob

log = logging.getLogger(__name__)


def is_major_search_candidate(e: dict[str, Any]) -> bool:
    """Крупное событие ещё до watchability — для medium soft-lock."""
    b = bar_event_blob(e)
    title = str(e.get("title", "")).strip()
    cat = str(e.get("category", "")).upper()

    if "NBA" in cat or re.search(r"\bnba\b", b, re.I):
        if re.search(r"conference\s+final|finals|playoff", b, re.I):
            return True
    if "HOCKEY" in cat or "NHL" in cat or re.search(r"\bnhl\b|stanley", b, re.I):
        if re.search(r"playoff|conference\s+final|stanley", b, re.I):
            return True
    if re.search(r"formula\s*1|\bf1\b|grand\s+prix", b, re.I):
        if re.search(r"qualifying|sprint|\brace\b", b, re.I):
            return True
    if re.search(r"premier\s+league|\bepl\b|\bucl\b|champions\s+league", b, re.I):
        if has_matchup_in_title(title):
            return True
    if re.search(r"europa\s+league|\buel\b", b, re.I) and has_matchup_in_title(title):
        return True
    if re.search(r"derby|el clasico|clasico|north london|manchester derby", b, re.I):
        return True
    if re.search(r"\bufc\b", b, re.I) and has_matchup_in_title(title):
        return True
    return False


def log_radar_rejection(
    stage: str,
    reason: str,
    event: dict[str, Any],
    *,
    extra: str = "",
) -> None:
    title = str(event.get("title", ""))[:80]
    msg = (
        f"radar_rejected stage={stage} reason={reason} title={title!r} "
        f"date={event.get('original_date') or event.get('date')} "
        f"time={event.get('original_time') or event.get('time')} "
        f"tz={event.get('source_timezone') or event.get('original_timezone')}"
    )
    if extra:
        msg += f" {extra}"
    log.info(msg)


def log_medium_accepted(event: dict[str, Any], *, via: str) -> None:
    log.info(
        "radar_medium_accepted: title=%r confidence=%s via=%s utc=%s local=%s %s",
        event.get("title"),
        event.get("confidence"),
        via,
        event.get("utc_datetime"),
        event.get("local_datetime"),
        event.get("local_time"),
    )


def soft_lock_search_candidate(
    cand: dict[str, Any],
    *,
    phase: str = "soft_medium",
) -> dict[str, Any] | None:
    """
    Medium-confidence path: lock schedule from search fields + trusted TZ inference.
    """
    from locked_time import lock_event_schedule

    if not str(cand.get("time") or cand.get("original_time", "")).strip():
        log_radar_rejection("verify", "missing_datetime", cand)
        return None

    locked = lock_event_schedule(dict(cand), phase=phase)
    if locked is None:
        log_radar_rejection("verify", "lock_schedule_failed", cand)
        return None

    locked["confidence"] = str(locked.get("confidence") or "medium").lower()
    if locked["confidence"] not in ("high", "medium"):
        locked["confidence"] = "medium"
    locked.setdefault("verified_via", "Gemini Search")
    locked.setdefault("verification_reason", "medium_soft_lock")
    log_medium_accepted(locked, via=locked.get("verified_via", "soft_lock"))
    return locked
