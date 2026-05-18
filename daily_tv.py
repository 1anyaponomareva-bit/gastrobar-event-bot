"""
Ограничение daily / now24 по числу телевизоров в баре.
События в одном временном окне (пересечение или старт в пределах 2 ч) — максимум GASTROBAR_TV_COUNT.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from bar_hours import infer_duration_minutes
from config import GASTROBAR_TV_COUNT, TIMEZONE
from daily_display import format_event_schedule_line
from daily_event import _daily_priority_score, event_start_datetime_vn
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

TZ = ZoneInfo(TIMEZONE)
CLUSTER_START_GAP_HOURS = 2


def event_end_datetime_vn(e: dict[str, Any]) -> datetime | None:
    start = event_start_datetime_vn(e)
    if not start:
        return None
    return start + timedelta(minutes=infer_duration_minutes(e))


def events_share_time_window(
    a: dict[str, Any],
    b: dict[str, Any],
    *,
    gap_hours: float = CLUSTER_START_GAP_HOURS,
) -> bool:
    """Пересечение по времени или старт в пределах gap_hours друг от друга."""
    sa, ea = event_start_datetime_vn(a), event_end_datetime_vn(a)
    sb, eb = event_start_datetime_vn(b), event_end_datetime_vn(b)
    if not sa or not sb:
        return False

    gap = timedelta(hours=gap_hours)
    if abs(sa - sb) <= gap:
        return True

    if ea and eb and sa < eb and sb < ea:
        return True

    # Старт одного внутри «хвоста» другого (длительность + gap)
    if ea and sb <= ea + gap:
        return True
    if eb and sa <= eb + gap:
        return True
    return False


def cluster_events_by_time_window(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    if not events:
        return []
    if len(events) == 1:
        return [list(events)]

    indexed = list(enumerate(events))
    parent = list(range(len(events)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(len(events)):
        for j in range(i + 1, len(events)):
            if events_share_time_window(events[i], events[j]):
                union(i, j)

    buckets: dict[int, list[dict[str, Any]]] = {}
    for i, e in indexed:
        root = find(i)
        buckets.setdefault(root, []).append(e)

    clusters = list(buckets.values())
    clusters.sort(
        key=lambda cluster: min(
            event_start_datetime_vn(e) or datetime.max.replace(tzinfo=TZ)
            for e in cluster
        )
    )
    return clusters


def apply_tv_limit_for_digest(
    events: list[dict[str, Any]],
    *,
    tv_count: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Для daily / now24: в каждом временном кластере оставить не больше tv_count событий
    (лучшие по _daily_priority_score). Остальные — skipped_due_to_tv_limit.
    """
    if not events:
        return [], []

    limit = max(1, int(tv_count if tv_count is not None else GASTROBAR_TV_COUNT))
    if len(events) <= limit:
        return list(events), []

    clusters = cluster_events_by_time_window(events)
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for cluster in clusters:
        if len(cluster) <= limit:
            selected.extend(cluster)
            continue

        ranked = sorted(
            cluster,
            key=lambda e: (
                _daily_priority_score(e),
                event_start_datetime_vn(e) or datetime.max.replace(tzinfo=TZ),
            ),
        )
        keep = ranked[:limit]
        drop = ranked[limit:]
        selected.extend(keep)
        skipped.extend(drop)
        for e in drop:
            log.info(
                "skipped_due_to_tv_limit: title=%r cluster_size=%s tv_count=%s",
                e.get("title"),
                len(cluster),
                limit,
            )

    selected.sort(
        key=lambda e: (
            _daily_priority_score(e),
            event_start_datetime_vn(e) or datetime.max.replace(tzinfo=TZ),
        )
    )
    return selected, skipped


def events_are_simultaneous(a: dict[str, Any], b: dict[str, Any]) -> bool:
    sa, ea = event_start_datetime_vn(a), event_end_datetime_vn(a)
    sb, eb = event_start_datetime_vn(b), event_end_datetime_vn(b)
    if not sa or not sb or not ea or not eb:
        return False
    return sa < eb and sb < ea


def format_dual_screen_daily_post(events: list[dict[str, Any]]) -> str:
    """Шаблон поста дня на два экрана (2 ТВ)."""
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

    lines = [
        "Сегодня ночью у нас двойной экран 😏",
        "",
        "На одном:",
        f"{em1} 🕒 {sched1}",
        title1,
    ]
    if sub1 and sub1.lower() != title1.lower():
        lines.append(sub1)
    lines.extend(
        [
            "",
            "На втором:",
            f"{em2} 🕒 {sched2}",
            title2,
        ]
    )
    if sub2 and sub2.lower() != title2.lower():
        lines.append(sub2)
    lines.append("")
    if events_are_simultaneous(e1, e2):
        lines.append("У нас 2 телевизора — можно включить оба.")
        lines.append("")
    lines.append("📍Океанус, улица с траками")
    return "\n".join(lines)
