"""
Единый Event Radar pipeline: collect → normalize (VN) → filter → sort → NOW24 / WEEK.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from config import NOW24_MAX_ITEMS, NOW24_MIN_ITEMS, RADAR_MIN_WATCHABILITY
from next24 import resolve_event_local_datetime_vn, vn_now

log = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
WindowMode = Literal["now24", "week", "master"]

_F1_SESSION_RE = re.compile(
    r"\b(practice\s*[123]?|fp[123]|qualifying|sprint|race|grand\s*prix)\b",
    re.I,
)
_ESPORTS_GAME_RE = re.compile(
    r"\b(cs2|counter-strike|dota\s*2?|lol|league\s+of\s+legends|valorant)\b",
    re.I,
)
_ESPORTS_TOUR_RE = re.compile(
    r"\b(dreamleague|dream\s+league|blast|major|esl|iem|pgl|betboom|msi|worlds)\b",
    re.I,
)
_HOCKEY_RE = re.compile(
    r"\b(nhl|khl|stanley|iihf|world\s+championship|хоккей|hockey)\b",
    re.I,
)
_FOOTBALL_RE = re.compile(
    r"\b(uefa|champions\s+league|europa\s+league|premier\s+league|la\s+liga|"
    r"serie\s+a|bundesliga|ligue\s+1|rpl|российск|кубок\s+рф)\b",
    re.I,
)
_FOOTBALL_FALSE_CHAMP = re.compile(
    r"\bchampionship\b",
    re.I,
)


@dataclass
class RadarPipelineStats:
    label: str = "radar"
    total_raw: int = 0
    after_normalize: int = 0
    after_window: int = 0
    after_priority: int = 0
    final_selected: int = 0
    drops: dict[str, int] = field(default_factory=dict)

    def drop(self, reason: str, n: int = 1) -> None:
        self.drops[reason] = self.drops.get(reason, 0) + n

    def flush(self) -> None:
        log.info(
            "%s TOTAL_RAW_EVENTS=%s AFTER_NORMALIZE=%s AFTER_WINDOW=%s "
            "AFTER_PRIORITY=%s FINAL_SELECTED=%s drops=%s",
            self.label,
            self.total_raw,
            self.after_normalize,
            self.after_window,
            self.after_priority,
            self.final_selected,
            self.drops,
        )
        if self.after_window == 0 and self.after_normalize > 0:
            log.warning(
                "%s after_window=0 but after_normalize=%s — проверьте datetime/timezone",
                self.label,
                self.after_normalize,
            )


def format_day_label(event_local: datetime, now_local: datetime | None = None) -> str:
    """СЕГОДНЯ / ЗАВТРА / ПТ … в timezone Asia/Ho_Chi_Minh."""
    now_local = now_local or vn_now()
    if event_local.tzinfo is None:
        event_local = event_local.replace(tzinfo=VN_TZ)
    else:
        event_local = event_local.astimezone(VN_TZ)
    now_local = now_local.astimezone(VN_TZ)
    d = event_local.date()
    today = now_local.date()
    if d == today:
        return "СЕГОДНЯ"
    if d == today + timedelta(days=1):
        return "ЗАВТРА"
    wd = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")
    return wd[d.weekday()]


def event_datetime_vn(e: dict[str, Any]) -> datetime | None:
    return resolve_event_local_datetime_vn(e)


def window_bounds(
    mode: WindowMode,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    now_local = now or vn_now()
    if now_local.tzinfo is None:
        now_local = now_local.replace(tzinfo=VN_TZ)
    else:
        now_local = now_local.astimezone(VN_TZ)
    if mode == "now24":
        return now_local, now_local + timedelta(hours=24)
    if mode == "week":
        return now_local, now_local + timedelta(days=7)
    return now_local, now_local + timedelta(days=7)


def in_time_window(
    e: dict[str, Any],
    mode: WindowMode,
    *,
    now: datetime | None = None,
) -> bool:
    start, end = window_bounds(mode, now)
    dt = event_datetime_vn(e)
    if dt is None:
        return False
    return start <= dt <= end


def resolve_sport_display(e: dict[str, Any]) -> dict[str, Any]:
    """
    Канонический sport / category / emoji. Хоккей проверяем до футбольных эвристик.
    """
    blob = (
        f"{e.get('sport','')} {e.get('category','')} {e.get('title','')} "
        f"{e.get('subtitle','')} {e.get('league','')}"
    ).lower()
    sport_raw = str(e.get("sport", "")).strip().lower()

    if sport_raw in ("hockey", "formula1", "f1", "esports", "basketball", "football", "mma"):
        sport = "formula1" if sport_raw == "f1" else sport_raw
    elif _HOCKEY_RE.search(blob) and not (
        _FOOTBALL_RE.search(blob)
        and not _HOCKEY_RE.search(str(e.get("title", "")).lower())
    ):
        sport = "hockey"
    elif _F1_SESSION_RE.search(blob) or "formula" in blob or sport_raw == "formula1":
        sport = "formula1"
    elif _ESPORTS_GAME_RE.search(blob) or _ESPORTS_TOUR_RE.search(blob) or sport_raw == "esports":
        sport = "esports"
    elif sport_raw == "basketball" or "nba" in blob:
        sport = "basketball"
    elif (
        _FOOTBALL_RE.search(blob)
        or "football" in str(e.get("category", "")).lower()
        or sport_raw == "football"
    ):
        sport = "football"
    else:
        sport = sport_raw or "other"

    mapping = {
        "football": ("FOOTBALL", "⚽", "football"),
        "hockey": ("HOCKEY", "🏒", "nhl"),
        "formula1": ("SPORTS", "🏎", "f1"),
        "esports": ("ESPORTS", "🎮", "esports"),
        "basketball": ("BASKETBALL", "🏀", "nba"),
        "mma": ("SPORTS", "🥊", "ufc"),
    }
    category, emoji, editorial = mapping.get(sport, ("SPORTS", "🏟", "other"))
    out = dict(e)
    out["sport"] = sport
    out["category"] = category
    out["emoji"] = emoji
    out["editorial_type"] = editorial
    return out


def _f1_display_subtitle(e: dict[str, Any]) -> str:
    blob = f"{e.get('title','')} {e.get('subtitle','')} {e.get('league','')}"
    gp = ""
    m = re.search(r"\b([A-Za-z][A-Za-z\s]{2,30})\s+GP\b", blob, re.I)
    if m:
        gp = m.group(0).strip()
    session = ""
    for pat, label in (
        (_F1_SESSION_RE, None),
    ):
        m2 = pat.search(blob)
        if m2:
            session = m2.group(0).strip().title()
            break
    if not session:
        if "qualifying" in blob.lower():
            session = "Qualifying"
        elif "practice" in blob.lower() or "fp" in blob.lower():
            session = "Practice"
        elif "sprint" in blob.lower():
            session = "Sprint"
        elif "race" in blob.lower():
            session = "Race"
    parts = ["Formula 1"]
    if gp:
        parts.append(gp)
    if session:
        parts.append(session)
    return " · ".join(parts) if len(parts) > 1 else (parts[0] if parts else "Formula 1")


def compute_radar_priority_score(e: dict[str, Any]) -> int:
    from watchability import enrich_watchability, is_major_weekly_event

    ev = enrich_watchability(dict(e))
    base = int(ev.get("watchability_score", 0))
    fs = int(ev.get("football_watchability_score", 0))
    score = max(base, fs)
    blob = f"{ev.get('title','')} {ev.get('subtitle','')} {ev.get('league','')}".lower()
    sport = str(ev.get("sport", "")).lower()

    if is_major_weekly_event(ev):
        score += 18
    if re.search(r"\b(final|playoff|play-off|semi|quarter|matchday)\b", blob, re.I):
        score += 12
    if sport == "formula1" or str(ev.get("editorial_type")) == "f1":
        score += 14
    if sport == "hockey" and re.search(r"\b(nhl|stanley|world\s+championship|khl)\b", blob, re.I):
        score += 12
    if sport == "esports" and _ESPORTS_TOUR_RE.search(blob):
        score += 14
    if re.search(r"\b(champions\s+league|europa\s+league|ucl|uel)\b", blob, re.I):
        score += 16
    if _ESPORTS_GAME_RE.search(blob):
        score += 8

    return min(100, score)


def normalize_radar_event(e: dict[str, Any]) -> dict[str, Any] | None:
    """Timezone-aware VN datetime + sport mapping + priority score."""
    ev = resolve_sport_display(dict(e))
    dt = event_datetime_vn(ev)
    if dt is None:
        return None
    ev["local_datetime"] = dt.isoformat()
    ev["event_datetime_vn"] = dt
    ev["local_date"] = dt.date().isoformat()
    ev["local_time"] = dt.strftime("%H:%M")
    wd = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")
    ev["local_weekday"] = wd[dt.weekday()]
    ev["weekday"] = ev["local_weekday"]
    ev["display_time"] = ev["local_time"]
    ev["timezone"] = "Asia/Ho_Chi_Minh"

    if str(ev.get("sport")) == "formula1":
        ev["subtitle"] = _f1_display_subtitle(ev)
        ev["league"] = ev["subtitle"]
    elif str(ev.get("sport")) == "esports":
        title = str(ev.get("title", ""))
        if _ESPORTS_GAME_RE.search(title.lower()):
            game = "CS2" if "cs" in title.lower() else "Dota 2" if "dota" in title.lower() else "Esports"
            if not str(ev.get("subtitle", "")).strip():
                ev["subtitle"] = game

    ev["radar_priority_score"] = compute_radar_priority_score(ev)
    ev["watchability_score"] = max(
        int(ev.get("watchability_score", 0)), int(ev["radar_priority_score"])
    )
    return ev


def sort_events_chronological(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        events,
        key=lambda x: event_datetime_vn(x) or datetime.max.replace(tzinfo=VN_TZ),
    )


def _passes_content_gate(e: dict[str, Any], *, min_score: int) -> tuple[bool, str]:
    from event_verifier import gastrobar_hard_reject
    from gastrobar_event_filter import (
        passes_gastrobar_content_filters,
        passes_gastrobar_watchability_floor,
    )

    if gastrobar_hard_reject(e):
        return False, "hard_reject"

    via_api = str(e.get("verified_via", "")).upper() == "API-SPORTS"
    sport = str(e.get("sport", "")).lower()
    if via_api and e.get("local_datetime") and sport != "football":
        floor = max(12, min_score - 16)
        if int(e.get("radar_priority_score", 0)) >= floor:
            return True, ""
        if passes_gastrobar_watchability_floor(e):
            return True, ""
        return False, "low_priority"

    ok, ev = passes_gastrobar_content_filters(e, enrich=False)
    if not ok:
        return False, "content_filter"
    if int(ev.get("radar_priority_score", 0)) < min_score and not passes_gastrobar_watchability_floor(
        ev
    ):
        return False, "low_priority"
    return True, ""


async def collect_raw_locked_events() -> tuple[list[dict[str, Any]], int]:
    from radar_sports_convert import lock_api_sports_program_item
    from sports_events import (
        _merge_raw_week_events,
        is_gastrobar_api_sport_worthy,
        raw_event_to_radar_program_item,
    )

    raw = await _merge_raw_week_events()
    locked: list[dict[str, Any]] = []
    for row in raw:
        if not is_gastrobar_api_sport_worthy(row):
            continue
        item = raw_event_to_radar_program_item(row)
        le = lock_api_sports_program_item(item, phase="radar_unified")
        if le:
            locked.append(le)
    return locked, len(raw)


async def build_master_radar_pool(
    *,
    stats_label: str = "radar_master",
) -> tuple[list[dict[str, Any]], RadarPipelineStats]:
    stats = RadarPipelineStats(label=stats_label)
    locked, raw_n = await collect_raw_locked_events()
    stats.total_raw = raw_n

    normalized: list[dict[str, Any]] = []
    for e in locked:
        ne = normalize_radar_event(e)
        if ne is None:
            stats.drop("bad_datetime")
            continue
        normalized.append(ne)
    stats.after_normalize = len(normalized)

    in_week: list[dict[str, Any]] = []
    for e in normalized:
        if in_time_window(e, "week"):
            in_week.append(e)
        else:
            dt = event_datetime_vn(e)
            if dt and dt < vn_now():
                stats.drop("old")
            else:
                stats.drop("outside_window")
    stats.after_window = len(in_week)

    min_score = RADAR_MIN_WATCHABILITY
    filtered: list[dict[str, Any]] = []
    for e in in_week:
        ok, reason = _passes_content_gate(e, min_score=min_score)
        if ok:
            filtered.append(e)
        else:
            stats.drop(reason or "low_priority")
    stats.after_priority = len(filtered)

    if not filtered and in_week:
        log.warning(
            "%s after_priority=0 — ослабляем порог (raw_normalize=%s window=%s)",
            stats.label,
            stats.after_normalize,
            stats.after_window,
        )
        relaxed_floor = max(10, min_score - 12)
        for e in in_week:
            if int(e.get("radar_priority_score", 0)) >= relaxed_floor:
                filtered.append(e)
        stats.drop("relaxed_pass", len(filtered))
        stats.after_priority = len(filtered)

    from radar_dedupe import dedupe_events

    out = sort_events_chronological(dedupe_events(filtered, log_prefix="radar_master", exact=True))
    stats.final_selected = len(out)
    stats.flush()
    return out, stats


def slice_window(
    master: list[dict[str, Any]],
    mode: WindowMode,
    *,
    now: datetime | None = None,
    stats_label: str | None = None,
) -> list[dict[str, Any]]:
    stats = RadarPipelineStats(label=stats_label or f"radar_{mode}")
    stats.total_raw = len(master)
    picked: list[dict[str, Any]] = []
    for e in master:
        if in_time_window(e, mode, now=now):
            picked.append(e)
        else:
            stats.drop("outside_window")
    stats.after_window = len(picked)
    stats.after_priority = len(picked)
    out = sort_events_chronological(picked)
    stats.final_selected = len(out)
    stats.flush()
    return out


def cap_now24_chronological(
    events: list[dict[str, Any]],
    *,
    max_items: int | None = None,
    min_items: int | None = None,
) -> list[dict[str, Any]]:
    """Без round-robin: первые N по времени; при нехватке — ослабление порога."""
    max_items = max_items if max_items is not None else NOW24_MAX_ITEMS
    min_items = min_items if min_items is not None else NOW24_MIN_ITEMS
    sorted_ev = sort_events_chronological(events)
    if len(sorted_ev) <= max_items:
        out = sorted_ev
    else:
        out = sorted_ev[:max_items]

    if len(out) < min(min_items, len(sorted_ev)) and len(sorted_ev) > len(out):
        from gastrobar_event_filter import passes_gastrobar_content_filters

        relaxed: list[dict[str, Any]] = []
        floor = max(12, RADAR_MIN_WATCHABILITY - 10)
        for e in sorted_ev:
            if int(e.get("radar_priority_score", 0)) >= floor:
                relaxed.append(e)
            elif passes_gastrobar_content_filters(e, enrich=False)[0]:
                relaxed.append(e)
        out = sort_events_chronological(relaxed)[: max(max_items, min_items)]

    log.info(
        "NOW24 cap chronological: candidates=%s final=%s max=%s min=%s",
        len(events),
        len(out),
        max_items,
        min_items,
    )
    return out


async def get_now24_from_pipeline() -> tuple[list[dict[str, Any]], int, str | None]:
    master, stats = await build_master_radar_pool(stats_label="radar_collect")
    sliced = slice_window(master, "now24", stats_label="radar_now24")
    final = cap_now24_chronological(sliced)
    note = "api_unified" if final else "api_filter_empty"
    return final, stats.total_raw, note


async def get_week_from_pipeline() -> tuple[list[dict[str, Any]], int, str | None]:
    master, stats = await build_master_radar_pool(stats_label="radar_collect")
    final = slice_window(master, "week", stats_label="radar_week")
    note = "api_unified" if final else "api_filter_empty"
    return final, stats.total_raw, note


def enrich_events_for_display(
    events: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Добавляет day_label для форматтера (строго после сортировки по времени)."""
    now_local = now or vn_now()
    out: list[dict[str, Any]] = []
    for e in sort_events_chronological(events):
        ev = dict(e)
        dt = event_datetime_vn(ev)
        if dt:
            ev["day_label"] = format_day_label(dt, now_local)
            ev["local_weekday"] = ev["day_label"]
        out.append(ev)
    return out
