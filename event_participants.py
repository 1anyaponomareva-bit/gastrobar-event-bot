"""
Строгие правила участников и форматирования для афиши Gastrobar.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from event_verifier import bar_event_blob

log = logging.getLogger(__name__)

_MATCHUP_RE = re.compile(
    r"\bvs\.?\b|\s—\s|\s–\s|\s-\s",
    re.I,
)

_VAGUE_PARTICIPANT_RE = [
    re.compile(p, re.I)
    for p in (
        r"between\s+top",
        r"top\s+european",
        r"top\s+teams",
        r"leading\s+clubs",
        r"women'?s\s+clubs",
        r"final\s+day\s+events?",
        r"^game\s+event$",
        r"^playoff\s+game$",
        r"^final\s+match$",
        r"^championship\s+game$",
        r"^conference\s+final$",
        r"two\s+teams",
        r"best\s+teams",
    )
]

_VAGUE_TITLE_RE = [
    re.compile(p, re.I)
    for p in (
        r"^final\s+day\s+events?$",
        r"^game\s+event$",
        r"^playoff\s+game$",
    )
]


def is_vague_participant_text(text: str) -> bool:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t or len(t) < 4:
        return False
    for rx in _VAGUE_PARTICIPANT_RE:
        if rx.search(t):
            return True
    return False


def has_matchup_in_title(title: str) -> bool:
    t = (title or "").strip()
    if len(t) < 6:
        return False
    if not _MATCHUP_RE.search(t):
        return False
    parts = re.split(_MATCHUP_RE, t, maxsplit=1)
    if len(parts) < 2:
        return False
    left, right = parts[0].strip(), parts[1].strip()
    return len(left) >= 2 and len(right) >= 2


def extract_participants(e: dict[str, Any]) -> str:
    raw = str(e.get("participants", "")).strip()
    if raw and not is_vague_participant_text(raw):
        return raw
    title = str(e.get("title", "")).strip()
    if has_matchup_in_title(title):
        return title
    return raw


def _is_sport_match_event(b: str, cat: str) -> bool:
    if cat in ("FOOTBALL", "BASKETBALL", "HOCKEY", "NHL", "NBA", "SPORTS"):
        return True
    return any(
        x in b
        for x in (
            "champions league",
            "europa league",
            "premier league",
            "nba",
            "nhl",
            "stanley cup",
            "conference final",
            " vs ",
            "—",
        )
    ) and "ufc" not in b and "formula 1" not in b and "f1" not in b


def _is_ufc_or_boxing(b: str) -> bool:
    return bool(re.search(r"\bufc\b|boxing|one championship", b))


def _is_f1(b: str) -> bool:
    return bool(re.search(r"formula\s*1|\bf1\b|grand\s+prix", b))


def _is_eurovision(b: str) -> bool:
    return "eurovision" in b


def _is_esports(b: str, cat: str) -> bool:
    if cat in ("ESPORTS", "GAMING"):
        return True
    return any(
        x in b
        for x in (
            "esports",
            "iem ",
            "cs2",
            "dota",
            "valorant champions",
            "lol worlds",
            "the international",
            "blast premier",
        )
    )


def _is_concert_or_show(b: str) -> bool:
    return any(
        x in b
        for x in ("concert", "coachella", "live show", "wwe", "aew", "grammy", "oscar")
    )


def passes_participant_rules(e: dict[str, Any]) -> tuple[bool, str]:
    """
    True — событие можно показывать в афише.
    False — отбраковать (спорт без участников) или только subtitle поправить снаружи.
    """
    title = str(e.get("title", "")).strip()
    subtitle = str(e.get("subtitle", e.get("league", ""))).strip()
    b = bar_event_blob(e)
    cat = str(e.get("category", "")).strip().upper()

    if not title:
        return False, "missing_title"

    for rx in _VAGUE_TITLE_RE:
        if rx.search(title):
            return False, "vague_title"

    if is_vague_participant_text(title):
        return False, "vague_title"

    if subtitle and is_vague_participant_text(subtitle):
        if _is_concert_or_show(b) and not _is_sport_match_event(b, cat):
            return True, "vague_subtitle_ok_for_show"
        return False, "vague_subtitle"

    if subtitle and title.lower() == subtitle.lower():
        if _is_concert_or_show(b) or _is_eurovision(b):
            return True, "title_equals_subtitle_show"
        return False, "title_equals_subtitle"

    if _is_f1(b):
        if re.search(r"practice|free\s+practice|fp1|fp2|fp3", b):
            return False, "f1_practice"
        if re.search(r"qualifying|sprint|\brace|grand\s+prix", b):
            return True, "f1_session_ok"
        return False, "f1_unknown_session"

    if _is_eurovision(b):
        if re.search(r"semi|grand\s+final|\bfinal\b", b):
            return True, "eurovision_stage_ok"
        return False, "eurovision_not_final"

    if _is_esports(b, cat):
        if re.search(r"final\s+day\s+events?", b):
            return False, "esports_vague_final_day"
        if re.search(r"grand\s+final|—\s|\bvs\.?\b|major|champions|iem|blast", b):
            return True, "esports_ok"
        return False, "esports_vague"

    if _is_ufc_or_boxing(b):
        if has_matchup_in_title(title):
            return True, "ufc_matchup_title"
        if re.search(r"ufc\s+fight\s+night|main\s+card|main\s+event", b) and re.search(
            r"\bvs\.?\b", title, re.I
        ):
            return True, "ufc_named_card"
        if "main card" in b or "main event" in b:
            if re.search(r"\bvs\.?\b", title, re.I):
                return True, "ufc_main_with_bout"
        return False, "ufc_missing_fighters"

    if _is_sport_match_event(b, cat):
        if has_matchup_in_title(title):
            return True, "sport_matchup"
        return False, "sport_missing_teams"

    if _is_concert_or_show(b):
        return True, "show_ok"

    if has_matchup_in_title(title):
        return True, "generic_matchup"

    return True, "non_sport_pass"


def sanitize_event_for_display(e: dict[str, Any]) -> dict[str, Any]:
    """Убрать мусорный subtitle; для шоу без лиги — оставить короткий league."""
    out = dict(e)
    subtitle = str(out.get("subtitle", out.get("league", ""))).strip()
    ok, reason = passes_participant_rules(out)
    if not ok:
        return out
    if reason == "vague_subtitle_ok_for_show" or (
        subtitle and is_vague_participant_text(subtitle)
    ):
        out["subtitle"] = ""
        out["league"] = str(out.get("league", "")).strip()
        if is_vague_participant_text(out["league"]):
            out["league"] = ""
    participants = extract_participants(out)
    if participants:
        out["participants"] = participants
    return out


def filter_events_by_participants(
    events: list[dict[str, Any]],
    *,
    log_prefix: str = "afisha",
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in events:
        ok, reason = passes_participant_rules(e)
        if not ok:
            log.info(
                "%s skipped event: title=%r reason=%s",
                log_prefix,
                e.get("title"),
                reason,
            )
            continue
        out.append(sanitize_event_for_display(e))
    return out


def is_gastrobar_eligible(e: dict[str, Any]) -> bool:
    from event_verifier import gastrobar_hard_reject

    if gastrobar_hard_reject(e):
        return False
    if int(e.get("radar_tier", 99)) >= 99:
        return False
    ok, _ = passes_participant_rules(e)
    return ok
