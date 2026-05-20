"""
Watchability для football now24: только топ-лиги и матчи, интересные гостям Gastrobar.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from event_participants import has_matchup_in_title
from event_verifier import bar_event_blob
from watchability import DERBY_MARKERS, FOOTBALL_TOP_CLUBS, LONDON_CLUBS, _count_tokens

log = logging.getLogger(__name__)

# API-SPORTS league id -> обязательная страна (domestic top flight)
_DOMESTIC_TOP: dict[int, str] = {
    39: "england",  # Premier League
    140: "spain",  # La Liga
    135: "italy",  # Serie A
    78: "germany",  # Bundesliga
    61: "france",  # Ligue 1
}

_UEFA_CUPS = frozenset({2, 3, 848})  # UCL, UEL, UECL

# Международные турниры (сборные / финалы)
_INTL_TOURNAMENTS = frozenset(
    {
        1,  # World Cup
        4,  # Euro Championship
        5,  # UEFA Nations League
        9,  # Copa America
        32,  # WC Qualification Europe
        960,  # Euro Qualification
        29,  # WC Qualification Africa
        30,  # WC Qualification Asia
        31,  # WC Qualification CONCACAF
        33,  # WC Qualification South America
    }
)

_CHAMPIONSHIP_ENGLAND = 40

_LEAGUE_BASE_SCORE: dict[int, int] = {
    2: 88,
    3: 76,
    848: 68,
    39: 78,
    140: 72,
    135: 72,
    78: 72,
    61: 68,
    _CHAMPIONSHIP_ENGLAND: 56,
    1: 92,
    4: 90,
    5: 74,
    9: 82,
}

_TOP_NATIONAL_TEAMS = (
    "russia",
    "england",
    "france",
    "germany",
    "spain",
    "italy",
    "portugal",
    "netherlands",
    "belgium",
    "croatia",
    "argentina",
    "brazil",
    "uruguay",
    "colombia",
    "mexico",
    "usa",
    "japan",
    "south korea",
    "australia",
)

_REJECT_LEAGUE_RE = re.compile(
    r"u-?21|u-?19|u-?18|u-?23|youth|women|female|reserve|amateur|"
    r"third\s+league|fourth\s+division|regional|qualification\s+—\s+oceania|"
    r"friendlies\s+clubs|lowland|highland|primera\s+b|segunda\s+b",
    re.I,
)

_PLAYOFF_FINAL_RE = re.compile(
    r"semi-?final|quarter-?final|play-?off|final|round\s+of\s+16|last\s+16|"
    r"полуфинал|финал|плей-?офф",
    re.I,
)


def _league_id(item: dict[str, Any]) -> int | None:
    raw = item.get("league_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _blob(item: dict[str, Any], event: dict[str, Any] | None = None) -> str:
    parts = [
        str(item.get("league", "")),
        str(item.get("league_country", "")),
        str(item.get("title", "")),
    ]
    if event:
        parts.extend(
            [
                str(event.get("league", "")),
                str(event.get("subtitle", "")),
                str(event.get("title", "")),
            ]
        )
    return " ".join(parts).lower()


def is_eligible_football_league_now24(item: dict[str, Any]) -> bool:
    """Жёсткий allowlist: только топ-лиги / еврокубки / крупные сборные."""
    league = str(item.get("league", "")).lower()
    if _REJECT_LEAGUE_RE.search(league):
        return False

    lid = _league_id(item)
    if lid is None:
        return False

    country = str(item.get("league_country", "")).strip().lower()

    if lid == _CHAMPIONSHIP_ENGLAND:
        return country == "england"

    if lid in _DOMESTIC_TOP:
        return country == _DOMESTIC_TOP[lid]

    if lid in _UEFA_CUPS or lid in _INTL_TOURNAMENTS:
        return True

    return False


def football_watchability_score(
    item: dict[str, Any],
    event: dict[str, Any] | None = None,
) -> tuple[int, str]:
    """
    0–100 для now24. 0 = не показывать.
    Учитывает league_id, узнаваемые клубы, дерби, плей-офф.
    """
    if not is_eligible_football_league_now24(item):
        return 0, "league_not_allowed"

    b = _blob(item, event)
    title = str((event or item).get("title", "")).strip()
    league = str(item.get("league", "")).lower()
    lid = _league_id(item) or 0

    if _REJECT_LEAGUE_RE.search(b):
        return 0, "rejected_marker"

    score = _LEAGUE_BASE_SCORE.get(lid, 50)
    reasons: list[str] = [f"league_{lid}"]

    if lid in _UEFA_CUPS:
        reasons.append("uefa")
        if _PLAYOFF_FINAL_RE.search(league):
            score += 12
            reasons.append("knockout")
    elif lid in _INTL_TOURNAMENTS:
        reasons.append("intl")
        nations = _count_tokens(b + " " + title.lower(), _TOP_NATIONAL_TEAMS)
        if nations >= 1:
            score += min(nations * 16, 32)
            reasons.append(f"nations×{nations}")
        else:
            score -= 25
            reasons.append("obscure_nations")

    if has_matchup_in_title(title):
        score += 10
        reasons.append("matchup")

    clubs = _count_tokens(b + " " + title.lower(), FOOTBALL_TOP_CLUBS)
    if clubs:
        score += min(clubs * 12, 36)
        reasons.append(f"top_clubs×{clubs}")
    elif lid in _UEFA_CUPS:
        # Еврокубок без «супер-бренда»: для бара всё равно сильный контент
        score = max(score, 50)
        reasons.append("uefa_match_any")

    if any(m in b for m in DERBY_MARKERS):
        score += 18
        reasons.append("derby")

    london_hits = sum(1 for c in LONDON_CLUBS if c in b or c in title.lower())
    if london_hits >= 2 and has_matchup_in_title(title):
        score += 16
        reasons.append("london_derby")

    if _PLAYOFF_FINAL_RE.search(league) and (clubs or lid in _DOMESTIC_TOP):
        score += 10
        reasons.append("playoff_stage")

    # «Premier League» без England — отсекаем на уровне eligibility, но страховка
    if "premier league" in league and str(item.get("league_country", "")).lower() != "england":
        return 0, "fake_premier_league"

    if "ligue 1" in league and country != "france":
        return 0, "ligue1_not_france"

    score = min(100, max(0, score))
    if clubs == 0 and lid in _DOMESTIC_TOP and not _PLAYOFF_FINAL_RE.search(league):
        # Два малоизвестных клуба даже в АПЛ — слабо для now24
        if score < 55:
            score = max(0, score - 8)
            reasons.append("weak_domestic_pair")

    return score, "+".join(reasons)


def passes_now24_football_threshold(
    item: dict[str, Any],
    event: dict[str, Any] | None = None,
    *,
    min_score: int,
) -> bool:
    score, reason = football_watchability_score(item, event)
    ok = score >= min_score
    if not ok:
        log.info(
            "now24 football skip: title=%r league_id=%s score=%s min=%s reason=%s",
            (event or item).get("title"),
            item.get("league_id"),
            score,
            min_score,
            reason,
        )
    return ok
