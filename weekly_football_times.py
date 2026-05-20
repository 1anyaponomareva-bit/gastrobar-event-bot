"""
Подтягивание точного времени топ-футбола для weekly афиши из API-SPORTS (UTC → VN).
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_TOP_LEAGUE_IDS = frozenset({39, 140, 135, 78, 61, 2, 3, 848})


def _norm_title(s: str) -> str:
    t = re.sub(r"\s+", " ", (s or "").lower().strip())
    t = t.replace(" — ", " vs ").replace(" – ", " vs ").replace("-", " vs ")
    return t


def _teams_from_title(s: str) -> frozenset[str]:
    parts = re.split(r"\s+vs\.?\s+|\s+—\s+|\s+–\s+", s, flags=re.I)
    return frozenset(p.strip().lower() for p in parts if len(p.strip()) > 2)


def _title_match(a: str, b: str) -> bool:
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if na in nb or nb in na:
        return True
    ta, tb = _teams_from_title(a), _teams_from_title(b)
    return bool(ta) and ta == tb


async def enrich_weekly_football_times(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Для событий топ-футбола в weekly: сверка с API-SPORTS fixture UTC (как now24).
    """
    if not events:
        return events

    from config import SPORTS_API_KEY

    if not SPORTS_API_KEY:
        return events

    from sports_events import get_football_events_next_days_vn
    from radar_sports_convert import lock_football_fixture_event
    from locked_time import reapply_local_from_utc

    api_rows = await get_football_events_next_days_vn(days_ahead=8)
    if not api_rows:
        return events

    api_index: list[tuple[dict[str, Any], str]] = []
    for row in api_rows:
        lid = row.get("league_id")
        try:
            lid_i = int(lid) if lid is not None else None
        except (TypeError, ValueError):
            lid_i = None
        if lid_i not in _TOP_LEAGUE_IDS:
            continue
        if not str(row.get("fixture_utc_iso") or "").strip():
            continue
        api_index.append((row, str(row.get("title", ""))))

    if not api_index:
        return events

    out: list[dict[str, Any]] = []
    for ev in events:
        blob = f"{ev.get('title','')} {ev.get('league','')} {ev.get('subtitle','')}".lower()
        if "foot" not in str(ev.get("category", "")).lower() and not re.search(
            r"premier|europa|\bchampions\s+league\b|\bucl\b|liga|serie|bundeslig",
            blob,
            re.I,
        ):
            out.append(ev)
            continue

        matched_row = None
        for row, api_title in api_index:
            if _title_match(str(ev.get("title", "")), api_title):
                matched_row = row
                break

        if matched_row:
            locked = lock_football_fixture_event(matched_row, phase="weekly_api_enrich")
            if locked:
                log.info(
                    "weekly api time: %r -> %s %s (utc=%s)",
                    ev.get("title"),
                    locked.get("local_weekday"),
                    locked.get("local_time"),
                    locked.get("utc_datetime"),
                )
                out.append(locked)
                continue

        if str(ev.get("utc_authority", "")).lower() == "api_sports":
            fixed = reapply_local_from_utc(ev)
            out.append(fixed if fixed else ev)
            continue

        out.append(ev)

    return out
