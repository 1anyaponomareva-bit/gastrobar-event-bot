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

_RPL_LEAGUE_ID = 235  # Russian Premier League (API-SPORTS)


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
    from next24 import resolve_event_local_datetime_vn

    return resolve_event_local_datetime_vn(e)


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
    """Меньше = важнее. Совпадает с gastrobar_audience_priority."""
    from gastrobar_priority import gastrobar_audience_priority

    if e.get("gastrobar_priority") is not None:
        return int(e["gastrobar_priority"])
    return gastrobar_audience_priority(e)


def is_in_daily_window(e: dict[str, Any], now: datetime | None = None) -> bool:
    """Событие в окне 24 ч по Asia/Ho_Chi_Minh (aware local_datetime)."""
    from next24 import is_in_next24_window

    return is_in_next24_window(e, now=now, log_checks=False)


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


def _now24_bucket(ev: dict[str, Any]) -> str:
    from watchability import detect_editorial_type

    et = str(ev.get("editorial_type") or "").strip().lower()
    if not et:
        et = detect_editorial_type(ev)
    if et == "f1":
        return "f1"
    if et == "football":
        return "football"
    if et == "nhl":
        return "nhl"
    if et == "nba":
        return "nba"
    if et == "esports":
        return "esports"
    if et == "ufc":
        return "ufc"
    if et in ("eurovision", "live"):
        return "live"
    return "other"


def _prune_weak_rpl_if_strong_alternatives(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Слабые матчи РПЛ не показываем, если в окне уже есть заметно более сильные эфиры
    (еврокубки, топ-футбол, NHL/F1 и т.д.).
    """
    from watchability import detect_editorial_type

    if len(candidates) < 10:
        return candidates

    def strength(e: dict[str, Any]) -> int:
        et = detect_editorial_type(e)
        fs = int(e.get("football_watchability_score", 0))
        ws = int(e.get("watchability_score", 0))
        if et == "f1":
            return 95
        if et == "nhl" and ws >= 42:
            return 82
        if et == "football" and fs >= 74:
            return 90
        if et == "football" and fs >= 64:
            return 72
        if et == "nba":
            return 68
        return ws

    if max((strength(e) for e in candidates), default=0) < 72:
        return candidates

    out: list[dict[str, Any]] = []
    for e in candidates:
        try:
            lid = int(e.get("league_id") or 0)
        except (TypeError, ValueError):
            lid = 0
        fs = int(e.get("football_watchability_score", 0))
        if (
            detect_editorial_type(e) == "football"
            and lid == _RPL_LEAGUE_ID
            and fs < 54
        ):
            continue
        out.append(e)
    return out if len(out) >= 4 else candidates


def _select_now24_balanced(
    candidates: list[dict[str, Any]],
    *,
    limit: int,
    min_items: int,
) -> list[dict[str, Any]]:
    """Round-robin по категориям, затем добор по score; дедуп только exact."""
    from radar_dedupe import radar_dedupe_key

    order = ("f1", "football", "nhl", "nba", "esports", "ufc", "live", "other")
    buckets: dict[str, list[dict[str, Any]]] = {k: [] for k in order}
    for e in candidates:
        buckets[_now24_bucket(e)].append(e)

    def sk(x: dict[str, Any]) -> tuple[Any, ...]:
        return (
            -int(x.get("watchability_score", 0)),
            -int(x.get("football_watchability_score", 0)),
            _daily_priority_score(x),
            event_start_datetime_vn(x) or datetime.max.replace(tzinfo=TZ),
        )

    for k in buckets:
        buckets[k].sort(key=sk)

    taken: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []

    def try_take(ev: dict[str, Any]) -> None:
        dk = radar_dedupe_key(ev, exact=True)
        if dk in taken:
            return
        taken.add(dk)
        out.append(ev)

    max_round = max((len(buckets[k]) for k in order), default=0)
    for ri in range(max_round):
        if len(out) >= limit:
            break
        for k in order:
            if len(out) >= limit:
                break
            lst = buckets[k]
            if ri < len(lst):
                try_take(lst[ri])

    remainder_sorted = sorted(candidates, key=sk)
    for e in remainder_sorted:
        if len(out) >= limit:
            break
        try_take(e)

    floor_cap = min(limit, len(candidates))
    floor = min(min_items, floor_cap)
    if len(out) < floor:
        for e in remainder_sorted:
            if len(out) >= floor_cap:
                break
            try_take(e)

    out.sort(
        key=lambda e: event_start_datetime_vn(e) or datetime.max.replace(tzinfo=TZ),
    )

    log.info(
        "NOW24 FINAL_SELECTED=%s (limit=%s floor=%s candidates=%s)",
        len(out),
        limit,
        floor,
        len(candidates),
    )
    return out


def select_now24_events(
    events: list[dict[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """NOW24 из того же normalized pool: окно 24 ч, сортировка по datetime."""
    from event_radar_pipeline import (
        finalize_now24_output,
        in_time_window,
        normalize_radar_event,
        pipeline_finalize_events,
    )
    from next24 import log_next24_window_header

    now = now or _vn_now()
    pool = events or []
    log_next24_window_header(now)

    candidates: list[dict[str, Any]] = []
    for e in pool:
        ne = normalize_radar_event(dict(e))
        if ne is None:
            continue
        if not in_time_window(ne, "now24", now=now):
            continue
        candidates.append(ne)

    if not candidates:
        return []

    candidates = _prune_weak_rpl_if_strong_alternatives(candidates)
    if not candidates and pool:
        candidates = pipeline_finalize_events(pool, mode="now24")
    selected = finalize_now24_output(candidates)
    selected = [enrich_daily_campaign_meta(e, now) for e in selected]
    for e in selected:
        dt = event_start_datetime_vn(e)
        log.info(
            "NEXT24 SORTED: %s | %s",
            dt.isoformat() if dt else "?",
            e.get("title"),
        )
    return selected


def collect_campaign_events(
    events: list[dict[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """События для ежедневного поста сегодня (окно 24 ч + campaign_post_date)."""
    from gastrobar_event_filter import passes_gastrobar_content_filters

    now = now or _vn_now()
    pool = events or []
    out: list[dict[str, Any]] = []

    for e in pool:
        ok, ev = passes_gastrobar_content_filters(e)
        if not ok:
            continue
        if not is_in_daily_window(ev, now):
            continue
        cpd = campaign_post_date(ev)
        if cpd and cpd != now.date():
            continue
        out.append(enrich_daily_campaign_meta(ev, now))
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
