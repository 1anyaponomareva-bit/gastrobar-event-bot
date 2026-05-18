"""
Event Radar — AI Event Editor для Gastrobar (Gemini Search + watchability score).

Отбор: что реально смотреть в баре (топ-матчи, дерби, F1-уикенд, плей-офф, не только grand finals).
Отсекаются сериальные финалы Chicago Med/Fire/P.D. и прочий TV noise.

По умолчанию один запрос Gemini+Search на всю неделю (экономия лимита free tier). Несколько шардов —
только если в .env RADAR_MULTI_SHARD=1 (для платного/высокого лимита).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import date, timedelta
from typing import Any

from concurrent.futures import ThreadPoolExecutor, as_completed

from bar_hours import filter_events_for_bar_hours, is_f1_excluded_event
from config import (
    GEMINI_API_KEY,
    RADAR_MIN_WATCHABILITY,
    RADAR_WEEKLY_MAX,
    RADAR_WEEKLY_TARGET_MIN,
    SPORTS_API_KEY,
)
from gemini_client import effective_gemini_model, generate_radar_content_sync, log_gemini_error

from event_verifier import (
    bar_event_blob,
    clear_fetch_cache,
    emoji_for_event,
    event_from_search_candidate,
    gastrobar_hard_reject,
    sort_key_verified,
    verify_event,
    _parse_time_flexible,
)

log = logging.getLogger(__name__)

# Совместимость: старый лимит 6 → теперь до RADAR_WEEKLY_MAX (по умолчанию 15)
RADAR_MAX_ITEMS = RADAR_WEEKLY_MAX
# Сколько кандидатов запросить у Gemini до verify.
RADAR_PER_SEARCH_MAX = 12
RADAR_TIER_LOW = 20

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?$")

# Название события не должно быть «только турнир / шоу без конкретики»
_ABSTRACT_TITLES = frozenset(
    x.lower()
    for x in (
        "nba playoffs",
        "nhl stanley cup playoffs",
        "stanley cup playoffs",
        "stanley cup",
        "ufc fight night",
        "formula 1 grand prix",
        "formula 1",
        "eurovision final",
        "eurovision",
        "eurovision song contest",
        "wwe raw",
        "wwe smackdown",
        "game release",
        "playstation showcase",
    )
)

_WD_RU = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")


def _today_iso() -> str:
    return date.today().isoformat()


def _week_range_human() -> str:
    t0 = date.today()
    t1 = t0 + timedelta(days=7)
    return f"{t0.isoformat()} — {t1.isoformat()}"


def _is_eurovision_event(e: dict[str, Any]) -> bool:
    return "eurovision" in bar_event_blob(e)


def _bar_priority_heuristic(e: dict[str, Any]) -> int:
    """
    1 = приоритет для бара (UCL, UFC, F1, Eurovision, плей-офф NBA/NHL, топ-бокс, мейджор-кибер).
    2 = допустимо (WWE/AEW крупное, большие концерты/стримы, шоукейсы, премии, вирусные эфиры).
    0 = не показывать.
    """
    if gastrobar_hard_reject(e):
        return 0
    b = bar_event_blob(e)

    if re.search(r"\bucl\b|champions\s+league|uefa\s+champions", b):
        return 1
    if "eurovision" in b:
        return 1
    if re.search(r"\bufc\b|\bone\s+championship\b", b):
        return 1
    if re.search(r"formula\s*1|\bf1\b|grand\s*prix", b):
        return 1
    if "nba" in b and re.search(r"playoff|finals|\bfinal\b", b):
        return 1
    if "stanley" in b or ("nhl" in b and "playoff" in b):
        return 1
    if "boxing" in b or "heavyweight" in b or "title fight" in b or "pay-per-view" in b or " ppv " in b:
        return 1
    if any(
        x in b
        for x in (
            "the international",
            "dota 2",
            "cs2 major",
            "blast premier",
            "iem ",
            "valorant champions",
            "lol worlds",
            "league of legends world",
            "capcom cup",
            "evo championship",
        )
    ):
        return 1
    if any(
        x in b
        for x in (
            "premier league",
            "la liga",
            "laliga",
            "bundesliga",
            "serie a",
            "ligue 1",
        )
    ) and re.search(r"\bvs\.?\b|\s—\s|\s-\s", b):
        return 1

    if re.search(r"\bwwe\b|\baew\b", b) and re.search(
        r"wrestlemania|royal rumble|summerslam|survivor series|double or nothing|all out|full gear",
        b,
    ):
        return 2
    if re.search(r"\bwwe\b|\baew\b", b) and ("pay-per-view" in b or " ppv " in b):
        return 2
    if "concert" in b or "coachella" in b or "taylor swift" in b or "beyonc" in b:
        return 2
    if "livestream" in b or "live stream" in b:
        if any(
            x in b
            for x in ("stadium", "arena", "youtube", "twitch", "tickets", "pay-per-view")
        ):
            return 2
    if any(
        x in b
        for x in (
            "the game awards",
            "game awards",
            "summer game fest",
            "gamescom",
            "nintendo direct",
            "playstation showcase",
            "xbox showcase",
        )
    ):
        return 2
    if any(
        x in b
        for x in (
            "academy award",
            "oscar",
            "grammy award",
            "golden globe",
            "emmy award",
        )
    ):
        return 2
    if "viral" in b or "pop_cult" in b or "pop cult" in b:
        return 2
    if "release" in b and any(
        g in b for g in ("gta ", "call of duty", "elden ring", "nintendo", "playstation 6", "ps6")
    ):
        return 2

    return 0


def _enrich_ufc_for_afisha(e: dict[str, Any]) -> dict[str, Any]:
    """Main Card: без точного времени главного боя, только старт карда."""
    b = bar_event_blob(e)
    if not re.search(r"\bufc\b|\bone\s+championship\b", b):
        return e
    title = str(e.get("title", "")).strip()
    has_bout = bool(re.search(r"\bvs\.?\b|\s—\s|\s-\s", title, re.I))
    is_main = "main card" in b or "main event" in b

    if not (is_main or has_bout):
        return e

    e["subtitle"] = "Main Card"
    e["league"] = "Main Card"
    if has_bout:
        e["ufc_main_note"] = "Главный бой ориентировочно позже"
    if str(e.get("time_precision", "")) != "unknown":
        e["time_precision"] = "estimated"
    td = str(e.get("display_time") or e.get("time_display") or e.get("time", "")).strip()
    if td and td != "время уточняется" and not td.startswith("≈"):
        e["time_display"] = f"≈{td}"
        if e.get("display_time"):
            e["display_time"] = td
    return e


def _bar_tier(e: dict[str, Any]) -> int:
    """Меньше = выше приоритет в афише. 99 = не показывать."""
    if gastrobar_hard_reject(e) or is_f1_excluded_event(e):
        return 99
    b = bar_event_blob(e)

    if "eurovision" in b:
        if re.search(r"grand\s+final", b) or (
            re.search(r"\bfinal\b", b) and "semi" not in b
        ):
            return 0
        if re.search(r"semi", b):
            return 1
        return 2

    if re.search(r"\bufc\b", b):
        if "main card" in b or "main event" in b:
            return 3
        if re.search(r"\bvs\.?\b|\s—\s", str(e.get("title", "")), re.I):
            return 3
        return 28

    if "nba" in b and re.search(r"conference\s+final", b):
        return 4
    if "nba" in b and re.search(r"playoff|finals", b):
        return 5

    if re.search(r"stanley|nhl", b) and re.search(r"conference\s+final", b):
        return 6
    if "stanley" in b or ("nhl" in b and re.search(r"playoff", b)):
        return 7

    if re.search(r"formula\s*1|\bf1\b", b):
        return 8

    if re.search(r"\bucl\b|champions\s+league|uefa\s+champions", b):
        return 9
    if re.search(r"europa\s+league|\buel\b|\buecl\b|conference\s+league", b):
        return 10

    legacy = _bar_priority_heuristic(e)
    if legacy == 1:
        return 12
    if legacy == 2:
        return 25
    return 99


def _bar_priority(e: dict[str, Any]) -> int:
    tier = _bar_tier(e)
    if tier >= 99:
        return 0
    if tier < RADAR_TIER_LOW:
        return 1
    return 2


def _radar_schema_instructions(max_n: int) -> str:
    today = _today_iso()
    week = _week_range_human()
    return f"""
Use Google Search. Return ONLY a JSON array (max {max_n} objects), no markdown, no prose outside JSON.
Each item MUST include:
- date (YYYY-MM-DD) — original local calendar date in source_timezone
- time (24h HH:MM) — original local wall-clock time in source_timezone (NOT Vietnam, NOT ICT)
- source_timezone — IANA only (e.g. Europe/Zurich, America/New_York, America/Toronto)
- category, title, optional subtitle, why_it_matters

PARTICIPANT RULES (strict):
- Sports matches / finals: title MUST name both sides (e.g. "Real Madrid — Barcelona", "Spurs vs Thunder"). Never "Between top clubs" or vague descriptions in title/subtitle.
- UFC / boxing: both fighters in title OR "UFC Fight Night: Name vs Name".
- F1: session type in title (Qualifying, Sprint, Race) — not Practice.
- Eurovision: Semi-final or Grand Final in title.
- Esports: tournament + stage (e.g. "IEM Atlanta 2026 — Grand Final"), not "Final Day Events".

CRITICAL TIME RULES:
- Do NOT convert to Asia/Ho_Chi_Minh or Vietnam time yourself.
- Do NOT output weekday — Python will compute it after conversion.
- Do NOT use "already converted" or ICT times as source.
- If official source_timezone is unknown, set source_timezone to "" (empty string) — do not guess UTC.
- For Eurovision use Europe/Zurich (or host city IANA). For UFC US cards use America/New_York or America/Los_Angeles.
- For F1 use the circuit's local IANA zone (e.g. America/Toronto for Canada GP).
- For Premier League / EPL use Europe/London. For UCL/UEL in UK use Europe/London, else Europe/Paris.
- For NBA use America/New_York unless West Coast teams (America/Los_Angeles).

Every row MUST have credible date AND time (HH:MM) from an official listing — Python discards rows without confirmed time.
Do NOT return summary rows like "Premier League Final Day" without specific matches — one row per concrete match/session.

BAR FILTER — NEVER include:
- Chicago Med / Chicago Fire / Chicago P.D. / One Chicago (any spelling: PD, P.D., etc.)
- US network procedural season/series finales (NBC/CBS/ABC/Fox/CW), ordinary episodic TV finales
- Formula 1 Practice / Free Practice / FP1 / FP2 / FP3 (ONLY Qualifying, Sprint, Race, Grand Prix)
- UFC prelims without Main Card — prefer one row per card: title + "Main Card", no exact main-fight time
- anything not suitable for a crowded bar TV night

Today: {today}
Week window: {week}
"""


def _radar_combined_prompt(max_n: int) -> str:
    year = date.today().year
    schema = _radar_schema_instructions(max_n)
    return f"""You are an AI Event Editor for Gastrobar (Nha Trang bar TVs).

Goal: what people will ACTUALLY want to watch at the bar this week — NOT a list of only grand finals.

Use Google Search. Return ONLY one JSON array (max {max_n} objects), no markdown.

INCLUDE when credible (with exact teams/fighters and times):
- Football: UCL, Europa League, Premier League / La Liga / Serie A / Bundesliga TOP matches, derbies, rivalries, important regular season games (e.g. Arsenal vs Liverpool, El Clásico), playoffs AND finals
- NBA: playoffs, conference finals, finals, rivalry games, important regular season games between top teams
- NHL: same logic as NBA
- Formula 1: ALL weekend sessions (Sprint Qualifying, Sprint, Qualifying, Race) — each as separate row
- UFC: Main Card / title fights / Fight Night with named main bouts only
- Eurovision {year}: semi-finals and Grand Final
- Esports: CS2/Valorant/Dota/LoL major finals or grand finals — NOT random qualifiers or "event day"
- Major live entertainment / trending broadcasts

SKIP: weak regular-season games, vague listings, F1 practice, UFC prelims-only, procedural TV finales.

{schema}
"""


def _is_gemini_free_quota_error(exc: BaseException) -> bool:
    t = str(exc)
    tl = t.lower()
    return (
        "429" in t
        or "RESOURCE_EXHAUSTED" in t
        or "Too Many Requests" in t
        or "generate_content_free_tier" in tl
        or ("quota" in tl and "exceed" in tl)
    )


def _radar_prompts(max_n: int) -> list[tuple[str, str]]:
    """Пара (лог-метка, полный промпт) — отдельные Gemini Search по темам."""
    year = date.today().year
    common = _radar_schema_instructions(max_n)

    return [
        (
            "radar_sports_utc",
            f"""Search focus: bar-watchable sports this week — football top leagues, derbies, NBA/NHL (playoffs AND top-team regular season), not only finals.
Suggested queries: "Premier League this week big matches", "La Liga El Clasico date time", "NBA national TV games this week".

Include named team vs team matchups. Skip anonymous cup finals without confirmed teams.
{common}
""",
        ),
        (
            "radar_ufc",
            f"""Search focus: UFC this week — numbered events / fight nights with confirmed main card start.
Suggested queries: "UFC this week main card time", "UFC official start time timezone".

Only concrete cards with named fights or official card start. No vague "UFC Fight Night" without card/time.
{common}
""",
        ),
        (
            "radar_f1",
            f"""Search focus: Formula 1 this week — race, sprint, qualifying sessions with official schedule.
Suggested queries: "Formula 1 this week race qualifying time", "F1 official schedule timezone".

Concrete session names (Qualifying, Sprint, Race) with times.
{common}
""",
        ),
        (
            "radar_eurovision",
            f"""Search focus: Eurovision Song Contest {year} — semi-finals and grand final ONLY with official broadcast times.

Run this exact-style Google query (adapt year if needed): "Eurovision Song Contest {max(year, 2026)} schedule final semi final time UTC"

Also try: "Eurovision this week final time UTC"

If any show occurs this week: MUST return it with exact date, time, and source_timezone (UTC or listed host city IANA).
Title example: "Eurovision Song Contest {year} Grand Final" or Semi-Final 1/2.
subtitle example: "Music / Live show"
{common}
""",
        ),
        (
            "radar_esports",
            f"""Search focus: major esports finals / tier-1 tournament stages this week (Worlds, Majors, International, BLAST finals, Valorant Champions, etc.).
Suggested query: "major esports events this week final schedule"

NOT random regional leagues. International mass audience only.
{common}
""",
        ),
        (
            "radar_concerts_streams",
            f"""Search focus: major concerts, arena/stadium live shows, and large official livestreams this week.
Suggested query: "major concerts livestream this week"

NOT small club gigs. NOT regular TV. Eurovision is handled elsewhere — skip duplicate here unless a different mega-livestream.
{common}
""",
        ),
        (
            "radar_week_broad_backup",
            f"""Search focus: BACKUP — any major bar-TV events in the week window (sports playoffs, UFC, F1, big football, esports finals, Eurovision if in window).
Return up to {max_n} DISTINCT items other shards might have missed. Same JSON schema as other shards.
{common}
""",
        ),
    ]


def _extract_json_array(text: str) -> list[Any]:
    t = text.strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t)
        if m:
            t = m.group(1).strip()

    def _parse(s: str) -> Any:
        return json.loads(s)

    try:
        data = _parse(t)
    except json.JSONDecodeError:
        i0, i1 = t.find("["), t.rfind("]")
        if i0 == -1 or i1 <= i0:
            raise
        data = _parse(t[i0 : i1 + 1])

    if isinstance(data, dict):
        for key in ("events", "items", "radar", "schedule"):
            if key in data and isinstance(data[key], list):
                data = data[key]
                break
    if not isinstance(data, list):
        raise ValueError("ожидался JSON-массив")
    return data


def _normalize_hhmm(t: str) -> str | None:
    t = str(t).strip().removeprefix("≈").strip()
    t = re.sub(
        r"\s*(UTC|GMT|CET|CEST|EST|EDT|PST|PDT|BST|IST|MSK)\s*$",
        "",
        t,
        flags=re.I,
    ).strip()
    m = _TIME_RE.match(t)
    if not m:
        return None
    h, mi = int(m.group(1)), m.group(2)
    return f"{h:02d}:{mi}"


def _weekday_ru_for_date(d: date) -> str:
    return _WD_RU[d.weekday()]


def _emoji_for_category(cat: str) -> str:
    c = (cat or "").upper()
    if "AWARD" in c or "GRAMMY" in c or "OSCAR" in c or "EMMY" in c or "GOLDEN GLOBE" in c:
        return "🏆"
    if "STREAM" in c or "TWITCH" in c or "LIVESTREAM" in c or "YOUTUBE LIVE" in c:
        return "📡"
    if "VIRAL" in c or "POP_CULT" in c or "POP CULT" in c or "TREND" in c:
        return "🔥"
    if "TV_FINAL" in c or "FINALE" in c or "NETFLIX" in c or "HBO" in c or "DISNEY+" in c:
        return "📺"
    if "NBA" in c or "BASKET" in c:
        return "🏀"
    if "NHL" in c or "HOCKEY" in c or "STANLEY" in c:
        return "🏒"
    if "UFC" in c or "MMA" in c:
        return "🥊"
    if "F1" in c or "FORMULA" in c:
        return "🏎"
    if "FOOT" in c or "SOCCER" in c or "CHAMPIONS" in c or "UEFA" in c or "LIGA" in c:
        return "⚽"
    if "ESPORT" in c or "CS2" in c or "DOTA" in c or "LOL" in c or "VALORANT" in c:
        return "🎮"
    if "CONCERT" in c or "SONG" in c or "MUSIC" in c or "EUROVISION" in c:
        return "🎤"
    if "SHOW" in c or "WWE" in c:
        return "📺"
    if "GAME" in c or "GAMING" in c or "GTA" in c or "PLAYSTATION" in c or "XBOX" in c or "NINTENDO" in c:
        return "🕹"
    return "🏟"


def _validate_concrete_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        log.info("local_validation_removed: not_a_dict")
        return None

    date_s = str(raw.get("date", "")).strip()
    if not _DATE_RE.match(date_s):
        log.info("local_validation_removed: bad_date raw=%s", raw)
        return None

    try:
        d_obj = date.fromisoformat(date_s)
    except ValueError:
        log.info("local_validation_removed: invalid_date raw=%s", raw)
        return None

    time_raw = raw.get("time")
    time_s: str | None = None
    time_approx = False
    if time_raw is not None and str(time_raw).strip():
        time_s, time_approx = _parse_time_flexible(str(time_raw))
        if not time_s:
            log.info(
                "local_validation: unparsed_time title=%s time=%r — пропустим в verify",
                raw.get("title"),
                time_raw,
            )
            time_s = str(time_raw).strip()
            time_approx = True
    else:
        log.info(
            "local_validation: no_time title=%s — пропустим в verify (время уточняется)",
            raw.get("title"),
        )
        time_s = ""

    title = str(raw.get("title", "")).strip()
    if not title or len(title) < 3:
        log.info("local_validation_removed: no_title raw=%s", raw)
        return None

    subtitle = str(raw.get("subtitle", raw.get("league", ""))).strip()
    source_timezone = str(
        raw.get("source_timezone") or raw.get("original_timezone") or ""
    ).strip()

    tl = title.lower()
    if tl in _ABSTRACT_TITLES:
        log.info("skipped_abstract_title: %s", title)
        return None
    if subtitle and tl == subtitle.lower():
        log.info("skipped_abstract_title equals subtitle: %s", title)
        return None

    category = str(raw.get("category", "EVENT")).strip()[:48] or "EVENT"
    why = (
        str(raw.get("why_it_matters", "")).strip()
        or str(raw.get("why", "")).strip()
        or str(raw.get("why_people_care", "")).strip()
    )
    cand_pre = {
        "title": title,
        "category": category,
        "subtitle": subtitle,
        "league": subtitle,
        "why": why,
    }
    if gastrobar_hard_reject(cand_pre):
        return None

    from event_participants import passes_participant_rules

    ok_part, part_reason = passes_participant_rules(cand_pre)
    if not ok_part:
        log.info(
            "local_validation_removed: participants title=%s reason=%s",
            title,
            part_reason,
        )
        return None

    if is_f1_excluded_event(cand_pre):
        log.info("local_validation_removed: f1_practice title=%s", title)
        return None

    out = {
        "date": date_s,
        "time": time_s or "",
        "weekday": "",
        "category": category,
        "title": title,
        "subtitle": subtitle,
        "league": subtitle,
        "why": why,
        "source_timezone": source_timezone,
        "original_date": date_s,
        "original_time": time_s or "",
        "original_timezone": source_timezone,
    }
    out["emoji"] = emoji_for_event(out)
    if time_approx:
        out["time_precision"] = "estimated"
    return out


def _event_dedupe_key(e: dict[str, Any]) -> tuple[str, str, str]:
    t = re.sub(r"\s+", " ", (e.get("title") or "").lower().strip())
    return (str(e.get("date", "")), str(e.get("time", "")), t)


def _gemini_fetch_validated_sync(
    prompt: str,
    *,
    log_label: str,
    max_attempts: int = 3,
    use_search: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY обязателен для Event Radar")

    mode = "search" if use_search else "plain"
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            text = generate_radar_content_sync(prompt, use_search=use_search)
            raw_list = _extract_json_array(text)
            raw_total = len(raw_list)

            validated: list[dict[str, Any]] = []
            for raw in raw_list:
                if not isinstance(raw, dict):
                    continue
                v = _validate_concrete_event(raw)
                if v:
                    validated.append(v)

            log.info(
                "%s (%s): raw=%s pre_verify=%s sample=%s (attempt %s) model=%s",
                log_label,
                mode,
                raw_total,
                len(validated),
                [(x.get("date"), x.get("time"), x.get("title")) for x in validated[:3]],
                attempt,
                effective_gemini_model(),
            )
            if raw_total > 0 and not validated and attempt < max_attempts:
                log.warning(
                    "%s: %s raw rows failed local validation, retrying",
                    log_label,
                    raw_total,
                )
                time.sleep(1.8 * attempt)
                continue
            return validated, raw_total
        except Exception as e:
            if _is_gemini_free_quota_error(e):
                raise
            if use_search:
                log.error("Gemini Search error", exc_info=True)
                log_gemini_error(f"{log_label}_search", e)
            else:
                log_gemini_error(f"{log_label}_plain", e)
            last_err = e
            log.warning(
                "Event Radar shard %s (%s) attempt %s/%s failed: %s",
                log_label,
                mode,
                attempt,
                max_attempts,
                e,
            )
            if attempt < max_attempts:
                time.sleep(1.8 * attempt)
    if last_err:
        raise last_err
    raise RuntimeError("Event Radar shard exhausted retries")


def _gemini_fetch_with_search_fallback(
    prompt: str,
    *,
    log_label: str,
    max_attempts: int = 2,
) -> tuple[list[dict[str, Any]], int, str | None]:
    """
    Сначала Google Search; при ошибке (не 429) — plain Gemini без Search.
    Возвращает (кандидаты, raw_count, fetch_note).
    """
    try:
        prelim, raw = _gemini_fetch_validated_sync(
            prompt,
            log_label=log_label,
            max_attempts=max_attempts,
            use_search=True,
        )
        return prelim, raw, None
    except Exception as e:
        if _is_gemini_free_quota_error(e):
            raise
        log.error("Gemini Search error", exc_info=True)
        log_gemini_error(f"{log_label}_search_fallback", e)
        log.warning(
            "%s: Google Search failed (%s), fallback to plain Gemini",
            log_label,
            type(e).__name__,
        )
    try:
        prelim, raw = _gemini_fetch_validated_sync(
            prompt,
            log_label=f"{log_label}_plain",
            max_attempts=1,
            use_search=False,
        )
        return prelim, raw, "search_fallback"
    except Exception as e:
        if _is_gemini_free_quota_error(e):
            raise
        log_gemini_error(f"{log_label}_plain_failed", e)
        raise


def _dedupe_radar_candidates(merged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for e in merged:
        k = _event_dedupe_key(e)
        if k in seen:
            continue
        seen.add(k)
        deduped.append(e)
    return deduped


def _fetch_radar_multi_search_sharded() -> tuple[list[dict[str, Any]], int, str | None]:
    prompts = _radar_prompts(RADAR_PER_SEARCH_MAX)
    total_raw = 0
    merged: list[dict[str, Any]] = []
    shard_failures = 0
    fetch_note: str | None = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(
                _gemini_fetch_with_search_fallback,
                prompt,
                log_label=label,
                max_attempts=1,
            ): label
            for label, prompt in prompts
        }
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                prelim, raw, note = fut.result()
            except Exception as e:
                if _is_gemini_free_quota_error(e):
                    log.error("Event Radar shard %s: Gemini quota: %s", label, e)
                    return [], 0, "gemini_quota"
                log.error("Gemini Search error", exc_info=True)
                log_gemini_error(f"shard_{label}", e)
                shard_failures += 1
                continue
            if note == "search_fallback":
                fetch_note = note
            total_raw += raw
            merged.extend(prelim)

    deduped = _dedupe_radar_candidates(merged)
    log.info(
        "Event Radar multi-search (sharded): shards=%s merged_pre=%s raw_sum=%s failures=%s",
        len(prompts),
        len(deduped),
        total_raw,
        shard_failures,
    )
    if not deduped and total_raw == 0 and shard_failures >= len(prompts):
        return deduped, total_raw, "gemini_error"
    return deduped, total_raw, fetch_note


def _fetch_radar_combined_once() -> tuple[list[dict[str, Any]], int, str | None]:
    max_n = min(max(RADAR_PER_SEARCH_MAX * 3, 20), 28)
    try:
        prelim, raw_total, fetch_note = _gemini_fetch_with_search_fallback(
            _radar_combined_prompt(max_n),
            log_label="radar_combined",
            max_attempts=2,
        )
    except Exception as e:
        if _is_gemini_free_quota_error(e):
            log.error("Event Radar combined: Gemini quota exhausted: %s", e)
            return [], 0, "gemini_quota"
        log.error("Gemini Search error", exc_info=True)
        log_gemini_error("radar_combined_fatal", e)
        return [], 0, "gemini_error"

    deduped = _dedupe_radar_candidates(prelim)
    log.info(
        "Event Radar combined: merged_pre=%s raw_sum=%s fetch_note=%s model=%s",
        len(deduped),
        raw_total,
        fetch_note,
        effective_gemini_model(),
    )
    return deduped, raw_total, fetch_note


def fetch_radar_multi_search_sync() -> tuple[list[dict[str, Any]], int, str | None]:
    """
    Кандидаты после локальной валидации, сумма длин сырых JSON-массивов, код пустого ответа.

    По умолчанию один combined-запрос (1 счётчик generateContent на free tier).
    Несколько шардов — RADAR_MULTI_SHARD=1 (много запросов, только при платном лимите).
    """
    flag = os.getenv("RADAR_MULTI_SHARD", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        log.info("Event Radar: RADAR_MULTI_SHARD enabled — multiple Gemini calls")
        return _fetch_radar_multi_search_sharded()
    return _fetch_radar_combined_once()


def _confidence_sort_key(e: dict[str, Any]) -> tuple[int, str, str]:
    rank = 0 if str(e.get("confidence", "medium")).lower() == "high" else 1
    sk = sort_key_verified(e)
    return (rank, sk[0], sk[1])


def _tier_sort_key(e: dict[str, Any]) -> tuple[int, int, str, str]:
    tier = int(e.get("radar_tier", 99))
    return (tier, *_confidence_sort_key(e))


def _prepare_for_afisha_selection(e: dict[str, Any]) -> dict[str, Any]:
    from watchability import enrich_watchability

    e = _enrich_ufc_for_afisha(e)
    tier = _bar_tier(e)
    e["radar_tier"] = tier
    if tier < RADAR_TIER_LOW:
        e["radar_priority"] = 1
    elif tier < 99:
        e["radar_priority"] = 2
    else:
        e["radar_priority"] = 0
    if _is_eurovision_event(e) and not str(e.get("subtitle", "")).strip():
        e["subtitle"] = "Music / Live show"
        e["league"] = e["subtitle"]
    e["emoji"] = emoji_for_event(e)
    return enrich_watchability(e)


def _watchability_sort_key(e: dict[str, Any]) -> tuple[int, int, int, str, str]:
    return (
        int(e.get("gastrobar_priority", 99)),
        -int(e.get("watchability_score", 0)),
        int(e.get("radar_tier", 99)),
        *_confidence_sort_key(e)[1:],
    )


def _select_weekly_radar_events(verified: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Weekly афиша: watchability + major events (medium OK).
    TV slot cap НЕ применяется — параллельные матчи группируются при отображении (EPL matchday).
    """
    verified = [
        e
        for e in verified
        if str(e.get("confidence", "medium")).lower() in ("high", "medium")
    ]
    from event_participants import filter_events_by_participants
    from watchability import is_major_weekly_event, min_watchability_for_event

    prepared = [_prepare_for_afisha_selection(dict(e)) for e in verified]
    prepared = filter_events_by_participants(prepared, log_prefix="weekly_select")

    from event_lock import has_confirmed_vn_time

    prepared = [e for e in prepared if has_confirmed_vn_time(e)]
    log.info("weekly events found after prepare+time: %s", len(prepared))

    eligible: list[dict[str, Any]] = []
    for e in prepared:
        floor = min_watchability_for_event(e, default_min=RADAR_MIN_WATCHABILITY)
        score = int(e.get("watchability_score", 0))
        if score >= floor:
            eligible.append(e)
        elif is_major_weekly_event(e) and score >= max(32, floor - 6):
            log.info(
                "weekly major event below floor but kept: title=%r score=%s floor=%s",
                e.get("title"),
                score,
                floor,
            )
            eligible.append(e)
        else:
            log.info(
                "weekly skipped event: title=%r reason=low_watchability score=%s floor=%s major=%s",
                e.get("title"),
                score,
                floor,
                is_major_weekly_event(e),
            )

    eligible.sort(key=_watchability_sort_key)

    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for e in eligible:
        k = _event_dedupe_key(e)
        if k in seen:
            continue
        seen.add(k)
        deduped.append(e)

    # Weekly: no 2-TV slot pruning — show full shortlist (grouping handles EPL blocks).
    out = deduped[:RADAR_WEEKLY_MAX]

    if len(out) < RADAR_WEEKLY_TARGET_MIN:
        in_out = {id(x) for x in out}
        for e in sorted(prepared, key=_watchability_sort_key):
            if id(e) in in_out:
                continue
            if not is_major_weekly_event(e):
                continue
            out.append(e)
            in_out.add(id(e))
            log.info(
                "weekly backfill major: title=%r watchability=%s",
                e.get("title"),
                e.get("watchability_score"),
            )
            if len(out) >= RADAR_WEEKLY_TARGET_MIN:
                break

    for e in out:
        log.info(
            "weekly selected event: title=%r watchability=%s confidence=%s "
            "gastrobar_priority=%s type=%s major=%s",
            e.get("title"),
            e.get("watchability_score"),
            e.get("confidence"),
            e.get("gastrobar_priority"),
            e.get("editorial_type"),
            is_major_weekly_event(e),
        )

    for e in deduped[RADAR_WEEKLY_MAX:]:
        log.info(
            "weekly skipped event: title=%r reason=weekly_cap watchability=%s",
            e.get("title"),
            e.get("watchability_score"),
        )

    out.sort(key=sort_key_verified)
    log.info(
        "weekly radar selection: prepared=%s eligible=%s selected=%s target_min=%s",
        len(prepared),
        len(eligible),
        len(out),
        RADAR_WEEKLY_TARGET_MIN,
    )
    return out


def _select_final_radar_events(verified: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Алиас для совместимости."""
    return _select_weekly_radar_events(verified)


def _program_item_to_radar_event(item: dict[str, Any]) -> dict[str, Any] | None:
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
    subtitle = str(item.get("league_label_ru", item.get("league_raw", ""))).strip()
    tier = str(item.get("tier", "high")).lower()
    return {
        "date": date_s,
        "time": time_s,
        "weekday": _weekday_ru_for_date(d_obj),
        "category": "FOOTBALL",
        "title": title,
        "subtitle": subtitle,
        "league": subtitle,
        "why": "Подборка API-SPORTS (резерв без Gemini)",
        "emoji": "⚽",
        "source_timezone": "UTC",
        "verified_via": "API-SPORTS",
        "confidence": "high",
        "radar_priority": 1 if tier == "high" else 2,
    }


async def _fallback_events_from_sports_api() -> tuple[list[dict[str, Any]], int]:
    """Резервная подборка из API-SPORTS, когда Gemini недоступен (429 / квота)."""
    if not SPORTS_API_KEY:
        return [], 0
    from sports_events import get_week_events_with_stats

    program, raw_total, _ = await get_week_events_with_stats()
    prelim: list[dict[str, Any]] = []
    for item in program:
        ev = _program_item_to_radar_event(item)
        if ev and not gastrobar_hard_reject(ev):
            prelim.append(ev)
    if not prelim:
        return [], raw_total
    final = _select_final_radar_events(prelim)
    log.info(
        "Event Radar sports fallback: raw_api=%s pre=%s final=%s",
        raw_total,
        len(prelim),
        len(final),
    )
    return final, raw_total


async def _fetch_radar_pipeline() -> tuple[
    list[dict[str, Any]], int, list[dict[str, Any]], str | None
]:
    """
    Пул после verify + bar hours + tier (до финального отбора week/now24).
    (pool, raw_total, prelim_raw, fetch_note)
    """
    clear_fetch_cache()
    prelim, raw_total, fetch_note = await asyncio.to_thread(fetch_radar_multi_search_sync)

    if fetch_note == "gemini_quota":
        fallback, fb_raw = await _fallback_events_from_sports_api()
        if fallback:
            for e in fallback:
                _prepare_for_afisha_selection(e)
            return fallback, fb_raw, [], "sports_fallback"

    results = await asyncio.gather(*[verify_event(e) for e in prelim])
    from locked_time import has_locked_schedule, lock_event_schedule

    verified_all: list[dict[str, Any]] = []
    verify_dropped = 0
    for cand, r in zip(prelim, results):
        if r and str(r.get("confidence", "medium")).lower() in ("high", "medium"):
            if not has_locked_schedule(r):
                r = lock_event_schedule(r, phase="weekly_pipeline") or r
            verified_all.append(r)
        else:
            verify_dropped += 1
            if r:
                log.info(
                    "verify_skipped_low_confidence: title=%r confidence=%s",
                    r.get("title"),
                    r.get("confidence"),
                )
            else:
                log.info(
                    "verify_removed_in_pipeline: title=%r date=%s time=%s",
                    cand.get("title"),
                    cand.get("date"),
                    cand.get("time"),
                )

    if not verified_all and prelim:
        log.warning(
            "Event Radar: verify dropped all %s candidates; no unverified pass-through",
            len(prelim),
        )

    from event_lock import has_confirmed_vn_time

    verified_all = filter_events_for_bar_hours(verified_all)
    verified_all = [v for v in verified_all if has_confirmed_vn_time(v)]
    for v in verified_all:
        _prepare_for_afisha_selection(v)

    verified_prio = [v for v in verified_all if int(v.get("radar_tier", 99)) < RADAR_TIER_LOW]
    if verified_prio:
        pool = verified_prio
    else:
        pool = [v for v in verified_all if not gastrobar_hard_reject(v)]
        if pool:
            log.warning(
                "Event Radar: no P1/P2; using %s rows (bar filter only)",
                len(pool),
            )
            for v in pool:
                if v.get("radar_priority", 0) < 1:
                    v["radar_priority"] = 2

    log.info(
        "Event Radar pipeline: raw=%s pre=%s kept=%s dropped=%s pool=%s",
        raw_total,
        len(prelim),
        len(verified_all),
        verify_dropped,
        len(pool),
    )
    return pool, raw_total, prelim, fetch_note


def _finalize_week_selection(pool: list[dict[str, Any]], prelim: list[dict[str, Any]]) -> list[dict[str, Any]]:
    final = _select_final_radar_events(pool)
    final = [e for e in final if not gastrobar_hard_reject(e)]
    if prelim and not final:
        log.warning(
            "Event Radar week: no confirmed events after selection (no last-chance rewrite)"
        )
    return final


async def get_event_radar_week() -> tuple[list[dict[str, Any]], int, int, int, str | None]:
    """Афиша недели: editorial подбор до RADAR_WEEKLY_MAX событий."""
    pool, raw_total, prelim, fetch_note = await _fetch_radar_pipeline()
    if fetch_note == "sports_fallback":
        final = pool[:RADAR_MAX_ITEMS]
    else:
        final = _finalize_week_selection(pool, prelim)
    log.info("Event Radar week final=%s", len(final))
    if final:
        from weekly_events_cache import save_weekly_events_cache

        await save_weekly_events_cache(final, source="weekly_radar")
    return final, raw_total, len(prelim), len(final), fetch_note


async def get_event_radar_now24() -> tuple[list[dict[str, Any]], int, int, int, str | None]:
    """События ближайших 24 часов (без добивания слабым хвостом)."""
    from daily_event import select_now24_events

    pool, raw_total, prelim, fetch_note = await _fetch_radar_pipeline()
    final = select_now24_events(pool)
    log.info("Event Radar now24 final=%s", len(final))
    return final, raw_total, len(prelim), len(final), fetch_note


def format_radar_afisha(
    events: list[dict[str, Any]],
    *,
    section_title: str = "🔥 НА ЭТОЙ НЕДЕЛЕ В GASTROBAR",
    apply_grouping: bool = True,
) -> str:
    """Weekly афиша: Python formatter only (locked events)."""
    from event_lock import format_locked_weekly_afisha, lock_events_for_formatter

    locked = lock_events_for_formatter(events, log_prefix="weekly_afisha")
    log.info("FORMATTER RECEIVED EVENTS: count=%s", len(locked))
    return format_locked_weekly_afisha(locked, section_title=section_title)


def format_radar_week_message(events: list[dict[str, Any]]) -> str:
    body = format_radar_afisha(
        events,
        section_title="🔥 НА ЭТОЙ НЕДЕЛЕ В GASTROBAR",
    )
    return f"🔭 Event Radar · Week\n\n{body}"


def format_radar_now24_message(events: list[dict[str, Any]]) -> str:
    body = format_radar_afisha(
        events,
        section_title="⚡ СОБЫТИЯ В БЛИЖАЙШИЕ 24 ЧАСА",
    )
    return f"⚡ Event Radar · Next 24h\n\n{body}"


def radar_fetch_header(fetch_note: str | None) -> str:
    if fetch_note == "search_fallback":
        return "🔭 Event Radar · Gemini (fallback)\nGoogle Search недоступен."
    if fetch_note == "sports_fallback":
        return "🔭 Event Radar · API-SPORTS (резерв)\nЛимит Gemini исчерпан."
    return ""


def radar_events_to_db_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Совместимость с replace_week_events."""
    rows: list[dict[str, Any]] = []
    for e in events:
        rows.append(
            {
                "sport": e.get("category", "RADAR"),
                "title": e.get("title", ""),
                "league": e.get("subtitle", e.get("league", "")),
                "date": e.get("date", _today_iso()),
                "time": e.get("time", ""),
                "importance": "high",
                "reason": " ".join(
                    x
                    for x in (
                        str(e.get("why", "")).strip(),
                        str(e.get("verified_via", "")).strip(),
                    )
                    if x
                ),
            }
        )
    return rows


def format_radar_scheduler_summary(events: list[dict[str, Any]]) -> str:
    if not events:
        return "Подборка Event Radar пуста."
    lines: list[str] = []
    for e in events:
        sub = e.get("subtitle", e.get("league", ""))
        tm = str(
            e.get("display_time") or e.get("time_display") or e.get("time", "")
        ).strip()
        note = str(e.get("note", "")).strip()
        sub_part = sub
        if note and note.lower() not in str(sub).lower():
            sub_part = f"{sub} — {note}" if sub else note
        lines.append(
            f"{e.get('emoji', '•')} {e.get('weekday', '')} {tm} — "
            f"{e.get('title', '')}" + (f" ({sub_part})" if str(sub_part).strip() else "")
        )
        if e.get("why"):
            lines.append(f"   → {e['why']}")
        lines.append("")
    return "\n".join(lines).strip()


def important_today_tomorrow_radar(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    t0 = date.today()
    dates = {t0.isoformat(), (t0 + timedelta(days=1)).isoformat()}
    out: list[dict[str, Any]] = []
    for e in events:
        sd = str(e.get("date", "")).strip()
        if sd in dates:
            out.append(e)
    return out[:8]


def spotlight_line_radar(events: list[dict[str, Any]]) -> str:
    parts = [
        f"{e.get('weekday', '')} {e.get('display_time') or e.get('time_display') or e.get('time', '')} {e.get('title', '')}"
        for e in events[:4]
    ]
    return "; ".join(p for p in parts if p.strip())
