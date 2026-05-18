"""
Приоритет аудитории Gastrobar + временные слоты (90 мин, 2 ТВ).

Меньше gastrobar_audience_priority = важнее.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from config import GASTROBAR_TV_COUNT, TIMEZONE
from daily_event import event_start_datetime_vn
from event_participants import has_matchup_in_title
from event_verifier import bar_event_blob
from watchability import detect_editorial_type
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

TZ = ZoneInfo(TIMEZONE)
CONFLICT_WINDOW_MINUTES = 90


def gastrobar_audience_priority(e: dict[str, Any]) -> int:
    """
    Порядок для Gastrobar:
    1. UCL / Europa / топ-футбол
    2. UFC Main Card
    3. F1 Race / Qualifying / Sprint
    4. NHL Playoffs / Stanley Cup
    5. NBA Playoffs / Conf Finals / Finals
    6. Esports / Eurovision / live
    """
    b = bar_event_blob(e)
    title = str(e.get("title", "")).strip()
    etype = detect_editorial_type(e)

    if etype == "football":
        if _re(r"champions\s+league|\bucl\b", b) and _re(r"\bfinal\b", b):
            return 8
        if _re(r"champions\s+league|\bucl\b", b):
            return 10
        if _re(r"europa\s+league|\buel\b", b) and _re(r"\bfinal\b", b):
            return 12
        if _re(r"europa\s+league|\buel\b", b):
            return 14
        if any(m in b for m in ("derby", "el clasico", "clasico", "superclasico")):
            return 15
        if _re(r"premier league|la liga|serie a|bundesliga|ligue 1", b):
            if has_matchup_in_title(title):
                return 18
            return 35
        if has_matchup_in_title(title):
            return 22
        return 40

    if etype == "ufc":
        if _re(r"main card|main event|title fight", b) and has_matchup_in_title(title):
            return 20
        if has_matchup_in_title(title):
            return 28
        return 75

    if etype == "f1":
        if _re(r"\brace\b|grand\s+prix", b):
            return 24
        if _re(r"sprint", b):
            return 26
        if _re(r"qualifying", b):
            return 28
        return 50

    if etype == "nhl":
        if _re(r"stanley cup", b) and _re(r"\bfinal\b", b):
            return 30
        if _re(r"conference\s+final", b):
            return 32
        if _re(r"playoff", b):
            return 34
        return 70

    if etype == "nba":
        if _re(r"nba\s+finals", b) or (
            _re(r"\bfinals\b", b) and "conference" not in b
        ):
            return 38
        if _re(r"western\s+conference\s+final|eastern\s+conference\s+final", b):
            return 40
        if _re(r"conference\s+final", b):
            return 42
        if _re(r"playoff", b):
            return 45
        return 85

    if etype == "eurovision":
        if _re(r"grand\s+final", b):
            return 48
        if _re(r"semi", b):
            return 52
        return 60

    if etype == "esports":
        if _re(r"grand\s+final|major|worlds|international", b):
            return 50
        return 65

    if etype == "live":
        return 55

    return 90


def _re(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, re.I))


def enrich_gastrobar_priority(e: dict[str, Any]) -> dict[str, Any]:
    out = dict(e)
    p = gastrobar_audience_priority(out)
    out["gastrobar_priority"] = p
    return out


def _time_slot_label(start: datetime) -> str:
    return start.strftime("%Y-%m-%d %H:%M")


def events_in_conflict_window(
    a: dict[str, Any],
    b: dict[str, Any],
    *,
    window_minutes: int = CONFLICT_WINDOW_MINUTES,
) -> bool:
    sa = event_start_datetime_vn(a)
    sb = event_start_datetime_vn(b)
    if not sa or not sb:
        return False
    return abs(sa - sb) <= timedelta(minutes=window_minutes)


def cluster_events_by_conflict_window(
    events: list[dict[str, Any]],
    *,
    window_minutes: int = CONFLICT_WINDOW_MINUTES,
) -> list[list[dict[str, Any]]]:
    if not events:
        return []

    with_start = [
        e for e in events if event_start_datetime_vn(e) is not None
    ]
    without = [e for e in events if event_start_datetime_vn(e) is None]

    with_start.sort(
        key=lambda e: event_start_datetime_vn(e) or datetime.max.replace(tzinfo=TZ)
    )

    clusters: list[list[dict[str, Any]]] = []
    for e in with_start:
        placed = False
        for cluster in clusters:
            if any(
                events_in_conflict_window(e, x, window_minutes=window_minutes)
                for x in cluster
            ):
                cluster.append(e)
                placed = True
                break
        if not placed:
            clusters.append([e])

    for e in without:
        clusters.append([e])

    clusters.sort(
        key=lambda c: min(
            event_start_datetime_vn(x) or datetime.max.replace(tzinfo=TZ) for x in c
        )
    )
    return clusters


def apply_audience_slot_selection(
    events: list[dict[str, Any]],
    *,
    tv_count: int | None = None,
    log_prefix: str = "slot",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    В каждом окне 90 мин — максимум tv_count событий по gastrobar_priority.
    События в разных окнах не конкурируют (NBA утром ≠ футбол вечером).
    """
    if not events:
        return [], []

    limit = max(1, int(tv_count if tv_count is not None else GASTROBAR_TV_COUNT))
    enriched = [enrich_gastrobar_priority(dict(e)) for e in events]

    for e in enriched:
        start = event_start_datetime_vn(e)
        log.info(
            "%s event score: title=%r watchability=%s gastrobar_priority=%s start=%s",
            log_prefix,
            e.get("title"),
            e.get("watchability_score"),
            e.get("gastrobar_priority"),
            _time_slot_label(start) if start else "?",
        )

    clusters = cluster_events_by_conflict_window(enriched)
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for idx, cluster in enumerate(clusters):
        start = event_start_datetime_vn(cluster[0])
        slot = _time_slot_label(start) if start else f"unknown_{idx}"
        titles = [str(x.get("title", ""))[:50] for x in cluster]
        log.info(
            "%s conflict group #%s slot=%s size=%s events=%s",
            log_prefix,
            idx,
            slot,
            len(cluster),
            titles,
        )

        if len(cluster) <= limit:
            for e in cluster:
                selected.append(e)
                log.info(
                    "%s kept: title=%r reason=no_slot_conflict slot=%s",
                    log_prefix,
                    e.get("title"),
                    slot,
                )
            continue

        ranked = sorted(
            cluster,
            key=lambda x: (
                int(x.get("gastrobar_priority", 99)),
                -int(x.get("watchability_score", 0)),
                event_start_datetime_vn(x) or datetime.max.replace(tzinfo=TZ),
            ),
        )
        keep = ranked[:limit]
        drop = ranked[limit:]

        for e in keep:
            selected.append(e)
            log.info(
                "%s kept: title=%r reason=higher_priority_in_slot slot=%s "
                "gastrobar_priority=%s",
                log_prefix,
                e.get("title"),
                slot,
                e.get("gastrobar_priority"),
            )
        for e in drop:
            skipped.append(e)
            log.info(
                "%s skipped: title=%r reason=slot_conflict_lower_priority slot=%s "
                "gastrobar_priority=%s beat_by=%s",
                log_prefix,
                e.get("title"),
                slot,
                e.get("gastrobar_priority"),
                [k.get("title") for k in keep],
            )

    selected.sort(
        key=lambda x: (
            event_start_datetime_vn(x) or datetime.max.replace(tzinfo=TZ),
            int(x.get("gastrobar_priority", 99)),
        )
    )
    return selected, skipped
