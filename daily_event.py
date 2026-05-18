"""
Пост дня: выбор главного события ближайших ~24 ч (отдельно от weekly radar).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time as dtime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from bar_hours import filter_events_for_bar_hours
from config import TIMEZONE
from event_radar import (
    _prepare_for_afisha_selection,
    bar_event_blob,
    get_event_radar_week,
)
from event_verifier import _parse_time_flexible

log = logging.getLogger(__name__)

TZ = ZoneInfo(TIMEZONE)
# События 00:00–09:59 — пост можно готовить накануне.
MORNING_CUTOFF_HOUR = 10


def _vn_now() -> datetime:
    return datetime.now(TZ)


def _parse_display_minutes(e: dict[str, Any]) -> int | None:
    raw = str(
        e.get("display_time") or e.get("time_display") or e.get("time", "")
    ).strip()
    if raw == "время уточняется" or not raw:
        return None
    norm, _ = _parse_time_flexible(raw)
    return norm and int(norm.split(":")[0]) * 60 + int(norm.split(":")[1])


def event_start_datetime_vn(e: dict[str, Any]) -> datetime | None:
    date_s = str(e.get("date", "")).strip()
    try:
        d = date.fromisoformat(date_s)
    except ValueError:
        return None
    mins = _parse_display_minutes(e)
    if mins is None:
        return None
    h, mi = divmod(mins, 60)
    return datetime.combine(d, dtime(h, mi), tzinfo=TZ)


def campaign_post_date(e: dict[str, Any]) -> date | None:
    """
    День, когда логично публиковать пост:
    утренние старты (до 10:00) — накануне; иначе в день события.
    """
    start = event_start_datetime_vn(e)
    if not start:
        return None
    if start.hour < MORNING_CUTOFF_HOUR:
        return (start.date() - timedelta(days=1))
    return start.date()


def _daily_priority_score(e: dict[str, Any]) -> int:
    """Меньше = важнее для поста дня (UFC → Eurovision → UCL → …)."""
    b = bar_event_blob(e)
    if re.search(r"\bufc\b", b):
        if "main card" in b or "main event" in b or re.search(
            r"\bvs\.?\b", str(e.get("title", "")), re.I
        ):
            return 1
    if "eurovision" in b:
        return 2
    if "nba" in b and re.search(
        r"finals|conference\s+final|playoff|game\s*[1-7]", b
    ):
        return 3
    if re.search(
        r"\bucl\b|champions\s+league|uefa\s+champions|europa\s+league|\buel\b", b
    ):
        return 4
    if re.search(r"formula\s*1|\bf1\b", b) and re.search(
        r"qualifying|sprint|\brace|grand\s+prix", b
    ):
        return 5
    if "stanley" in b or ("nhl" in b and re.search(r"playoff|final|conference", b)):
        return 6
    if any(
        x in b
        for x in (
            "esports",
            "grand final",
            "the international",
            "valorant champions",
            "lol worlds",
            "cs2 major",
        )
    ):
        return 7
    if "wwe" in b or re.search(r"live\s+show|pay.per.view|ppv", b):
        return 8
    tier = int(e.get("radar_tier", 50))
    if tier < 20:
        return 10 + tier
    return 99


def is_in_daily_window(e: dict[str, Any], now: datetime | None = None) -> bool:
    """Событие в окне ~24 ч или утреннее «завтра» при посте сегодня."""
    now = now or _vn_now()
    start = event_start_datetime_vn(e)
    if not start:
        return False

    if now <= start <= now + timedelta(hours=24):
        return True

    # Утро следующего дня: можно анонсировать сегодня
    if start.hour < MORNING_CUTOFF_HOUR:
        tomorrow = now.date() + timedelta(days=1)
        if start.date() == tomorrow and campaign_post_date(e) == now.date():
            return True
    return False


def enrich_daily_campaign_meta(e: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or _vn_now()
    out = dict(e)
    start = event_start_datetime_vn(e)
    cpd = campaign_post_date(e)
    out["campaign_post_date"] = cpd.isoformat() if cpd else ""
    out["display_time"] = str(
        e.get("display_time") or e.get("time_display") or e.get("time", "")
    )
    if start:
        if start.date() == now.date():
            if start.hour < MORNING_CUTOFF_HOUR:
                out["daily_timing_phrase"] = "уже этой ночью"
            else:
                out["daily_timing_phrase"] = "сегодня"
        elif start.date() == now.date() + timedelta(days=1):
            if start.hour < MORNING_CUTOFF_HOUR:
                out["daily_timing_phrase"] = "завтра рано утром"
            else:
                out["daily_timing_phrase"] = "завтра"
        else:
            wd = str(e.get("weekday", "")).strip()
            out["daily_timing_phrase"] = f"{wd} {out['display_time']}".strip()
    else:
        out["daily_timing_phrase"] = "скоро"
    return out


async def fetch_week_events_for_daily() -> list[dict[str, Any]]:
    events, _, _, _, _ = await get_event_radar_week()
    filtered = filter_events_for_bar_hours(events)
    return [_prepare_for_afisha_selection(dict(e)) for e in filtered]


NOW24_MAX_ITEMS = 4


def select_now24_events(
    events: list[dict[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Сильные события в ближайшие 24 ч; без добивания слабым хвостом."""
    from event_participants import is_gastrobar_eligible
    from watchability import enrich_watchability

    now = now or _vn_now()
    pool = events or []
    candidates: list[dict[str, Any]] = []

    for e in pool:
        ev = enrich_watchability(dict(e))
        if int(ev.get("radar_tier", 99)) >= 99 and int(ev.get("watchability_score", 0)) < 52:
            continue
        if not is_gastrobar_eligible(ev):
            continue
        if not is_in_daily_window(ev, now):
            continue
        candidates.append(enrich_daily_campaign_meta(ev, now))

    if not candidates:
        return []

    candidates.sort(
        key=lambda x: (
            -int(x.get("watchability_score", 0)),
            _daily_priority_score(x),
            event_start_datetime_vn(x) or datetime.max.replace(tzinfo=TZ),
        )
    )
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for e in candidates:
        if len(out) >= NOW24_MAX_ITEMS:
            break
        key = (
            str(e.get("date", "")),
            str(e.get("title", "")).lower()[:80],
            str(e.get("display_time") or e.get("time", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(e)

    from daily_tv import apply_tv_limit_for_digest

    limited, _ = apply_tv_limit_for_digest(out)
    return limited


def collect_campaign_events(
    events: list[dict[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """События для ежедневного поста сегодня (окно 24 ч + campaign_post_date)."""
    now = now or _vn_now()
    pool = events or []
    out: list[dict[str, Any]] = []
    from event_participants import is_gastrobar_eligible

    for e in pool:
        if not is_in_daily_window(e, now):
            continue
        cpd = campaign_post_date(e)
        if cpd and cpd != now.date():
            continue
        if int(e.get("radar_tier", 99)) >= 99:
            continue
        if not is_gastrobar_eligible(e):
            continue
        out.append(enrich_daily_campaign_meta(e, now))
    out.sort(
        key=lambda x: (
            _daily_priority_score(x),
            event_start_datetime_vn(x) or datetime.max.replace(tzinfo=TZ),
        )
    )
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for e in out:
        key = (
            str(e.get("date", "")),
            str(e.get("title", "")).lower()[:80],
            str(e.get("display_time") or e.get("time", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)

    from daily_tv import apply_tv_limit_for_digest

    capped = deduped[:NOW24_MAX_ITEMS]
    limited, _ = apply_tv_limit_for_digest(capped)
    return limited


def get_next_featured_event(
    events: list[dict[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """
    Лучшее событие для поста дня: priority + окно 24 ч + bar hours / display_time.
    """
    now = now or _vn_now()
    pool = events or []
    candidates: list[dict[str, Any]] = []
    for e in pool:
        if int(e.get("radar_tier", 99)) >= 99:
            continue
        if not is_in_daily_window(e, now):
            log.info(
                "daily_skip: title=%r reason=outside_24h_window start=%s",
                e.get("title"),
                event_start_datetime_vn(e),
            )
            continue
        cpd = campaign_post_date(e)
        if cpd and cpd > now.date():
            log.info("daily_skip: title=%r reason=campaign_in_future cpd=%s", e.get("title"), cpd)
            continue
        candidates.append(enrich_daily_campaign_meta(e, now))

    if not candidates:
        log.info("daily: no candidates in window")
        return None

    candidates.sort(
        key=lambda x: (
            _daily_priority_score(x),
            event_start_datetime_vn(x) or datetime.max.replace(tzinfo=TZ),
        )
    )
    best = candidates[0]
    log.info(
        "daily featured: title=%r score=%s start=%s campaign=%s display_time=%s",
        best.get("title"),
        _daily_priority_score(best),
        event_start_datetime_vn(best),
        best.get("campaign_post_date"),
        best.get("display_time"),
    )
    return best


def select_nearest_upcoming(
    events: list[dict[str, Any]] | None = None,
    *,
    now: datetime | None = None,
    within_days: int = 7,
) -> list[dict[str, Any]]:
    """Ближайшее событие из кэша (если в next24 пусто)."""
    from event_participants import is_gastrobar_eligible
    from watchability import enrich_watchability

    now = now or _vn_now()
    horizon = now + timedelta(days=within_days)
    pool = events or []
    future: list[dict[str, Any]] = []

    for e in pool:
        ev = enrich_watchability(dict(e))
        if not is_gastrobar_eligible(ev):
            continue
        start = event_start_datetime_vn(ev)
        if not start or start <= now or start > horizon:
            continue
        future.append(enrich_daily_campaign_meta(ev, now))

    if not future:
        log.info("daily: no upcoming events within %s days", within_days)
        return []

    future.sort(
        key=lambda x: (
            event_start_datetime_vn(x) or datetime.max.replace(tzinfo=TZ),
            -int(x.get("watchability_score", 0)),
        )
    )
    best = future[0]
    log.info(
        "daily nearest upcoming: title=%r start=%s watchability=%s",
        best.get("title"),
        event_start_datetime_vn(best),
        best.get("watchability_score"),
    )
    return [best]


def format_upcoming_preview_message(events: list[dict[str, Any]]) -> str:
    """Сообщение, когда в 24ч пусто, но есть ближайший эфир."""
    if not events:
        return "Крупных событий для Gastrobar в ближайшие 24 часа не найдено."

    e = events[0]
    sched = str(e.get("weekday", "")).strip()
    tm = str(e.get("display_time") or e.get("time", "")).strip()
    title = str(e.get("title", "")).strip()
    start = event_start_datetime_vn(e)
    when = ""
    if start:
        delta = start - _vn_now()
        hours = int(delta.total_seconds() // 3600)
        if hours < 48:
            when = f"через ~{hours} ч"
        else:
            when = f"{sched} {tm}".strip()

    return (
        "В ближайшие 24 часа крупных эфиров нет — но вот что скоро в афише:\n\n"
        f"📅 {when}\n"
        f"⭐ {title}\n\n"
        "Можно подготовить пост заранее: нажмите /daily ещё раз ближе к эфиру "
        "или «Пост дня» после обновления афиши."
    )
