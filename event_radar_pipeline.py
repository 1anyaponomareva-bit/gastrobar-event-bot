"""
Event Radar: API + Gemini Search discovery → light verify → rule-based editor.

Gemini Search = scout (находит события).
Python light verify = гигиена (datetime, окно, не историческое).
radar_rules = финальный редактор (ranking/inclusion).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo
import time

from config import NOW24_MAX_ITEMS, RADAR_WEEKLY_MAX, RADAR_WEEKLY_TARGET_MIN
from next24 import resolve_event_local_datetime_vn, vn_now

log = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
WindowMode = Literal["now24", "next72", "week", "master"]
FetchScope = Literal["now24", "next72", "safe_72h"]

MASTER_CACHE_KEY = "betboom_3d"
NEXT72_OUTPUT_MIN = 8
NEXT72_OUTPUT_MAX = 20
NOW24_OUTPUT_MIN = 5
NOW24_OUTPUT_MAX = max(NOW24_OUTPUT_MIN, NOW24_MAX_ITEMS)

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
_TENNIS_TIER_RE = re.compile(
    r"\b(atp\s*500|wta\s*500|atp\s*1000|wta\s*1000|masters|hamburg|wimbledon|"
    r"roland\s+garros|us\s+open|australian\s+open)\b",
    re.I,
)

_last_normalized_pool: list[dict[str, Any]] = []
_shared_master_by_scope: dict[str, tuple[list[dict[str, Any]], RadarPipelineStats, float]] = {}
_MASTER_TTL_SEC = 300.0

_last_now24_debug: "Now24DebugSnapshot | None" = None


@dataclass
class Now24DebugSnapshot:
    now_local: str = ""
    window_end: str = ""
    all_events: int = 0
    after_normalize: int = 0
    parsed_ok: int = 0
    inside_window: int = 0
    after_window: int = 0
    after_score: int = 0
    after_final: int = 0
    outside_window: int = 0
    bad_datetime: int = 0
    already_started: int = 0
    drops: list[dict[str, str]] = field(default_factory=list)


def get_last_now24_debug() -> Now24DebugSnapshot | None:
    return _last_now24_debug


@dataclass
class RadarPipelineStats:
    label: str = "radar"
    api_fetch_note: str | None = None
    raw_found: int = 0
    after_worthy: int = 0
    after_lock: int = 0
    after_normalize: int = 0
    after_time_window: int = 0
    after_dedupe: int = 0
    after_score: int = 0
    final_selected: int = 0
    football_found: int = 0
    hockey_found: int = 0
    khl_found: int = 0
    nhl_found: int = 0
    world_hockey_found: int = 0
    f1_found: int = 0
    esports_found: int = 0
    nba_found: int = 0
    tennis_found: int = 0
    other_found: int = 0
    drops: dict[str, int] = field(default_factory=dict)
    dropped_samples: list[dict[str, str]] = field(default_factory=list)
    raw_by_sport: dict[str, int] = field(default_factory=dict)
    gemini_discovered: int = 0
    gemini_after_light: int = 0
    final_football: int = 0
    final_hockey: int = 0
    final_esports: int = 0
    final_f1: int = 0
    final_nba: int = 0
    final_ufc: int = 0

    def drop(
        self,
        reason: str,
        *,
        event: dict[str, Any] | None = None,
        now_local: datetime | None = None,
        end_local: datetime | None = None,
    ) -> None:
        self.drops[reason] = self.drops.get(reason, 0) + 1
        if event is not None:
            local_dt = event_datetime_vn(event)
            delta_h: str | float = "?"
            if local_dt is not None and now_local is not None:
                delta_h = round(
                    (local_dt - now_local).total_seconds() / 3600.0, 2
                )
            sample = {
                "title": str(event.get("title", ""))[:80],
                "category": str(event.get("category", "")),
                "sport": str(event.get("sport", "")),
                "local_datetime": (
                    local_dt.isoformat()
                    if local_dt
                    else str(
                        event.get("local_datetime")
                        or f"{event.get('local_date','')} {event.get('local_time','')}"
                    )
                ),
                "reason": reason,
                "delta_hours": str(delta_h),
            }
            if len(self.dropped_samples) < 20:
                self.dropped_samples.append(sample)
            if reason in ("outside_window", "already_started", "bad_datetime"):
                from event_datetime_norm import log_now24_drop

                log_now24_drop(
                    event,
                    reason,
                    now_local=now_local,
                    end_local=end_local,
                )
            else:
                log.info(
                    "DROP event=%r sport=%s reason=%s local=%s",
                    (event.get("title") or "")[:80],
                    event.get("sport"),
                    reason,
                    sample["local_datetime"],
                )

    def count_raw_sports(self, rows: list[dict[str, Any]]) -> None:
        from sports_events import classify_hockey_bucket

        fb = hk = khl = nhl = wh = f1 = esp = nba = tennis = 0
        for row in rows:
            sp = str(row.get("sport", "")).lower()
            if sp == "football":
                fb += 1
            elif sp == "hockey":
                hk += 1
                bucket = classify_hockey_bucket(row)
                if bucket == "khl":
                    khl += 1
                elif bucket == "nhl":
                    nhl += 1
                elif bucket == "world_hockey":
                    wh += 1
            elif sp in ("formula1", "f1"):
                f1 += 1
            elif sp == "esports":
                esp += 1
            elif sp == "basketball":
                nba += 1
            elif sp == "tennis":
                tennis += 1
        self.raw_by_sport = {
            "football": fb,
            "hockey": hk,
            "khl": khl,
            "nhl": nhl,
            "world_hockey": wh,
            "f1": f1,
            "esports": esp,
            "nba": nba,
            "tennis": tennis,
        }
        self.football_found = fb
        self.hockey_found = hk
        self.khl_found = khl
        self.nhl_found = nhl
        self.world_hockey_found = wh
        self.f1_found = f1
        self.esports_found = esp
        self.nba_found = nba
        self.tennis_found = tennis
        self.other_found = len(rows) - (fb + hk + f1 + esp + nba + tennis)

    def count_categories(self, events: list[dict[str, Any]]) -> None:
        from sports_events import classify_hockey_bucket

        self.football_found = sum(1 for e in events if e.get("sport") == "football")
        self.hockey_found = sum(1 for e in events if e.get("sport") == "hockey")
        self.khl_found = sum(
            1 for e in events if e.get("sport") == "hockey" and classify_hockey_bucket(e) == "khl"
        )
        self.nhl_found = sum(
            1 for e in events if e.get("sport") == "hockey" and classify_hockey_bucket(e) == "nhl"
        )
        self.world_hockey_found = sum(
            1
            for e in events
            if e.get("sport") == "hockey" and classify_hockey_bucket(e) == "world_hockey"
        )
        self.f1_found = sum(1 for e in events if e.get("sport") == "formula1")
        self.esports_found = sum(1 for e in events if e.get("sport") == "esports")
        self.nba_found = sum(1 for e in events if e.get("sport") == "basketball")
        self.tennis_found = sum(1 for e in events if e.get("sport") == "tennis")
        self.other_found = len(events) - (
            self.football_found
            + self.hockey_found
            + self.f1_found
            + self.esports_found
            + self.nba_found
            + self.tennis_found
        )

    def set_final_categories(self, events: list[dict[str, Any]]) -> None:
        from radar_rules import count_final_by_category

        fc = count_final_by_category(events)
        self.final_football = fc["FINAL_FOOTBALL"]
        self.final_hockey = fc["FINAL_HOCKEY"]
        self.final_esports = fc["FINAL_ESPORTS"]
        self.final_f1 = fc["FINAL_F1"]
        self.final_nba = fc["FINAL_NBA"]
        self.final_ufc = fc["FINAL_UFC"]

    def flush(self, *, suffix: str = "") -> None:
        tag = f"{self.label}{suffix}"
        log.info(
            "%s RADAR_RAW_TOTAL=%s AFTER_WORTHY=%s AFTER_LOCK=%s AFTER_NORMALIZE=%s "
            "AFTER_TIME_WINDOW=%s AFTER_DEDUPE=%s AFTER_SCORE=%s FINAL=%s | "
            "FOOTBALL_FOUND=%s HOCKEY_FOUND=%s KHL_FOUND=%s NHL_FOUND=%s "
            "WORLD_HOCKEY_FOUND=%s ESPORTS_FOUND=%s F1_FOUND=%s NBA_FOUND=%s "
            "TENNIS_FOUND=%s OTHER_FOUND=%s drops=%s",
            tag,
            self.raw_found,
            self.after_worthy,
            self.after_lock,
            self.after_normalize,
            self.after_time_window,
            self.after_dedupe,
            self.after_score,
            self.final_selected,
            self.football_found,
            self.hockey_found,
            self.khl_found,
            self.nhl_found,
            self.world_hockey_found,
            self.esports_found,
            self.f1_found,
            self.nba_found,
            self.tennis_found,
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
    # next72 / legacy week → 72 часа (не 7 дней)
    return now_local, now_local + timedelta(hours=72)


def in_time_window(
    e: dict[str, Any],
    mode: WindowMode,
    *,
    now: datetime | None = None,
) -> bool:
    start, end = window_bounds(mode, now)
    dt = event_datetime_vn(e)
    return dt is not None and start <= dt <= end


def slice_by_window(
    events: list[dict[str, Any]],
    mode: WindowMode,
    stats: RadarPipelineStats,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Фильтр окна ПОСЛЕ normalize/dedupe. Отдельные причины: already_started / outside_window."""
    from event_datetime_norm import log_now24_event_raw

    now_local = (now or vn_now()).astimezone(VN_TZ)
    start, end = window_bounds(mode, now_local)
    in_window: list[dict[str, Any]] = []
    for e in events:
        if mode == "now24":
            log_now24_event_raw(
                e,
                sport=str(e.get("sport", "")),
                now_local=now_local,
                end_local=end,
                phase="pre_window",
            )
        dt = event_datetime_vn(e)
        if dt is None:
            stats.drop(
                "bad_datetime",
                event=e,
                now_local=now_local,
                end_local=end,
            )
            continue
        if dt < now_local:
            stats.drop(
                "already_started",
                event=e,
                now_local=now_local,
                end_local=end,
            )
        elif dt > end:
            stats.drop(
                "outside_window",
                event=e,
                now_local=now_local,
                end_local=end,
            )
        else:
            in_window.append(e)
    stats.after_time_window = len(in_window)
    return in_window


def prepare_master_pool(normalized: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe + sort на полном массиве до разрезания week/now24."""
    from radar_dedupe import dedupe_events

    deduped = dedupe_events(normalized, log_prefix="radar_master", exact=True)
    return sort_events_chronological(deduped)


async def fetch_shared_master_pool(
    *,
    fetch_scope: FetchScope = "safe_72h",
    days_ahead: int = 3,
    include_gemini: bool = True,
    force_gemini: bool = False,
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], RadarPipelineStats]:
    """Один normalized master (SAFE_MODE_72H) для NOW24 и NEXT72; TTL 5 мин."""
    global _shared_master_by_scope, _last_normalized_pool

    cache_key = f"{MASTER_CACHE_KEY}_{days_ahead}d"
    cached = _shared_master_by_scope.get(cache_key)
    fresh = (
        not force_refresh
        and cached is not None
        and (time.monotonic() - cached[2]) < _MASTER_TTL_SEC
    )
    if fresh:
        master, stats, ts = cached
        log.info(
            "RADAR shared master cache hit key=%s events=%s age_sec=%.0f",
            cache_key,
            len(master),
            time.monotonic() - ts,
        )
        return master, stats

    normalized, stats = await build_normalized_radar_pool(
        stats_label=f"radar_collect_{days_ahead}d",
        days_ahead=days_ahead,
        include_gemini=include_gemini,
        force_gemini=force_gemini,
    )
    master = prepare_master_pool(normalized)
    _shared_master_by_scope[cache_key] = (master, stats, time.monotonic())
    _last_normalized_pool = master
    log.info(
        "RADAR shared master built SAFE_MODE_72H normalized=%s master=%s raw=%s",
        len(normalized),
        len(master),
        stats.raw_found,
    )
    return master, stats


def now24_drop_reason(
    e: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str | None:
    """Почему событие не попало в NOW24 (для диагностики пустого списка)."""
    dt = event_datetime_vn(e)
    if dt is None:
        return "bad_datetime"
    start, end = window_bounds("now24", now)
    if dt < start:
        return "already_started_or_past"
    if dt > end:
        return "outside_24h_window"
    reason = low_quality_drop_reason(e)
    if reason:
        return reason
    return None


def log_empty_now24_diagnostics(
    normalized: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    limit: int = 20,
) -> None:
    """Если NOW24 пуст — первые N событий с title/category/local_datetime/drop_reason."""
    now = now or vn_now()
    start, end = window_bounds("now24", now)
    log.warning(
        "NOW24 EMPTY DIAG: pool=%s window=%s .. %s",
        len(normalized),
        start.isoformat(),
        end.isoformat(),
    )
    rows: list[tuple[datetime, dict[str, Any], str]] = []
    for e in normalized:
        dt = event_datetime_vn(e)
        reason = now24_drop_reason(e, now=now) or "would_include"
        if dt is None:
            rows.append((datetime.max.replace(tzinfo=VN_TZ), e, reason))
        else:
            rows.append((dt, e, reason))
    rows.sort(key=lambda x: x[0])
    for dt, e, reason in rows[:limit]:
        log.warning(
            "NOW24 DIAG: title=%r category=%s sport=%s local=%s drop_reason=%s",
            (e.get("title") or "")[:100],
            e.get("category"),
            e.get("sport"),
            dt.isoformat() if dt != datetime.max.replace(tzinfo=VN_TZ) else None,
            reason,
        )


def resolve_sport_display(e: dict[str, Any]) -> dict[str, Any]:
    from radar_rules import detect_sport, emoji_for_sport

    sport = detect_sport(e)
    mapping = {
        "football": ("FOOTBALL", "football"),
        "hockey": ("HOCKEY", "hockey"),
        "formula1": ("SPORTS", "f1"),
        "esports": ("ESPORTS", "esports"),
        "basketball": ("BASKETBALL", "nba"),
        "tennis": ("SPORTS", "tennis"),
        "mma": ("SPORTS", "ufc"),
        "boxing": ("SPORTS", "ufc"),
    }
    category, editorial = mapping.get(sport, ("SPORTS", "other"))
    out = dict(e)
    out["sport"] = sport
    out["category"] = category
    out["emoji"] = emoji_for_sport(sport, out)
    out["editorial_type"] = editorial
    out["radar_rules_tier"] = None
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
    from radar_rules import rule_priority_score, rule_watchability_tier

    tier = rule_watchability_tier(e)
    score = rule_priority_score(e)
    ev = dict(e)
    ev["radar_rules_tier"] = tier
    ev["watchability_score"] = score
    ev["radar_priority_score"] = score
    return score


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
    elif ev["sport"] == "tennis" and not str(ev.get("subtitle", "")).strip():
        ev["subtitle"] = str(ev.get("league", "")).strip() or "Tennis"

    score = calculate_watchability_score(ev)
    from radar_rules import emoji_for_sport, rule_watchability_tier

    tier = rule_watchability_tier(ev)
    ev["radar_rules_tier"] = tier
    ev["emoji"] = emoji_for_sport(str(ev.get("sport", "")), ev)
    ev["radar_priority_score"] = score
    ev["watchability_score"] = score
    return ev


def sort_events_chronological(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        events,
        key=lambda x: event_datetime_vn(x) or datetime.max.replace(tzinfo=VN_TZ),
    )


def low_quality_drop_reason(
    e: dict[str, Any],
    *,
    for_now24: bool = False,
) -> str | None:
    """Rule-based: high/medium проходят; mandatory виды не режутся в NOW24."""
    from radar_rules import radar_rules_drop_reason

    if event_datetime_vn(e) is None:
        return "bad_datetime"
    return radar_rules_drop_reason(e, for_now24=for_now24)


def filter_low_quality_only(
    events: list[dict[str, Any]],
    stats: RadarPipelineStats,
    *,
    for_now24: bool = False,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in events:
        reason = low_quality_drop_reason(e, for_now24=for_now24)
        if reason:
            stats.drop(reason, event=e)
        else:
            out.append(e)
    stats.after_score = len(out)
    return out


async def collect_all_events(
    *,
    days_ahead: int = 3,
) -> tuple[list[dict[str, Any]], int, RadarPipelineStats, str | None]:
    from api_sports_status import is_api_failure_note
    from betboom_parser import merge_betboom_with_api_fallback
    from event_datetime_norm import log_now24_event_raw
    from next24 import next24_bounds
    from radar_sports_convert import lock_betboom_program_item, lock_api_sports_program_item
    from sports_events import (
        is_gastrobar_api_sport_worthy,
        raw_event_to_radar_program_item,
    )

    stats = RadarPipelineStats(label=f"radar_collect_{days_ahead}d")
    raw, fetch_note, _raw_n = await merge_betboom_with_api_fallback(days_ahead=days_ahead)
    stats.api_fetch_note = fetch_note
    source = fetch_note or "betboom"
    if fetch_note == "betboom_cache":
        source = "cache"
    elif fetch_note == "api_sports_fallback":
        source = "api"
    log.info("EVENT_RADAR_SOURCE_SELECTED=%s raw=%s", source, len(raw))
    if fetch_note in ("betboom_unavailable", "betboom_parse_error") and not raw:
        stats.raw_found = 0
        stats.flush(suffix="_BETBOOM_FAIL")
        return [], 0, stats, fetch_note
    if fetch_note == "api_sports_fallback":
        log.info("collect: using optional API-SPORTS fallback rows=%s", len(raw))
    elif is_api_failure_note(fetch_note) and not raw:
        stats.raw_found = 0
        stats.flush(suffix="_API_FAIL")
        return [], 0, stats, fetch_note

    use_betboom_lock = fetch_note in ("betboom_ok", "betboom_cache") or (
        bool(fetch_note) and str(fetch_note).startswith("betboom")
    )
    stats.raw_found = len(raw)
    stats.count_raw_sports(raw)
    stats.flush(suffix="_RAW")

    now_local, end_local = next24_bounds()
    log.info(
        "SAFE_MODE_72H collect: raw=%s now_local=%s end_24h=%s",
        len(raw),
        now_local.isoformat(),
        (now_local + timedelta(hours=72)).isoformat(),
    )
    for row in raw:
        log_now24_event_raw(
            row,
            sport=str(row.get("sport", "")),
            now_local=now_local,
            end_local=now_local + timedelta(hours=72),
            phase="api_raw",
        )

    locked: list[dict[str, Any]] = []
    worthy_rows: list[dict[str, Any]] = []
    for row in raw:
        if not is_gastrobar_api_sport_worthy(row):
            stats.drop("not_worthy", event=row)
            continue
        worthy_rows.append(row)
    stats.after_worthy = len(worthy_rows)

    for row in worthy_rows:
        item = raw_event_to_radar_program_item(row)
        if use_betboom_lock or str(row.get("source", "")).lower() == "betboom":
            le = lock_betboom_program_item(item, phase="radar_betboom")
        else:
            le = lock_api_sports_program_item(item, phase="radar_safe_72h")
        if le:
            locked.append(le)
            log_now24_event_raw(
                le,
                sport=str(le.get("sport", "")),
                now_local=now_local,
                end_local=now_local + timedelta(hours=72),
                phase="after_lock",
            )
        else:
            stats.drop("lock_failed", event=row)
            log.warning(
                "SAFE_MODE lock_failed title=%r ts=%s iso=%s",
                (row.get("title") or "")[:80],
                row.get("fixture_timestamp"),
                row.get("fixture_utc_iso"),
            )
    stats.after_lock = len(locked)
    stats.count_categories(locked)
    stats.flush(suffix="_LOCK")
    return locked, len(raw), stats, None


async def collect_gemini_discovery(
    *,
    force_gemini: bool = False,
    stats: RadarPipelineStats | None = None,
) -> tuple[list[dict[str, Any]], int, str | None]:
    """Gemini Search scout — отключён при quota / RADAR_GEMINI_DISCOVERY=0."""
    import asyncio

    from config import RADAR_GEMINI_DISCOVERY
    from gemini_client import gemini_search_disabled_reason, is_gemini_search_available

    st = stats or RadarPipelineStats(label="radar_gemini")
    if not RADAR_GEMINI_DISCOVERY:
        log.info("GEMINI_SEARCH_DISABLED_REASON=RADAR_GEMINI_DISCOVERY=0")
        return [], 0, "gemini_search_disabled"
    if not is_gemini_search_available():
        reason = gemini_search_disabled_reason() or "quota_exhausted"
        log.info("GEMINI_SEARCH_DISABLED_REASON=%s", reason)
        return [], 0, "gemini_quota"
    if not force_gemini:
        log.info("GEMINI_SEARCH_DISABLED_REASON=discovery_not_forced")
        return [], 0, "gemini_search_disabled"

    try:
        from event_radar import fetch_radar_multi_search_sync

        prelim, raw_n, note = await asyncio.to_thread(
            fetch_radar_multi_search_sync,
            force_gemini=force_gemini,
        )
    except Exception as exc:
        log.exception("Gemini discovery failed", exc_info=True)
        st.drop("gemini_error")
        return [], 0, "gemini_error"

    st.gemini_discovered = len(prelim)
    if note in ("gemini_quota", "gemini_api_key_missing"):
        log.info("Gemini discovery skipped: %s", note)
        return [], raw_n, note

    from radar_light_verify import light_verify_discovery_event

    verified: list[dict[str, Any]] = []
    for cand in prelim:
        lv = light_verify_discovery_event(cand)
        if lv:
            verified.append(lv)
        else:
            st.drop("light_verify_failed", event=cand)
    st.gemini_after_light = len(verified)
    log.info(
        "GEMINI DISCOVERY: raw_candidates=%s after_light_verify=%s note=%s",
        len(prelim),
        len(verified),
        note,
    )
    return verified, raw_n, note


async def build_normalized_radar_pool(
    *,
    stats_label: str = "radar_normalize",
    days_ahead: int = 3,
    include_gemini: bool = True,
    force_gemini: bool = False,
) -> tuple[list[dict[str, Any]], RadarPipelineStats]:
    """API + Gemini discovery → normalize (VN), без окна week/now24."""
    from radar_dedupe import dedupe_events

    stats = RadarPipelineStats(label=stats_label)
    locked, raw_n, collect_stats, api_note = await collect_all_events(
        days_ahead=days_ahead
    )
    if api_note:
        stats.api_fetch_note = api_note
        stats.flush(suffix="_API_FAIL")
        return [], stats
    stats.api_fetch_note = collect_stats.api_fetch_note
    stats.raw_found = raw_n
    stats.raw_by_sport = dict(collect_stats.raw_by_sport)
    stats.after_worthy = collect_stats.after_worthy
    stats.after_lock = collect_stats.after_lock
    stats.drops.update(collect_stats.drops)
    stats.dropped_samples.extend(collect_stats.dropped_samples[:10])

    merged: list[dict[str, Any]] = list(locked)
    gemini_note: str | None = None
    from config import RADAR_GEMINI_DISCOVERY
    from gemini_client import is_gemini_search_available

    allow_gemini = (
        include_gemini
        and RADAR_GEMINI_DISCOVERY
        and is_gemini_search_available()
        and force_gemini
    )
    if allow_gemini:
        gemini_events, _gem_raw, gemini_note = await collect_gemini_discovery(
            force_gemini=force_gemini,
            stats=stats,
        )
        if gemini_events:
            merged = dedupe_events(
                merged + gemini_events,
                log_prefix="api_gemini_merge",
                exact=True,
            )
            log.info(
                "RADAR MERGE locked=%s gemini_ok=%s merged=%s note=%s",
                len(locked),
                len(gemini_events),
                len(merged),
                gemini_note,
            )
    else:
        log.info(
            "RADAR Gemini discovery skipped (include=%s RADAR_GEMINI_DISCOVERY=%s search_ok=%s)",
            include_gemini,
            RADAR_GEMINI_DISCOVERY,
            is_gemini_search_available(),
        )

    normalized: list[dict[str, Any]] = []
    for e in merged:
        ne = normalize_radar_event(e)
        if ne is None:
            stats.drop("bad_datetime", event=e)
            continue
        normalized.append(ne)
    stats.after_normalize = len(normalized)
    stats.count_categories(normalized)
    stats.flush(suffix="_NORMALIZE")
    return normalized, stats


def now24_minimal_drop_reason(e: dict[str, Any]) -> str | None:
    """NOW24: только мусор и broken — без score/tier отсечения."""
    from event_verifier import gastrobar_hard_reject
    from radar_rules import _JUNK_RE, _blob

    if event_datetime_vn(e) is None:
        return "bad_datetime"
    if gastrobar_hard_reject(e):
        return "hard_reject"
    if _JUNK_RE.search(_blob(e)):
        return "junk_league"
    return None


def apply_now24_finalize(
    in_window: list[dict[str, Any]],
    stats: RadarPipelineStats,
) -> list[dict[str, Any]]:
    """
    NOW24 после окна 24ч: без aggressive ranking/score filter.
    Хронология + cap 15.
    """
    passed: list[dict[str, Any]] = []
    for e in in_window:
        reason = now24_minimal_drop_reason(e)
        if reason:
            stats.drop(reason, event=e)
        else:
            passed.append(e)

    out = sort_events_chronological(passed)
    if len(out) > NOW24_OUTPUT_MAX:
        log.info("NOW24 cap: %s -> %s", len(out), NOW24_OUTPUT_MAX)
        out = out[:NOW24_OUTPUT_MAX]
    stats.after_score = len(out)
    stats.after_dedupe = len(passed)
    stats.final_selected = len(out)
    log.info(
        "NOW24 soft finalize: in_window=%s after_minimal_filter=%s final=%s",
        len(in_window),
        len(passed),
        len(out),
    )
    return out


def build_now24_debug_snapshot(
    stats: RadarPipelineStats,
    *,
    master: list[dict[str, Any]],
    now: datetime | None = None,
) -> Now24DebugSnapshot:
    now_local, end_local = window_bounds("now24", now)
    drops: list[dict[str, str]] = []
    for sample in stats.dropped_samples[:15]:
        drops.append(
            {
                "title": sample.get("title", "?"),
                "local_datetime": sample.get("local_datetime", "?"),
                "reason": sample.get("reason", "?"),
                "delta_hours": sample.get("delta_hours", "?"),
            }
        )
    return Now24DebugSnapshot(
        now_local=now_local.isoformat(),
        window_end=end_local.isoformat(),
        all_events=stats.raw_found,
        after_normalize=len(master),
        parsed_ok=sum(1 for e in master if event_datetime_vn(e) is not None),
        inside_window=stats.after_time_window,
        after_window=stats.after_time_window,
        after_score=stats.after_score,
        after_final=stats.final_selected,
        outside_window=stats.drops.get("outside_window", 0),
        bad_datetime=stats.drops.get("bad_datetime", 0),
        already_started=stats.drops.get("already_started", 0),
        drops=drops,
    )


def apply_gastrobar_rules_layer(
    in_window: list[dict[str, Any]],
    stats: RadarPipelineStats,
    *,
    mode: WindowMode,
) -> list[dict[str, Any]]:
    """Rules: только skip-tier отсекается; ranking + category guarantees."""
    from radar_rules import (
        build_gastrobar_radar_output,
        check_rules_overfiltering,
        log_pool_scores,
        log_removed_by_rules,
        radar_rules_drop_reason,
    )

    log_pool_scores(in_window, label=stats.label)

    for e in in_window:
        reason = radar_rules_drop_reason(e, for_now24=(mode == "now24"))
        if reason:
            log_removed_by_rules(e, reason)
            stats.drop(reason, event=e)

    if mode == "now24":
        max_items = NOW24_OUTPUT_MAX
        min_items = NOW24_OUTPUT_MIN
        rules_mode = "now24"
    else:
        max_items = NEXT72_OUTPUT_MAX
        min_items = NEXT72_OUTPUT_MIN
        rules_mode = "next72"

    out = build_gastrobar_radar_output(
        in_window,
        mode=rules_mode,
        max_items=max_items,
        min_items=min_items,
    )
    stats.after_score = len(out)
    check_rules_overfiltering(
        len(in_window),
        stats.after_score,
        label=stats.label,
    )
    return out


def apply_window_and_finalize(
    master: list[dict[str, Any]],
    mode: WindowMode,
    *,
    stats_label: str | None = None,
    collect_stats: RadarPipelineStats | None = None,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], RadarPipelineStats]:
    """
    master = normalize → dedupe → sort (ALL_EVENTS).
    Сначала slice по окну, потом rules (week) или soft (now24).
    """
    stats = RadarPipelineStats(label=stats_label or f"radar_{mode}")
    if collect_stats:
        stats.raw_found = collect_stats.raw_found
        stats.after_normalize = len(master)
        stats.after_worthy = collect_stats.after_worthy
        stats.after_lock = collect_stats.after_lock
        stats.gemini_discovered = collect_stats.gemini_discovered
        stats.gemini_after_light = collect_stats.gemini_after_light
    else:
        stats.raw_found = len(master)
        stats.after_normalize = len(master)

    now = now or vn_now()
    in_window = slice_by_window(master, mode, stats, now=now)

    if mode == "now24":
        out = apply_now24_finalize(in_window, stats)
    elif mode in ("next72", "week"):
        out = apply_gastrobar_rules_layer(in_window, stats, mode="next72")
    else:
        out = apply_gastrobar_rules_layer(in_window, stats, mode=mode)
        stats.final_selected = len(out)

    stats.count_categories(out)
    stats.set_final_categories(out)

    log.info(
        "%s master=%s AFTER_WINDOW=%s AFTER_RULES=%s FINAL=%s | "
        "FINAL_FOOTBALL=%s FINAL_HOCKEY=%s FINAL_ESPORTS=%s FINAL_F1=%s "
        "FINAL_NBA=%s FINAL_UFC=%s",
        stats.label,
        len(master),
        stats.after_time_window,
        stats.after_score,
        stats.final_selected,
        stats.final_football,
        stats.final_hockey,
        stats.final_esports,
        stats.final_f1,
        stats.final_nba,
        stats.final_ufc,
    )
    stats.flush()
    return out, stats


async def build_master_radar_pool(
    *,
    stats_label: str = "radar_collect",
) -> tuple[list[dict[str, Any]], RadarPipelineStats]:
    """Недельный пул из shared master."""
    master, cstats = await fetch_shared_master_pool()
    return apply_window_and_finalize(
        master, "next72", stats_label=stats_label, collect_stats=cstats
    )


def now24_emergency_from_window(
    master: list[dict[str, Any]],
    collect_stats: RadarPipelineStats,
    *,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], RadarPipelineStats]:
    """Если soft-filter вычистил всё при непустом окне — только window + cap."""
    stats = RadarPipelineStats(label="radar_now24_emergency")
    stats.raw_found = collect_stats.raw_found
    stats.after_normalize = len(master)
    in_window = slice_by_window(master, "now24", stats, now=now)
    out = finalize_now24_output(in_window)
    stats.after_score = len(out)
    stats.final_selected = len(out)
    log.warning(
        "NOW24 emergency fallback: window=%s final=%s",
        len(in_window),
        len(out),
    )
    return out, stats


def slice_window(
    master: list[dict[str, Any]],
    mode: WindowMode,
    *,
    now: datetime | None = None,
    stats_label: str | None = None,
) -> list[dict[str, Any]]:
    """Срез уже отфильтрованного списка (legacy/cache)."""
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
    """Legacy alias → NEXT72 cap."""
    return finalize_next72_output(events)


def finalize_next72_output(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """8–20 событий, хронология."""
    out = sort_events_chronological(events)
    if len(out) > NEXT72_OUTPUT_MAX:
        log.info("NEXT72 cap: %s -> %s", len(out), NEXT72_OUTPUT_MAX)
        out = out[:NEXT72_OUTPUT_MAX]
    return out


def finalize_now24_output(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """5–15 событий в 24 ч, хронология (без Gemini top-N)."""
    out = sort_events_chronological(events)
    if len(out) > NOW24_OUTPUT_MAX:
        log.info("NOW24 cap: %s -> %s (by datetime)", len(out), NOW24_OUTPUT_MAX)
        out = out[:NOW24_OUTPUT_MAX]
    return out


_last_debug_stats: RadarPipelineStats | None = None


def format_radar_debug_message(
    stats: RadarPipelineStats,
    *,
    raw: dict[str, int] | None = None,
) -> str:
    rb = raw or stats.raw_by_sport
    lines = [
        "🔧 Event Radar Debug (rule-based)",
        "",
        f"RAW_TOTAL: {stats.raw_found}",
        f"FOOTBALL: {rb.get('football', 0)}",
        f"HOCKEY: {rb.get('hockey', 0)} (KHL {rb.get('khl', 0)}, NHL {rb.get('nhl', 0)}, WC {rb.get('world_hockey', 0)})",
        f"ESPORTS: {rb.get('esports', 0)}",
        f"F1: {rb.get('f1', 0)}",
        f"NBA: {rb.get('nba', 0)}",
        f"TENNIS: {rb.get('tennis', 0)}",
        f"GEMINI_DISCOVERED: {stats.gemini_discovered}",
        f"GEMINI_AFTER_LIGHT: {stats.gemini_after_light}",
        "",
        f"AFTER_WORTHY: {stats.after_worthy}",
        f"AFTER_LOCK: {stats.after_lock}",
        f"AFTER_NORMALIZE: {stats.after_normalize}",
        f"AFTER_WINDOW: {stats.after_time_window}",
        f"AFTER_RULES: {stats.after_score}",
        f"FINAL: {stats.final_selected}",
        "",
        f"FINAL_FOOTBALL: {stats.final_football}",
        f"FINAL_HOCKEY: {stats.final_hockey}",
        f"FINAL_ESPORTS: {stats.final_esports}",
        f"FINAL_F1: {stats.final_f1}",
        f"FINAL_NBA: {stats.final_nba}",
        f"FINAL_UFC: {stats.final_ufc}",
        "",
        f"drops: {stats.drops}",
    ]
    if stats.dropped_samples:
        lines.append("")
        lines.append("Удалённые (до 20):")
        for s in stats.dropped_samples:
            lines.append(
                f"• {s.get('title','?')} | {s.get('sport','?')} | {s.get('local_datetime','?')} → {s.get('reason','?')}"
            )
    return "\n".join(lines)


async def get_radar_debug_report() -> str:
    """Полный прогон API + Gemini для /radar_debug."""
    global _last_debug_stats
    from runtime_messages import build_tag_line

    master, norm_stats = await fetch_shared_master_pool(
        fetch_scope="safe_72h",
        include_gemini=False,
        force_gemini=False,
        force_refresh=True,
    )
    raw_by_sport = dict(norm_stats.raw_by_sport)

    week_final, week_stats = apply_window_and_finalize(
        master, "next72", stats_label="radar_debug_next72", collect_stats=norm_stats
    )
    now_final, now_stats = apply_window_and_finalize(
        master, "now24", stats_label="radar_debug_now24", collect_stats=norm_stats
    )

    merged = RadarPipelineStats(label="radar_debug_summary")
    merged.raw_found = norm_stats.raw_found
    merged.raw_by_sport = raw_by_sport
    merged.gemini_discovered = norm_stats.gemini_discovered
    merged.gemini_after_light = norm_stats.gemini_after_light
    merged.after_worthy = norm_stats.after_worthy
    merged.after_lock = norm_stats.after_lock
    merged.after_normalize = len(master)
    merged.after_time_window = week_stats.after_time_window
    merged.after_score = week_stats.after_score
    merged.final_selected = len(week_final)
    merged.set_final_categories(week_final)
    merged.football_found = week_stats.football_found
    merged.hockey_found = week_stats.hockey_found
    merged.khl_found = week_stats.khl_found
    merged.nhl_found = week_stats.nhl_found
    merged.world_hockey_found = week_stats.world_hockey_found
    merged.esports_found = week_stats.esports_found
    merged.f1_found = week_stats.f1_found
    merged.nba_found = week_stats.nba_found
    merged.tennis_found = week_stats.tennis_found
    merged.drops = {**norm_stats.drops, **week_stats.drops}
    merged.dropped_samples = (norm_stats.dropped_samples + week_stats.dropped_samples)[:20]
    _last_debug_stats = merged

    body = format_radar_debug_message(merged, raw=raw_by_sport)
    body += (
        f"\n\nNOW24 final: {len(now_final)} | NEXT72 final: {len(week_final)}\n"
        f"{build_tag_line()}"
    )
    return body


def count_now24_window_candidates(
    normalized: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> int:
    return sum(1 for e in normalized if in_time_window(e, "now24", now=now))


async def get_now24_from_pipeline(
    *,
    include_gemini: bool = True,
    force_gemini: bool = False,
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], int, int, str | None]:
    """
    (events, raw_total, after_window_count, note).
    Тот же shared master, что и WEEK. NOW24 = window first, soft filter after.
    """
    global _last_now24_debug

    master, norm_stats = await fetch_shared_master_pool(
        fetch_scope="safe_72h",
        days_ahead=2,
        include_gemini=include_gemini,
        force_gemini=force_gemini,
        force_refresh=force_refresh,
    )
    if norm_stats.api_fetch_note:
        return [], norm_stats.raw_found, 0, norm_stats.api_fetch_note
    window_n = count_now24_window_candidates(master)
    final, win_stats = apply_window_and_finalize(
        master, "now24", stats_label="radar_now24", collect_stats=norm_stats
    )

    _last_now24_debug = build_now24_debug_snapshot(win_stats, master=master)

    if not final and window_n > 0:
        log.warning(
            "NOW24 empty after soft filter but window=%s — emergency window-only fallback",
            window_n,
        )
        final, win_stats = now24_emergency_from_window(master, norm_stats)
        _last_now24_debug = build_now24_debug_snapshot(win_stats, master=master)
        if final:
            return final, norm_stats.raw_found, window_n, "now24_emergency"

    if not final:
        log_empty_now24_diagnostics(master)
        if window_n > 0:
            note = "api_window_only"
        elif norm_stats.after_normalize > 0:
            note = "api_window_empty"
        else:
            note = "api_ok_empty"
        return [], norm_stats.raw_found, window_n, note

    return final, norm_stats.raw_found, window_n, "api_unified"


async def get_next72_from_pipeline(
    *,
    include_gemini: bool = True,
    force_gemini: bool = False,
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], int, str | None]:
    master, stats = await fetch_shared_master_pool(
        fetch_scope="safe_72h",
        days_ahead=3,
        include_gemini=include_gemini,
        force_gemini=force_gemini,
        force_refresh=force_refresh,
    )
    if stats.api_fetch_note:
        return [], stats.raw_found, stats.api_fetch_note
    final, _ = apply_window_and_finalize(
        master, "next72", stats_label="radar_next72", collect_stats=stats
    )
    if final:
        note = "api_gemini_unified" if stats.gemini_after_light else "api_unified"
    elif stats.raw_found > 0:
        note = "api_ok_empty"
    else:
        note = "api_filter_empty"
    return final, stats.raw_found, note


async def get_week_from_pipeline(
    *,
    include_gemini: bool = True,
    force_gemini: bool = False,
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], int, str | None]:
    """Legacy alias → NEXT72."""
    return await get_next72_from_pipeline(
        include_gemini=include_gemini,
        force_gemini=force_gemini,
        force_refresh=force_refresh,
    )


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
    from radar_dedupe import dedupe_events

    master = prepare_master_pool(normalized)
    win_mode: WindowMode = "now24" if mode == "now24" else "next72"
    out, stats = apply_window_and_finalize(
        master, win_mode, stats_label=f"radar_{mode}_legacy", collect_stats=stats
    )
    if mode == "now24" and not out:
        log_empty_now24_diagnostics(master)
    stats.final_selected = len(out)
    stats.count_categories(out)
    stats.set_final_categories(out)
    stats.flush()
    return out
