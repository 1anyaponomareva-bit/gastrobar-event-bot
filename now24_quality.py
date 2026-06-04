"""
Качество выдачи «24 часа»: не групповые IEM, приоритет F1/NHL/футбол/UFC (включая BJJ).
"""

from __future__ import annotations

import re
from typing import Any

from event_participants import has_matchup_in_title
from event_verifier import bar_event_blob
from watchability import detect_editorial_type

_IEM_GROUP_RE = re.compile(
    r"\biem\b.*\b(?:major|college|cologne|katowice)\b",
    re.I,
)
_ESPORTS_FINAL_STAGE_RE = re.compile(
    r"grand\s+final|semi-?final|playoff|elimination|finals\b|grand\s+finals|"
    r"upper\s+bracket\s+final|lower\s+bracket\s+final",
    re.I,
)


def is_now24_esports_worthy(ev: dict[str, Any]) -> bool:
    """Для 24 ч — только финалы/плей-офф majors, не групповой этап IEM."""
    b = bar_event_blob(ev)
    title = str(ev.get("title", "")).strip()
    ws = int(ev.get("watchability_score", 0))

    if _ESPORTS_FINAL_STAGE_RE.search(b):
        return True
    if re.search(r"blast\s+slam|the\s+international|ti\s*\d|dreamleague\s+final", b, re.I):
        return ws >= 55
    if _IEM_GROUP_RE.search(b) and not _ESPORTS_FINAL_STAGE_RE.search(b):
        return False
    if re.search(r"\b(cs2|dota)\b", b, re.I) and has_matchup_in_title(title):
        if ws >= 78 and re.search(r"blast|dreamleague|major|esl\s+pro", b, re.I):
            return True
        return False
    return ws >= 80


def is_ufc_bjj_event(ev: dict[str, Any]) -> bool:
    b = bar_event_blob(ev)
    return bool(re.search(r"\bufc\b.*\bbjj\b|\bbjj\b.*\bufc\b", b, re.I))


def is_now24_core_headline_sport(ev: dict[str, Any]) -> bool:
    """F1 / NHL / футбол / MMA-UFC — для добора; UFC BJJ не блокирует поиск других."""
    if is_now24_junk_event(ev):
        return False
    if is_ufc_bjj_event(ev):
        return False
    return is_now24_headline_sport(ev)


def is_now24_headline_sport(ev: dict[str, Any]) -> bool:
    """Традиционный спорт для ТВ в баре (не киберспорт). UFC BJJ — да."""
    et = detect_editorial_type(ev)
    b = bar_event_blob(ev)
    if et == "f1":
        return True
    if et == "football":
        return True
    if et == "nhl":
        return True
    if et == "nba" and re.search(r"playoff|finals|conference\s+final", b, re.I):
        return True
    if et == "ufc":
        return has_matchup_in_title(str(ev.get("title", "")))
    return False


def is_now24_junk_event(ev: dict[str, Any]) -> bool:
    """Не показывать в «24 часа»."""
    et = detect_editorial_type(ev)
    if et == "esports" and not is_now24_esports_worthy(ev):
        return True
    return False
