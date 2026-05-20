"""
Группировка параллельных футбольных туров (EPL Final Day и т.п.) для weekly афиши.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from config import GASTROBAR_TV_COUNT
from event_verifier import bar_event_blob
from watchability import detect_editorial_type

log = logging.getLogger(__name__)

# Группировка параллельного тура (final matchday): от 3 матчей в одном слоте.
PARALLEL_KICKOFF_TOLERANCE_MIN = 20
PARALLEL_BLOCK_MIN_MATCHES = 3
PARALLEL_BLOCK_LISTED_MATCHES = 6

_LEAGUE_LABELS: list[tuple[str, str]] = (
    ("premier league", "Premier League"),
    ("la liga", "La Liga"),
    ("laliga", "La Liga"),
    ("serie a", "Serie A"),
    ("bundesliga", "Bundesliga"),
    ("ligue 1", "Ligue 1"),
    ("champions league", "UEFA Champions League"),
    ("europa league", "UEFA Europa League"),
)


def _parse_time_minutes(e: dict[str, Any]) -> int | None:
    raw = str(
        e.get("display_time") or e.get("time_display") or e.get("time", "")
    ).strip().removeprefix("≈")
    m = re.match(r"^(\d{1,2}):(\d{2})$", raw)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _football_league_label(e: dict[str, Any]) -> str | None:
    b = bar_event_blob(e)
    for key, label in _LEAGUE_LABELS:
        if key in b:
            return label
    if detect_editorial_type(e) == "football":
        sub = str(e.get("subtitle", e.get("league", ""))).strip()
        if sub and len(sub) < 48:
            return sub
    return None


def _block_group_key(e: dict[str, Any]) -> tuple[str, int, str] | None:
    if detect_editorial_type(e) != "football":
        return None
    date_s = str(e.get("date", "")).strip()
    mins = _parse_time_minutes(e)
    league = _football_league_label(e)
    if not date_s or mins is None or not league:
        return None
    slot = mins // PARALLEL_KICKOFF_TOLERANCE_MIN
    return (date_s, slot, league.lower())


def _find_parallel_blocks(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    buckets: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
    for e in events:
        key = _block_group_key(e)
        if not key:
            continue
        buckets.setdefault(key, []).append(e)

    blocks: list[list[dict[str, Any]]] = []
    for key, group in buckets.items():
        if len(group) >= PARALLEL_BLOCK_MIN_MATCHES:
            blocks.append(group)
            log.info(
                "parallel football block: league=%s date=%s matches=%s",
                key[2],
                key[0],
                len(group),
            )
    return blocks


def _block_headline(league: str, group: list[dict[str, Any]]) -> str:
    b_all = " ".join(bar_event_blob(x) for x in group)
    if re.search(r"final\s+day|matchday|match\s+day", b_all):
        return f"{league} — финальный тур / Matchday"
    if re.search(r"\bfinal\b", b_all):
        return f"{league} — ключевые матчи"
    return f"{league} — параллельный тур"


def _is_standalone_football_match(e: dict[str, Any]) -> bool:
    """Топ-матч / дерби — отдельной строкой, не внутри блока matchday."""
    from watchability import is_major_weekly_event

    if is_major_weekly_event(e):
        return True
    if int(e.get("watchability_score", 0)) >= 52:
        return True
    return False


def _make_parallel_block_event(
    group: list[dict[str, Any]],
    *,
    tv_count: int | None = None,
) -> dict[str, Any]:
    ranked = sorted(
        group,
        key=lambda x: -int(x.get("watchability_score", 0)),
    )
    listed = ranked[:PARALLEL_BLOCK_LISTED_MATCHES]
    anchor = ranked[0]
    tvs = max(1, int(tv_count if tv_count is not None else GASTROBAR_TV_COUNT))
    league = _football_league_label(anchor) or "Топ-футбол"
    wd = str(anchor.get("weekday", "")).strip()
    tm = str(
        anchor.get("display_time")
        or anchor.get("time_display")
        or anchor.get("time", "")
    ).strip()

    return {
        "afisha_kind": "parallel_block",
        "emoji": "⚽",
        "weekday": wd,
        "display_time": tm,
        "time": anchor.get("time", ""),
        "date": anchor.get("date", ""),
        "category": "FOOTBALL",
        "title": _block_headline(league, group),
        "subtitle": league,
        "league": league,
        "block_headline": _block_headline(league, group),
        "block_matches": [str(p.get("title", "")).strip() for p in listed if p.get("title")],
        "block_match_count": len(group),
        "block_note": f"параллельные матчи тура · на {tvs} экрана в Gastrobar",
        "watchability_score": max(int(p.get("watchability_score", 0)) for p in ranked),
        "editorial_type": "football",
        "confidence": anchor.get("confidence", "medium"),
        "_block_members": [dict(x) for x in group],
    }


def _event_identity_key(e: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(e.get("date", "")).strip(),
        str(e.get("display_time") or e.get("time", "")).strip(),
        str(e.get("title", "")).strip().lower(),
    )


def apply_grouping_for_weekly_display(
    events: list[dict[str, Any]],
    *,
    collapse_blocks: bool = True,
) -> list[dict[str, Any]]:
    """
    Rich weekly: топ-матчи отдельно; 3+ параллельных матча тура — блок с перечислением.
    """
    if not events:
        return []

    if not collapse_blocks:
        return list(events)

    blocks = _find_parallel_blocks(events)
    standalone_keys: set[tuple[str, str, str]] = set()
    block_by_slot: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
    member_keys: set[tuple[str, str, str]] = set()

    for group in blocks:
        key = _block_group_key(group[0])
        if not key:
            continue
        standalones = [e for e in group if _is_standalone_football_match(e)]
        block_members = [e for e in group if e not in standalones]
        for e in standalones:
            standalone_keys.add(_event_identity_key(e))
        if len(block_members) >= PARALLEL_BLOCK_MIN_MATCHES:
            block_by_slot[key] = block_members
            for e in block_members:
                member_keys.add(_event_identity_key(e))
        else:
            for e in group:
                standalone_keys.add(_event_identity_key(e))

    display: list[dict[str, Any]] = []
    emitted_blocks: set[tuple[str, int, str]] = set()

    for e in events:
        ident = _event_identity_key(e)
        if ident in standalone_keys:
            display.append(e)
            continue
        slot = _block_group_key(e)
        if ident in member_keys and slot and slot in block_by_slot:
            if slot not in emitted_blocks:
                display.append(_make_parallel_block_event(block_by_slot[slot]))
                emitted_blocks.add(slot)
            continue
        display.append(e)

    log.info(
        "weekly display grouping: in=%s out=%s parallel_blocks=%s standalone=%s",
        len(events),
        len(display),
        len(emitted_blocks),
        len(standalone_keys),
    )
    return display


def format_parallel_block_lines(e: dict[str, Any]) -> list[str]:
    em = str(e.get("emoji", "⚽")).strip()
    wd = str(e.get("weekday", "")).strip()
    tm = str(e.get("display_time") or e.get("time", "")).strip()
    headline = str(e.get("block_headline") or e.get("title", "")).strip()
    matches: list[str] = list(e.get("block_matches") or [])
    note = str(e.get("block_note", "")).strip()
    extra = int(e.get("block_match_count", 0)) - len(matches)

    lines = [f"{em} {wd} {tm}", headline, ""]
    for m in matches:
        if m:
            lines.append(m)
    if extra > 0:
        lines.append(f"ещё {extra} матча тура параллельно")
    if note:
        lines.append(f"_{note}_")
    return lines
