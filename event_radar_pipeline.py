"""
Единый Event Radar pipeline (WEEK и NOW24 из одного массива).

collect → normalize (VN) → watchability score → filter_low_quality_only → dedupe → sort → slice.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from config import NOW24_MAX_ITEMS, RADAR_WEEKLY_MAX, RADAR_WEEKLY_TARGET_MIN
from next24 import resolve_event_local_datetime_vn, vn_now

log = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
WindowMode = Literal["now24", "week", "master"]

WEEKLY_SOFT_CAP = min(25, max(RADAR_WEEKLY_TARGET_MIN, int(RADAR_WEEKLY_MAX) if RADAR_WEEKLY_MAX < 100 else 25))

_WD = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")

_F1_SESSION_RE = re.compile(
    r"\b(practice\s*[123]?|fp[123]|qualifying|sprint|race|grand\s*prix)\b",
    re.I,
)
_ESPORTS_GAME_RE = re.compile(
    r"\b(cs2|counter-strike|dota\s*2?|lol|league\s+of\s+legends|valorant)\b",
    re.I,
)
_ESPORTS_TOUR_RE = re.compile(
    r"\b(dreamleague|dream\s+league|blast|major|esl|iem|pgl|betboom|msi|worlds|"
    r"navi|spirit|liquid|falcons|tundra|virtus|mouz|g2)\b",
    re.I,
)
_HOCKEY_RE = re.compile(
    r"\b(nhl|khl|stanley|iihf|world\s+championship|хоккей|hockey|playoff)\b",
    re.I,
)
_WORLD_HOCKEY_NATIONS = re.compile(
    r"\b(latvia|finland|canada|norway|denmark|slovakia|switzerland|germany|"
    r"sweden|czech|usa|austria|france|poland|italy|great\s+britain|uk)\b",
    re.I,
)
_FOOTBALL_RE = re.compile(
    r"\b(uefa|champions\s+league|europa\s+league|conference\s+league|premier\s+league|"
    r"la\s+liga|serie\s+a|bundesliga|ligue\s+1|rpl|российск|кубок|cup\s+final)\b",
    re.I,
)
_JUNK_LEAGUE_RE = re.compile(
    r"\b(u21|youth|reserve|friendly|товарищ|women'?s\s+league|"
    r"2nd\s+division|third\s+division|amateur)\b",
    re.I,
)


@dataclass
class RadarPipelineStats:
    label: str = "radar"
    raw_found: int = 0
    after_normalize: int = 0
    after_time_window: int = 0
    after_dedupe: int = 0
    after_score: int = 0
    final_selected: int = 0
    football_found: int = 0
    hockey_found: int = 0
    f1_found: int = 0
    esports_found: int = 0
    nba_found: int = 0
    other_found: int = 0
    drops: dict[str, int] = field(default_factory=dict)

    def drop(self, reason: str, *, event: dict[str, Any] | None = None) -> None:
        self.drops[reason] = self.drops.get(reason, 0) + 1
        if event is not None:
            log.info(
                "DROP event=%r sport=%s reason=%s local=%s",
                (event.get("title") or "")[:80],
                event.get("sport"),
                reason,
                event.get("local_datetime"),
            )

    def count_categories(self, events: list[dict[str, Any]]) -> None:
        self.football_found = sum(1 for e in events if e.get("sport") == "football")
        self.hockey_found = sum(1 for e in events if e.get("sport") == "hockey")
        self.f1_found = sum(1 for e in events if e.get("sport") == "formula1")
        self.esports_found = sum(1 for e in events if e.get("sport") == "esports")
        self.nba_found = sum(1 for e in events if e.get("sport") == "basketball")
        self.other_found = len(events) - (
            self.football_found
            + self.hockey_found
            + self.f1_found
            + self.esports_found
            + self.nba_found
        )

    def flush(self) -> None:
        log.info(
            "%s RAW_FOUND=%s AFTER_NORMALIZE=%s AFTER_TIME_WINDOW=%s "
            "AFTER_DEDUPE=%s AFTER_SCORE=%s FINAL_SELECTED=%s | "
            "FOOTBALL_FOUND=%s HOCKEY_FOUND=%s F1_FOUND=%s ESPORTS_FOUND=%s "
            "NBA_FOUND=%s OTHER_FOUND=%s drops=%s",
            self.label,
            self.raw_found,
            self.after_normalize,
            self.after_time_window,
            self.after_dedupe,
            self.after_score,
            self.final_selected,
            self.football_found,
            self.hockey_found,
            self.f1_found,
            self.esports_found,
            self.nba_found,
            self.other_found,
            self.drops,
        )


def _wd_short(d: datetime) -> str:
    return _WD[d.weekday()]


def format_day_label(event_local: datetime, now_local: datetime | None = None) -> str:
    now_local = now_local or vn_now()
    event_local = event_local.astimezone(VN_TZ)
    now_local = now_local.astimezone(VN_TZ)
    d = event_local.date()
    today = now_local.date()
    if d == today:
        return "СЕГОДНЯ"
    if d == today + timedelta(days=1):
        return "ЗАВТРА"
    return _wd_short(event_local)


def format_event_day_time(local_dt: datetime, now_local: datetime | None = None) -> str:
    """
    Время для афиши. 00:00–05:59 → ночь с предыдущего дня (ПТ→СБ 03:30).
    """
    now_local = now_local or vn_now()
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=VN_TZ)
    else:
        local_dt = local_dt.astimezone(VN_TZ)
    now_local = now_local.astimezone(VN_TZ)
    hhmm = local_dt.strftime("%H:%M")
    day_lbl = format_day_label(local_dt, now_local)

    if local_dt.hour < 6:
        prev = local_dt - timedelta(days=1)
        prev_s, cur_s = _wd_short(prev), _wd_short(local_dt)
        if day_lbl in ("СЕГОДНЯ", "ЗАВТРА"):
            return f"🌙 {day_lbl} {prev_s}→{cur_s} {hhmm}"
        return f"🌙 {prev_s}→{cur_s} {hhmm}"

    if day_lbl in ("СЕГОДНЯ", "ЗАВТРА"):
        return f"{day_lbl} {hhmm}"
    return f"{_wd_short(local_dt)} {hhmm}"


def event_datetime_vn(e: dict[str, Any]) -> datetime | None:
    return resolve_event_local_datetime_vn(e)


def window_bounds(
    mode: WindowMode,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    now_local = (now or vn_now()).astimezone(VN_TZ)
    if mode == "now24":
        return now_local, now_local + timedelta(hours=24)
    return now_local, now_local + timedelta(days=7)


def in_time_window(
    e: dict[str, Any],
    mode: WindowMode,
    *,
    now: datetime | None = None,
) -> bool:
    start, end = window_bounds(mode, now)
    dt = event_datetime_vn(e)
    return dt is not None and start <= dt <= end


def resolve_sport_display(e: dict[str, Any]) -> dict[str, Any]:
    blob = (
        f"{e.get('sport','')} {e.get('category','')} {e.get('title','')} "
        f"{e.get('subtitle','')} {e.get('league','')}"
    ).lower()
    sport_raw = str(e.get("sport", "")).strip().lower()

    if sport_raw in ("hockey", "formula1", "f1", "esports", "basketball", "football", "mma"):
        sport = "formula1" if sport_raw == "f1" else sport_raw
    elif _HOCKEY_RE.search(blob) or _WORLD_HOCKEY_NATIONS.search(
        str(e.get("title", "")).lower()
    ):
        sport = "hockey"
    elif _F1_SESSION_RE.search(blob) or "formula" in blob:
        sport = "formula1"
    elif _ESPORTS_GAME_RE.search(blob) or _ESPORTS_TOUR_RE.search(blob):
        sport = "esports"
    elif sport_raw == "basketball" or "nba" in blob:
        sport = "basketball"
    elif _FOOTBALL_RE.search(blob) or sport_raw == "football":
        sport = "football"
    else:
        sport = sport_raw or "other"

    mapping = {
        "football": ("FOOTBALL", "⚽", "football"),
        "hockey": ("HOCKEY", "🏒", "hockey"),
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
    gp_m = re.search(r"\b([A-Za-z][A-Za-z\s]{2,40})\s+GP\b", blob, re.I)
    gp = gp_m.group(0).strip() if gp_m else ""
    session = ""
    for label, pat in (
        ("Practice 1", r"practice\s*1|fp1"),
        ("Practice 2", r"practice\s*2|fp2"),
        ("Practice 3", r"practice\s*3|fp3"),
        ("Sprint Qualifying", r"sprint\s+qualifying"),
        ("Sprint", r"\bsprint\b"),
        ("Qualifying", r"qualifying"),
        ("Race", r"\brace\b"),
    ):
        if re.search(pat, blob, re.I):
            session = label
            break
    parts = ["Formula 1"]
    if gp:
        parts.append(gp.replace(" GP", " Grand Prix"))
    if session:
        parts.append(session)
    return " · ".join(parts) if len(parts) > 1 else "Formula 1"


def calculate_watchability_score(e: dict[str, Any]) -> int:
    from watchability import enrich_watchability

    ev = enrich_watchability(dict(e))
    base = int(ev.get("watchability_score", 0))
    fs = int(ev.get("football_watchability_score", 0))
    score = max(base, fs)
    blob = f"{ev.get('title','')} {ev.get('subtitle','')} {ev.get('league','')}".lower()
    sport = str(ev.get("sport", "")).lower()

    if sport == "formula1":
        score = max(score, 55)
    if sport == "hockey":
        score = max(score, 50)
        if _WORLD_HOCKEY_NATIONS.search(blob) or "world championship" in blob:
            score = max(score, 62)
        if "playoff" in blob or "stanley" in blob:
            score = max(score, 58)
    if sport == "esports":
        score = max(score, 48)
        if _ESPORTS_TOUR_RE.search(blob):
            score = max(score, 58)
    if sport == "football":
        score = max(score, fs, 45)
    if re.search(r"\b(final|playoff|semi|quarter)\b", blob, re.I):
        score += 10

    return min(100, score)


def normalize_radar_event(e: dict[str, Any]) -> dict[str, Any] | None:
    ev = resolve_sport_display(dict(e))
    dt = event_datetime_vn(ev)
    if dt is None:
        return None
    ev["local_datetime"] = dt.isoformat()
    ev["event_datetime_vn"] = dt
    ev["local_date"] = dt.date().isoformat()
    ev["local_time"] = dt.strftime("%H:%M")
    ev["local_weekday"] = _wd_short(dt)
    ev["weekday"] = ev["local_weekday"]
    ev["display_time"] = ev["local_time"]
    ev["timezone"] = "Asia/Ho_Chi_Minh"

    if ev["sport"] == "formula1":
        ev["subtitle"] = _f1_display_subtitle(ev)
        ev["league"] = ev["subtitle"]
    elif ev["sport"] == "esports" and not str(ev.get("subtitle", "")).strip():
        t = str(ev.get("title", "")).lower()
        if "cs2" in t or "counter" in t:
            ev["subtitle"] = "CS2"
        elif "dota" in t:
            ev["subtitle"] = "Dota 2"

    score = calculate_watchability_score(ev)
    ev["radar_priority_score"] = score
    ev["watchability_score"] = score
    return ev


def sort_events_chronological(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        events,
        key=lambda x: event_datetime_vn(x) or datetime.max.replace(tzinfo=VN_TZ),
    )


def is_priority_gastrobar_event(e: dict[str, Any]) -> bool:
    """События, которые нельзя выкидывать из-за score/ranking."""
    sport = str(e.get("sport", "")).lower()
    blob = f"{e.get('title','')} {e.get('subtitle','')} {e.get('league','')}".lower()

    if sport == "formula1":
        return True
    if sport == "hockey":
        return True
    if sport == "esports":
        return True
    if sport == "basketball" and ("nba" in blob or "playoff" in blob):
        return True
    if sport == "football":
        from football_watchability import is_eligible_football_league_now24

        item = {
            "league_id": e.get("league_id"),
            "league_country": e.get("league_country", ""),
            "league": e.get("league") or e.get("subtitle", ""),
            "title": e.get("title", ""),
        }
        if is_eligible_football_league_now24(item):
            return True
        if _FOOTBALL_RE.search(blob):
            return True
    return False


def low_quality_drop_reason(e: dict[str, Any]) -> str | None:
    """Только мусор / вне окна / без времени — не режем приоритетные виды спорта."""
    from event_verifier import gastrobar_hard_reject

    if gastrobar_hard_reject(e):
        return "hard_reject"

    if event_datetime_vn(e) is None:
        return "bad_datetime"

    if is_priority_gastrobar_event(e):
        blob = f"{e.get('title','')} {e.get('league','')}".lower()
        if _JUNK_LEAGUE_RE.search(blob):
            return "junk_league"
        return None

    blob = f"{e.get('title','')} {e.get('subtitle','')} {e.get('league','')}".lower()
    if _JUNK_LEAGUE_RE.search(blob):
        return "junk_league"
    if int(e.get("radar_priority_score", 0)) < 12:
        return "very_low_score"
    return None


def filter_low_quality_only(
    events: list[dict[str, Any]],
    stats: RadarPipelineStats,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in events:
        reason = low_quality_drop_reason(e)
        if reason:
            stats.drop(reason, event=e)
        else:
            out.append(e)
    stats.after_score = len(out)
    return out


async def collect_all_events() -> tuple[list[dict[str, Any]], int]:
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
    stats_label: str = "radar_collect",
) -> tuple[list[dict[str, Any]], RadarPipelineStats]:
    stats = RadarPipelineStats(label=stats_label)
    locked, raw_n = await collect_all_events()
    stats.raw_found = raw_n

    normalized: list[dict[str, Any]] = []
    for e in locked:
        ne = normalize_radar_event(e)
        if ne is None:
            stats.drop("bad_datetime", event=e)
            continue
        normalized.append(ne)
    stats.after_normalize = len(normalized)

    in_week: list[dict[str, Any]] = []
    now = vn_now()
    for e in normalized:
        if in_time_window(e, "week", now=now):
            in_week.append(e)
        else:
            dt = event_datetime_vn(e)
            if dt and dt < now:
                stats.drop("old", event=e)
            else:
                stats.drop("outside_window", event=e)
    stats.after_time_window = len(in_week)

    scored = filter_low_quality_only(in_week, stats)

    from radar_dedupe import dedupe_events

    deduped = dedupe_events(scored, log_prefix="radar_master", exact=True)
    stats.after_dedupe = len(deduped)

    out = sort_events_chronological(deduped)
    stats.final_selected = len(out)
    stats.count_categories(out)
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
    stats.raw_found = len(master)
    picked: list[dict[str, Any]] = []
    for e in master:
        if in_time_window(e, mode, now=now):
            picked.append(e)
        else:
            stats.drop("outside_window", event=e)
    stats.after_time_window = len(picked)
    stats.after_dedupe = len(picked)
    stats.after_score = len(picked)
    out = sort_events_chronological(picked)
    stats.final_selected = len(out)
    stats.count_categories(out)
    stats.flush()
    return out


def finalize_week_output(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """10–25+ событий: все категории, хронология; мягкий cap только если >25."""
    out = sort_events_chronological(events)
    if len(out) > WEEKLY_SOFT_CAP:
        log.info(
            "WEEK soft cap: %s -> %s (chronological head)",
            len(out),
            WEEKLY_SOFT_CAP,
        )
        out = out[:WEEKLY_SOFT_CAP]
    return out


def finalize_now24_output(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Все события в 24 ч по времени, без score-ranking."""
    out = sort_events_chronological(events)
    if len(out) > NOW24_MAX_ITEMS:
        log.info("NOW24 cap: %s -> %s (by datetime)", len(out), NOW24_MAX_ITEMS)
        out = out[:NOW24_MAX_ITEMS]
    return out


async def get_now24_from_pipeline() -> tuple[list[dict[str, Any]], int, str | None]:
    master, stats = await build_master_radar_pool(stats_label="radar_collect")
    sliced = slice_window(master, "now24", stats_label="radar_now24")
    final = finalize_now24_output(sliced)
    note = "api_unified" if final else "api_filter_empty"
    return final, stats.raw_found, note


async def get_week_from_pipeline() -> tuple[list[dict[str, Any]], int, str | None]:
    master, stats = await build_master_radar_pool(stats_label="radar_collect")
    final = finalize_week_output(master)
    note = "api_unified" if final else "api_filter_empty"
    return final, stats.raw_found, note


def enrich_events_for_display(
    events: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now_local = now or vn_now()
    out: list[dict[str, Any]] = []
    for e in sort_events_chronological(events):
        ev = dict(e)
        dt = event_datetime_vn(ev)
        if dt:
            ev["day_label"] = format_day_label(dt, now_local)
            ev["display_day_time"] = format_event_day_time(dt, now_local)
            ev["local_weekday"] = _wd_short(dt)
        out.append(ev)
    return out


def pipeline_finalize_events(
    events: list[dict[str, Any]],
    *,
    mode: WindowMode,
) -> list[dict[str, Any]]:
    """Нормализация произвольного списка (Gemini/cache) тем же pipeline."""
    stats = RadarPipelineStats(label=f"radar_{mode}_legacy")
    normalized: list[dict[str, Any]] = []
    for e in events:
        ne = normalize_radar_event(e)
        if ne is None:
            stats.drop("bad_datetime", event=e)
            continue
        if not in_time_window(ne, mode):
            stats.drop("outside_window", event=ne)
            continue
        normalized.append(ne)
    stats.after_normalize = len(normalized)
    stats.after_time_window = len(normalized)
    scored = filter_low_quality_only(normalized, stats)
    from radar_dedupe import dedupe_events

    deduped = dedupe_events(scored, log_prefix=f"radar_{mode}_legacy", exact=True)
    stats.after_dedupe = len(deduped)
    out = sort_events_chronological(deduped)
    if mode == "now24":
        out = finalize_now24_output(out)
    else:
        out = finalize_week_output(out)
    stats.final_selected = len(out)
    stats.count_categories(out)
    stats.flush()
    return out
