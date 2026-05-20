"""
Ограничение daily / weekly по числу телевизоров — слоты 90 мин, приоритет Gastrobar.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from config import GASTROBAR_TV_COUNT, TIMEZONE
from daily_display import format_event_schedule_line
from daily_event import event_start_datetime_vn
from gastrobar_priority import apply_audience_slot_selection
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

TZ = ZoneInfo(TIMEZONE)

# Совместимость
CLUSTER_START_GAP_HOURS = 1.5
CONFLICT_WINDOW_MINUTES = 90


def event_end_datetime_vn(e: dict[str, Any]):
    from datetime import timedelta

    from bar_hours import infer_duration_minutes

    start = event_start_datetime_vn(e)
    if not start:
        return None
    return start + timedelta(minutes=infer_duration_minutes(e))


def events_share_time_window(a: dict[str, Any], b: dict[str, Any], **kwargs) -> bool:
    from gastrobar_priority import events_in_conflict_window

    return events_in_conflict_window(a, b)


def cluster_events_by_time_window(events: list[dict[str, Any]]):
    from gastrobar_priority import cluster_events_by_conflict_window

    return cluster_events_by_conflict_window(events)


def apply_tv_limit_for_digest(
    events: list[dict[str, Any]],
    *,
    tv_count: int | None = None,
    log_prefix: str = "daily_tv",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return apply_audience_slot_selection(
        events,
        tv_count=tv_count,
        log_prefix=log_prefix,
    )


def events_are_simultaneous(a: dict[str, Any], b: dict[str, Any]) -> bool:
    sa, ea = event_start_datetime_vn(a), event_end_datetime_vn(a)
    sb, eb = event_start_datetime_vn(b), event_end_datetime_vn(b)
    if not sa or not sb or not ea or not eb:
        return False
    return sa < eb and sb < ea


def format_dual_screen_daily_post(events: list[dict[str, Any]]) -> str:
    if len(events) != 2:
        raise ValueError("format_dual_screen_daily_post expects exactly 2 events")

    ordered = sorted(
        events,
        key=lambda e: event_start_datetime_vn(e) or datetime.max.replace(tzinfo=TZ),
    )
    e1, e2 = ordered
    em1 = str(e1.get("emoji", "🏟")).strip() or "🏟"
    em2 = str(e2.get("emoji", "🏟")).strip() or "🏟"
    sched1 = format_event_schedule_line(e1)
    sched2 = format_event_schedule_line(e2)
    title1 = str(e1.get("title", "")).strip()
    title2 = str(e2.get("title", "")).strip()
    sub1 = str(e1.get("subtitle", e1.get("league", ""))).strip()
    sub2 = str(e2.get("subtitle", e2.get("league", ""))).strip()

    blob = f"{sub1} {sub2} {title1} {title2}".lower()
    both_epl = "premier league" in blob or (
        "premier" in blob and "england" in f"{e1.get('league_country','')} {e2.get('league_country','')}".lower()
    )
    if both_epl:
        intro = "Сегодня ночью у нас Premier League на два экрана ⚽"
        outro = (
            "Большой футбольный вечер в Gastrobar.\n"
            "Пиво холодное, настойки заряжены."
        )
    else:
        intro = "Сегодня ночью у нас двойной экран 😏"
        outro = ""

    lines = [
        intro,
        "",
        "На одном:",
        f"🕒 {sched1} — {title1}",
    ]
    if sub1 and sub1.lower() != title1.lower() and "premier league" not in sub1.lower()[:20]:
        lines.append(sub1)
    lines.extend(["", "На втором:", f"🕒 {sched2} — {title2}"])
    if sub2 and sub2.lower() != title2.lower() and "premier league" not in sub2.lower()[:20]:
        lines.append(sub2)
    lines.append("")
    if outro:
        lines.append(outro)
        lines.append("")
    elif events_are_simultaneous(e1, e2):
        lines.append("У нас 2 телевизора — можно включить оба.")
        lines.append("")
    lines.append("📍Океанус, улица с траками")
    return "\n".join(lines)
