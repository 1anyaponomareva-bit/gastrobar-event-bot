"""
Конвертация событий API-SPORTS → radar event + lock по UTC fixture (без event_radar import).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WD_RU = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")


def _weekday_ru_for_date(d: date) -> str:
    return _WD_RU[d.weekday()]


def _normalize_hhmm(raw: str) -> str | None:
    from event_verifier import _parse_time_flexible

    norm, _ = _parse_time_flexible(str(raw or "").strip())
    return norm


_SPORT_CATEGORY = {
    "football": ("FOOTBALL", "⚽"),
    "hockey": ("HOCKEY", "🏒"),
    "basketball": ("BASKETBALL", "🏀"),
    "formula1": ("SPORTS", "🏎"),
    "f1": ("SPORTS", "🏎"),
    "esports": ("ESPORTS", "🎮"),
    "mma": ("SPORTS", "🥊"),
    "boxing": ("SPORTS", "🥊"),
}


def _resolve_program_sport(
    item: dict[str, Any],
    *,
    title: str = "",
    subtitle: str = "",
) -> str:
    sport = str(item.get("sport", "") or "").strip().lower()
    if sport in ("f1",):
        return "formula1"
    if sport and sport not in ("misc", "other", ""):
        return sport
    blob = f"{title} {subtitle}".lower()
    if re.search(r"\b(nhl|khl|stanley|hockey|iihf|world\s+championship)\b", blob, re.I):
        return "hockey"
    if re.search(
        r"\b(formula\s*1|grand\s+prix|qualifying|practice|sprint\s+race|fp[123])\b",
        blob,
        re.I,
    ):
        return "formula1"
    if re.search(r"\b(cs2|dota|esports|valorant|dreamleague|blast|major|esl)\b", blob, re.I):
        return "esports"
    if re.search(r"\bnba\b", blob, re.I):
        return "basketball"
    if re.search(r"\bvs\.?\b", title, re.I):
        return "football"
    return sport or "misc"


def program_item_to_radar_event(item: dict[str, Any]) -> dict[str, Any] | None:
    if item.get("kind") == "block":
        d_obj = date.today()
        date_s = d_obj.isoformat()
        line = str(item.get("line", "")).strip()
        if not line:
            return None
        return {
            "date": date_s,
            "time": "20:00",
            "weekday": _weekday_ru_for_date(d_obj),
            "category": "SPORTS",
            "title": line,
            "subtitle": line,
            "league": line,
            "why": "Подборка API-SPORTS (резерв без Gemini)",
            "emoji": str(item.get("emoji", "🏟")).strip() or "🏟",
            "source_timezone": "UTC",
            "verified_via": "API-SPORTS",
            "confidence": "high",
            "radar_priority": 1,
        }

    date_s = str(item.get("date", "")).strip()
    if not _DATE_RE.match(date_s):
        return None
    try:
        d_obj = date.fromisoformat(date_s)
    except ValueError:
        return None
    time_s = _normalize_hhmm(str(item.get("time", ""))) or "20:00"
    title = str(item.get("title", "")).strip()
    if not title:
        return None
    subtitle = str(item.get("league_label_ru", item.get("league_raw", item.get("league", "")))).strip()
    tier = str(item.get("tier", "high")).lower()
    sport = _resolve_program_sport(item, title=title, subtitle=subtitle)
    cat, default_emoji = _SPORT_CATEGORY.get(sport, ("SPORTS", "🏟"))
    raw_em = item.get("emoji")
    emoji = (
        default_emoji
        if raw_em is None or raw_em == ""
        else (str(raw_em).strip() or default_emoji)
    )
    ev: dict[str, Any] = {
        "date": date_s,
        "time": time_s,
        "sport": sport,
        "weekday": _weekday_ru_for_date(d_obj),
        "category": cat,
        "title": title,
        "subtitle": subtitle,
        "league": subtitle,
        "why": "API-SPORTS weekly pool",
        "emoji": emoji,
        "original_date": date_s,
        "original_time": time_s,
        "verified_via": "API-SPORTS",
        "confidence": "high",
        "radar_priority": 1 if tier == "high" else 2,
    }
    if item.get("league_id") is not None:
        ev["league_id"] = item.get("league_id")
    if item.get("league_country"):
        ev["league_country"] = item.get("league_country")
    return ev


def lock_football_fixture_event(
    item: dict[str, Any],
    *,
    phase: str = "api_sports",
) -> dict[str, Any] | None:
    """Lock по UTC ISO из API-SPORTS (date/time в item — только для отображения)."""
    iso = str(item.get("fixture_utc_iso") or "").strip()
    if not iso:
        return None
    title_s = str(item.get("title", ""))
    league_s = str(item.get("league", ""))
    sport = _resolve_program_sport(item, title=title_s, subtitle=league_s)
    pi: dict[str, Any] = {
            "kind": "match",
            "sport": sport,
            "title": item.get("title", ""),
            "league_label_ru": item.get("league", ""),
            "league_raw": item.get("league", ""),
            "date": item.get("date", ""),
            "time": item.get("time", ""),
            "league_id": item.get("league_id"),
            "league_country": item.get("league_country"),
            "tier": "high",
    }
    if item.get("emoji"):
        pi["emoji"] = item.get("emoji")
    ev = program_item_to_radar_event(pi)
    if not ev:
        return None
    from locked_time import lock_event_from_api_utc_iso

    return lock_event_from_api_utc_iso(ev, iso, phase=phase)


def lock_api_sports_program_item(
    item: dict[str, Any],
    *,
    phase: str = "api_sports",
) -> dict[str, Any] | None:
    """Football — UTC fixture; остальные виды — lock по локальному date/time."""
    if str(item.get("fixture_utc_iso") or "").strip():
        return lock_football_fixture_event(item, phase=phase)
    ev = program_item_to_radar_event(item)
    if not ev:
        return None
    from locked_time import lock_event_schedule

    return lock_event_schedule(ev, phase=phase)
