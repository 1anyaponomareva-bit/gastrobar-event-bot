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
    "esports": ("ESPORTS", "🎮"),
    "mma": ("SPORTS", "🥊"),
    "boxing": ("SPORTS", "🥊"),
}


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
    sport = str(item.get("sport", "football")).lower()
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
    sport = str(item.get("sport", "football")).lower()
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
