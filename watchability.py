"""
Watchability score — «что реально смотреть в баре», не только финалы.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from bar_hours import is_f1_excluded_event
from event_participants import has_matchup_in_title
from event_verifier import bar_event_blob, gastrobar_hard_reject

log = logging.getLogger(__name__)

FOOTBALL_TOP_CLUBS = (
    "arsenal",
    "liverpool",
    "manchester city",
    "manchester united",
    "chelsea",
    "tottenham",
    "barcelona",
    "real madrid",
    "atletico",
    "atleti",
    "bayern",
    "dortmund",
    "leipzig",
    "inter milan",
    "inter ",
    "ac milan",
    "juventus",
    "napoli",
    "roma",
    "psg",
    "paris saint",
    "marseille",
    "lyon",
    "ajax",
    "benfica",
    "porto",
    "sporting",
)

NBA_TOP_TEAMS = (
    "lakers",
    "celtics",
    "warriors",
    "nuggets",
    "thunder",
    "spurs",
    "knicks",
    "heat",
    "bucks",
    "76ers",
    "sixers",
    "suns",
    "mavericks",
    "mavs",
    "clippers",
    "timberwolves",
    "cavaliers",
    "cavs",
    "pacers",
    "magic",
)

NHL_TOP_TEAMS = (
    "oilers",
    "panthers",
    "rangers",
    "bruins",
    "maple leafs",
    "leafs",
    "avalanche",
    "golden knights",
    "lightning",
    "stars",
    "canucks",
    "jets",
)

DERBY_MARKERS = (
    "derby",
    "el clasico",
    "clasico",
    "superclasico",
    "north london",
    "manchester derby",
    "old firm",
    "derbi",
    "rivalry",
)


def detect_editorial_type(e: dict[str, Any]) -> str:
    b = bar_event_blob(e)
    cat = str(e.get("category", "")).upper()
    if "eurovision" in b:
        return "eurovision"
    if re.search(r"\bufc\b|boxing|one championship", b):
        return "ufc"
    if re.search(r"formula\s*1|\bf1\b|grand\s+prix", b):
        return "f1"
    if "nba" in b or cat == "BASKETBALL":
        return "nba"
    if "nhl" in b or "stanley" in b or cat == "HOCKEY":
        return "nhl"
    if _is_esports(b, cat):
        return "esports"
    if _is_football(b, cat):
        return "football"
    if any(x in b for x in ("concert", "wwe", "aew", "grammy", "oscar", "livestream")):
        return "live"
    return "generic"


def _is_football(b: str, cat: str) -> bool:
    if cat == "FOOTBALL":
        return True
    return any(
        x in b
        for x in (
            "premier league",
            "la liga",
            "laliga",
            "serie a",
            "bundesliga",
            "ligue 1",
            "champions league",
            "europa league",
            "uefa",
            "uecl",
        )
    )


def _is_esports(b: str, cat: str) -> bool:
    if cat in ("ESPORTS", "GAMING"):
        return True
    return any(
        x in b
        for x in (
            "esports",
            "cs2",
            "dota",
            "valorant",
            "lol worlds",
            "msi",
            "iem ",
            "blast",
            "the international",
        )
    )


def _count_tokens(text: str, tokens: tuple[str, ...]) -> int:
    t = text.lower()
    return sum(1 for tok in tokens if tok in t)


_WEAK_FOOTBALL_MARKERS = (
    "u21",
    "u-21",
    "u19",
    "u-19",
    "u23",
    "youth",
    "under-21",
    "under 21",
    "fa cup first round",
    "efl trophy",
    "league one",
    "league two",
    "national league",
    "conference south",
    "conference north",
)


def _football_watchability(b: str, title: str) -> tuple[int, str]:
    if any(m in b for m in _WEAK_FOOTBALL_MARKERS):
        return 0, "weak_league"

    score = 38
    reasons: list[str] = []

    if has_matchup_in_title(title):
        score += 28
        reasons.append("matchup")
    clubs = _count_tokens(b + " " + title.lower(), FOOTBALL_TOP_CLUBS)
    if clubs:
        score += min(clubs * 14, 32)
        reasons.append(f"top_clubs×{clubs}")

    if any(m in b for m in DERBY_MARKERS):
        score += 22
        reasons.append("derby")

    if re.search(r"champions\s+league|\bucl\b", b):
        score += 24
        reasons.append("ucl")
    elif re.search(r"europa\s+league|\buel\b", b):
        score += 18
        reasons.append("uel")
    elif any(x in b for x in ("premier league", "la liga", "serie a", "bundesliga", "ligue 1")):
        score += 14
        reasons.append("top_league")

    if re.search(r"\bfinal\b", b) and clubs == 0 and not has_matchup_in_title(title):
        score -= 18
        reasons.append("anonymous_final")

    if re.search(r"qualifier|group\s+stage", b) and clubs == 0:
        score -= 12
        reasons.append("low_stakes_round")

    return min(100, max(0, score)), "+".join(reasons) or "football"


def _nba_watchability(b: str, title: str) -> tuple[int, str]:
    """NBA — высокий приоритет для Gastrobar (выше обычного EPL)."""
    if re.search(r"nba\s+finals", b) or (
        re.search(r"\bfinals\b", b) and "conference" not in b and "nba" in b
    ):
        return 96, "nba_finals"

    if re.search(
        r"western\s+conference\s+final|eastern\s+conference\s+final|"
        r"conference\s+finals|nba\s+conference\s+final",
        b,
    ):
        return 92, "nba_conf_final"

    if re.search(r"conference\s+final", b) and "nba" in b:
        return 90, "nba_conf_final"

    score = 42
    reasons: list[str] = []
    teams = _count_tokens(b + " " + title.lower(), NBA_TOP_TEAMS)

    if has_matchup_in_title(title):
        score += 24
        reasons.append("matchup")
    if teams >= 2:
        score += 26
        reasons.append("top_vs_top")
    elif teams == 1:
        score += 12
        reasons.append("top_team")

    if "playoff" in b:
        score += 28
        reasons.append("playoffs")
        if re.search(r"game\s*[1-7]|game\s*\d", b):
            score += 10
            reasons.append("playoff_game")

    if teams == 0 and "playoff" not in b:
        score -= 28
        reasons.append("weak_regular")

    return min(100, max(0, score)), "+".join(reasons) or "nba"


def _nhl_watchability(b: str, title: str) -> tuple[int, str]:
    score = 30
    reasons: list[str] = []
    teams = _count_tokens(b + " " + title.lower(), NHL_TOP_TEAMS)

    if has_matchup_in_title(title):
        score += 22
        reasons.append("matchup")
    if teams >= 2:
        score += 26
        reasons.append("top_vs_top")
    elif teams == 1:
        score += 12
        reasons.append("top_team")

    if "stanley cup" in b and "final" in b:
        score += 28
        reasons.append("cup_final")
    elif re.search(r"conference\s+final", b):
        score += 22
        reasons.append("conf_final")
    elif "playoff" in b:
        score += 16
        reasons.append("playoffs")
    elif teams == 0:
        score -= 20
        reasons.append("weak_match")

    return min(100, max(0, score)), "+".join(reasons) or "nhl"


def _f1_watchability(b: str) -> tuple[int, str]:
    if re.search(r"\bpractice\b|\bfp[123]\b|free\s+practice", b):
        return 0, "practice"
    score = 72
    reasons = ["f1_weekend"]
    if re.search(r"\brace\b|grand\s+prix", b):
        score += 12
        reasons.append("race")
    elif re.search(r"sprint", b):
        score += 8
        reasons.append("sprint")
    elif re.search(r"qualifying", b):
        score += 6
        reasons.append("qualifying")
    return min(100, score), "+".join(reasons)


def _ufc_watchability(b: str, title: str) -> tuple[int, str]:
    if re.search(r"prelim|early\s+prelim", b) and "main" not in b:
        return 18, "prelims_only"
    score = 50
    reasons: list[str] = []
    if has_matchup_in_title(title):
        score += 35
        reasons.append("bout")
    if re.search(r"title\s+fight|championship", b):
        score += 32
        reasons.append("title")
    if "main card" in b or "main event" in b:
        score += 30
        reasons.append("main_card")
    if re.search(r"ufc\s+fight\s+night", b) and has_matchup_in_title(title):
        score += 12
        reasons.append("fight_night")
    if not has_matchup_in_title(title) and "main" not in b:
        return 15, "no_main_bout"
    return min(100, max(0, score)), "+".join(reasons) or "ufc"


def _eurovision_watchability(b: str) -> tuple[int, str]:
    if re.search(r"grand\s+final", b):
        return 92, "grand_final"
    if re.search(r"semi", b):
        return 78, "semi"
    return 45, "eurovision_other"


def _esports_watchability(b: str, title: str) -> tuple[int, str]:
    if re.search(r"final\s+day\s+events?|qualifier\s+round|group\s+stage", b):
        return 12, "vague_round"
    score = 40
    reasons: list[str] = []
    if re.search(r"grand\s+final|major|champions|worlds|international|iem|blast", b):
        score += 35
        reasons.append("major_stage")
    if has_matchup_in_title(title) or "—" in title:
        score += 15
        reasons.append("matchup")
    return min(100, max(0, score)), "+".join(reasons) or "esports"


def _live_watchability(b: str) -> tuple[int, str]:
    score = 50
    if any(x in b for x in ("wrestlemania", "royal rumble", "summerslam", "ppv")):
        score += 30
    if any(x in b for x in ("grammy", "oscar", "coachella", "taylor swift", "beyonc")):
        score += 25
    return min(100, score), "live_show"


def compute_watchability_score(e: dict[str, Any]) -> tuple[int, str, str]:
    """
    (score 0–100, editorial_type, reason_tags).
    0 = не показывать.
    """
    if gastrobar_hard_reject(e) or is_f1_excluded_event(e):
        return 0, "reject", "hard_reject"

    b = bar_event_blob(e)
    title = str(e.get("title", "")).strip()
    etype = detect_editorial_type(e)

    if etype == "football":
        s, r = _football_watchability(b, title)
    elif etype == "nba":
        s, r = _nba_watchability(b, title)
    elif etype == "nhl":
        s, r = _nhl_watchability(b, title)
    elif etype == "f1":
        s, r = _f1_watchability(b)
    elif etype == "ufc":
        s, r = _ufc_watchability(b, title)
    elif etype == "eurovision":
        s, r = _eurovision_watchability(b)
    elif etype == "esports":
        s, r = _esports_watchability(b, title)
    elif etype == "live":
        s, r = _live_watchability(b)
    else:
        s = 42
        if has_matchup_in_title(title):
            s += 20
        r = "generic"

    conf = str(e.get("confidence", "medium")).lower()
    if conf == "high":
        s = min(100, s + 4)
    elif conf == "low":
        s = max(0, s - 15)

    return min(100, max(0, s)), etype, r


def enrich_watchability(e: dict[str, Any]) -> dict[str, Any]:
    out = dict(e)
    score, etype, reason = compute_watchability_score(out)
    out["watchability_score"] = score
    out["editorial_type"] = etype
    out["watchability_reason"] = reason
    log.info(
        "watchability: title=%r score=%s type=%s reason=%s",
        out.get("title"),
        score,
        etype,
        reason,
    )
    return out
