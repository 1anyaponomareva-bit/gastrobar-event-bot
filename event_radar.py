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
    RADAR_API_FIRST,
    RADAR_API_MIN_SEED,
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
RADAR_PER_SEARCH_MAX = 32
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
    from radar_current_week import today_local

    return today_local().date().isoformat()


def _week_range_human() -> str:
    from radar_current_week import current_week_bounds

    t0, t1 = current_week_bounds()
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
            "europa league",
            "conference league",
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
    if re.search(r"world\s+championship|iihf", b, re.I):
        from event_participants import has_matchup_in_title

        if has_matchup_in_title(str(e.get("title", ""))):
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
LIVE CURRENT WEEK ONLY ({week}, timezone Asia/Ho_Chi_Minh for your planning — output source local times, NOT Vietnam).
Do NOT use training memory, famous past fights, or old UFC numbered cards (e.g. UFC 302, Fury vs Usyk rematch).
Do NOT invent tournament cities (e.g. "IEM Dallas" if official schedule says Atlanta).
Every row MUST be confirmed by a current official listing found via search — if unsure, omit the row.
Gemini is discovery only; Python rejects unverified or out-of-window events.

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

Every row MUST have credible date AND time (HH:MM) from an official listing — cross-check football/basketball/hockey kickoff on official league site, Google Sports, and BetBoom when available; Python discards rows without confirmed time.
Do NOT return summary rows like "Premier League Final Day" without specific matches — one row per concrete match/session.
Return a RICH bar TV schedule (15–22 rows): separate F1 sessions, separate NBA playoff games, separate top football matches — never one grouped digest.

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
    return f"""Find major watchable events for a bar audience in Nha Trang this week (Gastrobar TV guide).

LIVE CURRENT WEEK ONLY ({_week_range_human()}). No historical fights, old UFC cards, or memory-based schedules.
Search official sites; omit anything you cannot confirm for this exact week.

Use Google Search ONCE. Return ONLY one JSON array (aim for {max_n} distinct rows), no markdown.

Search across:
football (Champions League, Europa League, Premier League, La Liga, Serie A, Bundesliga — top matches, derbies, final matchday),
UFC / boxing main cards, Formula 1 (Sprint Qualifying, Sprint, Qualifying, Race — separate rows),
NBA / NHL playoffs and finals, esports majors (CS2, IEM, Dota TI, LoL Worlds, Valorant Champions),
Eurovision {year}, Super Bowl, Oscars, Grammys, Emmys, major award shows, Coachella-scale livestreams,
WWE major events, Apple/PlayStation/Xbox showcases, huge game launches, viral global live broadcasts.

Return only events that are:
* mass-interest and discussable in a bar
* suitable for Gastrobar screens
* exact date + time + source_timezone (IANA) from official listings

Do NOT include:
ordinary TV episodes, Chicago Med/Fire-style finales, local low-interest cups, U21/youth,
vague placeholders ("Final Day Events"), events without kickoff time.

Separate row per match/session (both teams in title). F1: no practice sessions.

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


def _gemini_fetch_note_for_error(exc: BaseException) -> str:
    from gemini_client import is_gemini_transient_error

    if _is_gemini_free_quota_error(exc):
        return "gemini_quota"
    if is_gemini_transient_error(exc):
        return "gemini_overloaded"
    return "gemini_error"


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

    from radar_current_week import discovery_candidate_ok

    if not discovery_candidate_ok(raw):
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
        from radar_recall import log_radar_rejection

        log_radar_rejection("fetch", "missing_datetime", raw)
        return None

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
    blob_cat = f"{title} {subtitle}".lower()
    if category == "EVENT":
        if re.search(r"premier\s+league|\bepl\b|champions\s+league|\bucl\b|la\s+liga", blob_cat):
            category = "FOOTBALL"
        elif re.search(r"\bnba\b", blob_cat):
            category = "BASKETBALL"
        elif re.search(r"\bnhl\b|stanley", blob_cat):
            category = "HOCKEY"
        elif re.search(r"formula\s*1|\bf1\b|grand\s+prix", blob_cat):
            category = "F1"
        elif re.search(r"\bufc\b", blob_cat):
            category = "UFC"
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
    from radar_dedupe import radar_dedupe_key

    return radar_dedupe_key(e)


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
            text = generate_radar_content_sync(
                prompt, use_search=use_search, purpose=log_label
            )
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
                from gemini_client import is_gemini_transient_error

                if is_gemini_transient_error(e):
                    time.sleep(min(30.0, 6.0 * attempt))
                else:
                    time.sleep(1.8 * attempt)
    if last_err:
        raise last_err
    raise RuntimeError("Event Radar shard exhausted retries")


def _gemini_fetch_with_search_fallback(
    prompt: str,
    *,
    log_label: str,
    max_attempts: int = 1,
) -> tuple[list[dict[str, Any]], int, str | None]:
    """
    Один Gemini Search запрос (без второго plain-вызова — экономия free tier).
  """
    allow_plain = os.getenv("RADAR_PLAIN_FALLBACK", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    try:
        prelim, raw = _gemini_fetch_validated_sync(
            prompt,
            log_label=log_label,
            max_attempts=max_attempts,
            use_search=True,
        )
        return prelim, raw, None
    except Exception as e:
        if _is_gemini_free_quota_error(e) or "rate limit guard" in str(e).lower():
            raise
        log_gemini_error(f"{log_label}_search", e)
        if not allow_plain:
            raise
        log.warning("%s: plain Gemini fallback (RADAR_PLAIN_FALLBACK=1)", log_label)
        prelim, raw = _gemini_fetch_validated_sync(
            prompt,
            log_label=f"{log_label}_plain",
            max_attempts=1,
            use_search=False,
        )
        return prelim, raw, "search_fallback"


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
    if not GEMINI_API_KEY:
        log.warning("Event Radar: GEMINI_API_KEY missing")
        return [], 0, "gemini_api_key_missing"
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


def _fetch_radar_combined_once(*, force_gemini: bool = False) -> tuple[list[dict[str, Any]], int, str | None]:
    from gemini_usage import should_skip_gemini_discovery_sync

    if not GEMINI_API_KEY:
        log.warning("Event Radar: GEMINI_API_KEY missing")
        return [], 0, "gemini_api_key_missing"
    if not force_gemini and should_skip_gemini_discovery_sync():
        log.warning("Event Radar: skipping Gemini discovery — daily limit guard")
        return [], 0, "gemini_quota"
    max_n = min(max(RADAR_PER_SEARCH_MAX + 8, 36), 42)
    try:
        prelim, raw_total, fetch_note = _gemini_fetch_with_search_fallback(
            _radar_combined_prompt(max_n),
            log_label="weekly_discovery",
            max_attempts=1,
        )
    except Exception as e:
        if _is_gemini_free_quota_error(e) or "rate limit guard" in str(e).lower():
            log.error("Event Radar combined: Gemini quota/guard: %s", e)
            return [], 0, "gemini_quota"
        log.error("Gemini Search error", exc_info=True)
        log_gemini_error("radar_combined_fatal", e)
        return [], 0, _gemini_fetch_note_for_error(e)

    deduped = _dedupe_radar_candidates(prelim)
    log.info(
        "Event Radar combined: merged_pre=%s raw_sum=%s fetch_note=%s model=%s",
        len(deduped),
        raw_total,
        fetch_note,
        effective_gemini_model(),
    )
    return deduped, raw_total, fetch_note


def fetch_radar_multi_search_sync(
    *,
    force_gemini: bool = False,
) -> tuple[list[dict[str, Any]], int, str | None]:
    """
    Кандидаты после локальной валидации, сумма длин сырых JSON-массивов, код пустого ответа.

    По умолчанию один combined-запрос (1 счётчик generateContent на free tier).
    Несколько шардов — RADAR_MULTI_SHARD=1 (много запросов, только при платном лимите).
    """
    flag = os.getenv("RADAR_MULTI_SHARD", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        log.info("Event Radar: RADAR_MULTI_SHARD enabled — multiple Gemini calls")
        return _fetch_radar_multi_search_sharded()
    return _fetch_radar_combined_once(force_gemini=force_gemini)


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
    if tier >= 99 and (
        e.get("source_verified")
        or "api-sports" in str(e.get("verified_via", "")).lower()
    ):
        tier = 12
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
    """Weekly: подробная афиша — все события выше порога watchability, без top-N."""
    from collections import Counter

    from event_participants import filter_events_by_participants
    from radar_dedupe import dedupe_events, radar_dedupe_key
    from radar_pipeline_stats import PipelineStats
    from watchability import detect_editorial_type, is_major_weekly_event, min_watchability_for_event

    in_counts = Counter(detect_editorial_type(e) for e in verified)
    log.info(
        "WEEKLY_PIPELINE INPUT: n=%s football=%s f1=%s nhl=%s nba=%s esports=%s ufc=%s other=%s",
        len(verified),
        in_counts.get("football", 0),
        in_counts.get("f1", 0),
        in_counts.get("nhl", 0),
        in_counts.get("nba", 0),
        in_counts.get("esports", 0),
        in_counts.get("ufc", 0),
        sum(v for k, v in in_counts.items() if k not in ("football", "f1", "nhl", "nba", "esports", "ufc")),
    )

    stats = PipelineStats(label="weekly_select")
    stats.set("FOUND", len(verified))

    verified = [
        e
        for e in verified
        if str(e.get("confidence", "medium")).lower() in ("high", "medium")
    ]
    stats.set("AFTER_CONFIDENCE", len(verified))

    prepared = [_prepare_for_afisha_selection(dict(e)) for e in verified]
    after_participants = filter_events_by_participants(prepared, log_prefix="weekly_select")
    stats.set("AFTER_PARTICIPANTS", len(after_participants))
    if len(prepared) > len(after_participants):
        log.info(
            "weekly_select participants dropped: %s",
            len(prepared) - len(after_participants),
        )

    from event_lock import has_confirmed_vn_time
    from locked_time import lock_event_schedule

    timed: list[dict[str, Any]] = []
    for e in after_participants:
        if has_confirmed_vn_time(e):
            timed.append(e)
            continue
        le = lock_event_schedule(dict(e), phase="weekly_select_lock")
        if le and has_confirmed_vn_time(le):
            timed.append(le)
            continue
        stats.removed(str(e.get("title", "")), "missing_datetime")
    stats.set("AFTER_TIME_LOCK", len(timed))

    eligible: list[dict[str, Any]] = []
    for e in timed:
        floor = min_watchability_for_event(e, default_min=RADAR_MIN_WATCHABILITY)
        score = int(e.get("watchability_score", 0))
        if score >= floor:
            eligible.append(e)
        elif is_major_weekly_event(e) and score >= max(16, floor - 16):
            eligible.append(e)
        else:
            stats.removed(
                str(e.get("title", "")),
                f"score_too_low score={score} floor={floor}",
            )
    stats.set("AFTER_SCORE_FILTER", len(eligible))

    deduped = dedupe_events(eligible, log_prefix="weekly_select", exact=True)
    stats.set("AFTER_DEDUPE", len(deduped))

    f1: list[dict[str, Any]] = []
    nba: list[dict[str, Any]] = []
    nhl: list[dict[str, Any]] = []
    football: list[dict[str, Any]] = []
    live: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for e in deduped:
        et = detect_editorial_type(e)
        if et == "f1":
            f1.append(e)
        elif et == "nba":
            nba.append(e)
        elif et == "nhl":
            nhl.append(e)
        elif et == "football":
            football.append(e)
        elif et in ("eurovision", "live", "ufc", "esports"):
            live.append(e)
        else:
            other.append(e)

    f1.sort(key=sort_key_verified)
    nba.sort(key=_watchability_sort_key)
    nhl.sort(key=_watchability_sort_key)
    football.sort(key=_watchability_sort_key)
    live.sort(key=_watchability_sort_key)
    other.sort(key=_watchability_sort_key)

    out: list[dict[str, Any]] = []
    seen_out: set[tuple[str, str, str]] = set()

    def _take(bucket: list[dict[str, Any]]) -> None:
        for e in bucket:
            k = radar_dedupe_key(e)
            if k in seen_out:
                continue
            seen_out.add(k)
            out.append(e)

    _take(f1)
    _take(nba)
    _take(nhl)
    _take(football)
    _take(live)
    _take(other)

    backfill_pool = sorted(
        [e for e in deduped if radar_dedupe_key(e) not in seen_out],
        key=_watchability_sort_key,
    )
    _take(backfill_pool)

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

    out.sort(key=_watchability_sort_key)
    stats.set("FINAL", len(out))
    stats.flush_summary()
    log.info(
        "weekly radar selection: f1=%s nba=%s nhl=%s football=%s live=%s "
        "(min_watchability=%s, no hard cap)",
        len(f1),
        len(nba),
        len(nhl),
        len(football),
        len(live),
        RADAR_MIN_WATCHABILITY,
    )
    return out


def _select_final_radar_events(verified: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Алиас для совместимости."""
    return _select_weekly_radar_events(verified)


def _program_item_to_radar_event(item: dict[str, Any]) -> dict[str, Any] | None:
    from radar_sports_convert import program_item_to_radar_event

    return program_item_to_radar_event(item)


async def _lock_events_from_sports_program(
    program: list[dict[str, Any]],
    *,
    phase: str,
) -> list[dict[str, Any]]:
    from locked_time import lock_event_schedule
    from radar_current_week import filter_radar_events
    from radar_sports_convert import lock_api_sports_program_item

    locked: list[dict[str, Any]] = []
    for item in program:
        ev = _program_item_to_radar_event(item)
        if not ev or gastrobar_hard_reject(ev):
            continue
        le = lock_api_sports_program_item(item, phase=phase)
        if le:
            le["verification_reason"] = "api_sports_match"
            le["source_verified"] = True
            locked.append(_prepare_for_afisha_selection(le))
        else:
            log.info("%s: lock failed title=%r", phase, ev.get("title"))
    from radar_current_week import is_in_current_week

    out: list[dict[str, Any]] = []
    for le in locked:
        if is_in_current_week(le):
            out.append(le)
        else:
            log.info(
                "%s: out of current week title=%r date=%s",
                phase,
                le.get("title"),
                le.get("local_date") or le.get("date"),
            )
    log.info(
        "%s: locked_in=%s week_ok=%s dropped_week=%s",
        phase,
        len(locked),
        len(out),
        len(locked) - len(out),
    )
    return out


async def _api_sports_weekly_seed() -> tuple[list[dict[str, Any]], int]:
    """API-first: футбол, хоккей, NBA, F1 — полный пул недели без top-6 editor cap."""
    if not SPORTS_API_KEY:
        return [], 0
    from sports_events import get_week_radar_pool_with_stats

    program, raw_total, _ = await get_week_radar_pool_with_stats()
    if not program:
        return [], raw_total
    locked = await _lock_events_from_sports_program(program, phase="api_weekly_seed")
    log.info(
        "Event Radar API weekly seed: raw_api=%s pool=%s locked=%s",
        raw_total,
        len(program),
        len(locked),
    )
    return locked, raw_total


async def _fallback_events_from_sports_api() -> tuple[list[dict[str, Any]], int]:
    """Резервная подборка из API-SPORTS, когда Gemini недоступен (429 / квота)."""
    locked, raw_total = await _api_sports_weekly_seed()
    if not locked:
        return [], raw_total
    final = _select_final_radar_events(locked)
    log.info(
        "Event Radar sports fallback: raw_api=%s locked=%s final=%s",
        raw_total,
        len(locked),
        len(final),
    )
    return final, raw_total


async def _fetch_radar_pipeline(
    *,
    force_gemini: bool = False,
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]], str | None]:
    """
    Пул после verify + bar hours + tier (до финального отбора week/now24).
    (pool, raw_total, prelim_raw, fetch_note)
    """
    clear_fetch_cache()
    api_seed, api_raw = await _api_sports_weekly_seed()

    prelim: list[dict[str, Any]] = []
    gemini_raw = 0
    fetch_note: str | None = None

    # «Обновить неделю» = свежий API, не принудительный Gemini
    skip_gemini = RADAR_API_FIRST and bool(api_seed)
    if skip_gemini:
        log.info(
            "Event Radar: API-first — skip Gemini (api_seed=%s, api_raw=%s)",
            len(api_seed),
            api_raw,
        )
        fetch_note = "api_first"
        gemini_raw = 0
    else:
        prelim, gemini_raw, fetch_note = await asyncio.to_thread(
            fetch_radar_multi_search_sync, force_gemini=force_gemini
        )
    raw_total = max(api_raw, gemini_raw)

    if skip_gemini and api_seed:
        pool = [e for e in api_seed if not gastrobar_hard_reject(e)]
        log.info(
            "WEEKLY_PIPELINE API_ONLY: FOUND(raw_api)=%s SEED=%s POOL=%s",
            api_raw,
            len(api_seed),
            len(pool),
        )
        final_pre = _finalize_week_selection(pool, [])
        return final_pre, api_raw, [], fetch_note

    if fetch_note == "gemini_quota":
        fallback, fb_raw = await _fallback_events_from_sports_api()
        if fallback:
            return fallback, fb_raw, [], "sports_fallback"
        if api_seed:
            return api_seed, api_raw, [], "sports_fallback"

    results = await asyncio.gather(*[verify_event(e) for e in prelim])
    from locked_time import has_locked_schedule, lock_event_schedule
    from radar_current_week import filter_radar_events, soft_medium_allowed
    from radar_recall import (
        is_major_search_candidate,
        log_radar_rejection,
        soft_lock_search_candidate,
    )

    verified_all: list[dict[str, Any]] = []
    stats = {
        "verify_failed": 0,
        "low_confidence": 0,
        "soft_medium_accepted": 0,
        "api_or_search_ok": 0,
        "missing_datetime_after": 0,
        "bar_hours_dropped": 0,
    }
    for cand, r in zip(prelim, results):
        if r is None and soft_medium_allowed() and is_major_search_candidate(cand):
            r = soft_lock_search_candidate(cand, phase="pipeline_soft_medium")
            if r:
                stats["soft_medium_accepted"] += 1
        if r and str(r.get("confidence", "medium")).lower() in ("high", "medium"):
            if not has_locked_schedule(r):
                r = lock_event_schedule(r, phase="weekly_pipeline") or r
            if r and has_locked_schedule(r):
                verified_all.append(r)
                stats["api_or_search_ok"] += 1
                if str(r.get("confidence", "")).lower() == "medium":
                    log.info(
                        "radar_medium_accepted pipeline: title=%r via=%s",
                        r.get("title"),
                        r.get("verified_via"),
                    )
            else:
                stats["missing_datetime_after"] += 1
                log_radar_rejection("pipeline", "missing_datetime_after_lock", cand)
        elif r:
            stats["low_confidence"] += 1
            log_radar_rejection(
                "pipeline",
                "low_confidence",
                cand,
                extra=f"conf={r.get('confidence')}",
            )
        else:
            stats["verify_failed"] += 1
            log_radar_rejection("pipeline", "verify_failed", cand)

    log.info(
        "Event Radar verify summary: prelim=%s kept=%s stats=%s",
        len(prelim),
        len(verified_all),
        stats,
    )

    from event_lock import has_confirmed_vn_time

    before_bar = len(verified_all)
    verified_all = filter_events_for_bar_hours(verified_all)
    stats["bar_hours_dropped"] = before_bar - len(verified_all)
    for v in list(verified_all):
        if not has_confirmed_vn_time(v):
            stats["missing_datetime_after"] += 1
            log_radar_rejection("pipeline", "no_confirmed_vn_time", v)
    verified_all = [v for v in verified_all if has_confirmed_vn_time(v)]
    for v in verified_all:
        _prepare_for_afisha_selection(v)

    if api_seed:
        from radar_dedupe import dedupe_events

        merged = dedupe_events(
            api_seed + verified_all, log_prefix="api_gemini_merge", exact=True
        )
        log.info(
            "Event Radar: merged API seed (%s) + verified (%s) -> %s",
            len(api_seed),
            len(verified_all),
            len(merged),
        )
        verified_all = merged

    verified_all = filter_radar_events(
        verified_all, phase="pipeline_final", allow_gemini_discovery=True
    )

    pool = [v for v in verified_all if not gastrobar_hard_reject(v)]
    for v in pool:
        if v.get("radar_priority", 0) < 1:
            v["radar_priority"] = 2

    log.info(
        "WEEKLY_PIPELINE pipeline_final: AFTER_RADAR_VALIDATE=%s "
        "AFTER_TIER_DROP=%s (dropped_tier99=%s)",
        len(verified_all),
        len(pool),
        len(verified_all) - len(pool),
    )

    verify_dropped = stats["verify_failed"] + stats["low_confidence"]
    log.info(
        "Event Radar pipeline: raw=%s pre=%s kept=%s dropped=%s pool=%s",
        raw_total,
        len(prelim),
        len(verified_all),
        verify_dropped,
        len(pool),
    )
    if fetch_note not in (
        "gemini_api_key_missing",
        "gemini_error",
        "gemini_overloaded",
        "gemini_quota",
        "sports_fallback",
        "search_fallback",
    ):
        if not prelim and raw_total == 0:
            fetch_note = "no_candidates"
        elif prelim and not pool:
            fetch_note = "verification_failed"
        elif not pool and api_seed:
            fetch_note = "api_filter_empty"
    return pool, raw_total, prelim, fetch_note


def _finalize_week_selection(pool: list[dict[str, Any]], prelim: list[dict[str, Any]]) -> list[dict[str, Any]]:
    final = _select_final_radar_events(pool)
    final = [e for e in final if not gastrobar_hard_reject(e)]
    if prelim and not final:
        log.warning(
            "Event Radar week: no confirmed events after selection (no last-chance rewrite)"
        )
    return final


async def get_event_radar_week(
    *,
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], int, int, int, str | None]:
    """Афиша недели: кэш за сегодня → иначе 1× Gemini discovery + локальный verify."""
    from gemini_usage import should_skip_gemini_discovery
    from weekly_events_cache import (
        get_weekly_events_cache_for_display,
        save_weekly_events_cache,
        weekly_cache_updated_today_vn,
    )

    if not force_refresh:
        if await weekly_cache_updated_today_vn():
            cached = await get_weekly_events_cache_for_display()
            if cached and len(cached) >= RADAR_API_MIN_SEED:
                log.info("Event Radar week: cache today (%s events)", len(cached))
                return (
                    cached,
                    len(cached),
                    len(cached),
                    len(cached),
                    "weekly_cache_today",
                )
            if cached:
                log.info(
                    "Event Radar week: thin cache today (%s) — refetch API pool",
                    len(cached),
                )

    if not force_refresh and await should_skip_gemini_discovery():
        cached = await get_weekly_events_cache_for_display()
        if cached:
            log.info("Event Radar week: quota guard → cached (%s)", len(cached))
            return (
                cached,
                len(cached),
                len(cached),
                len(cached),
                "weekly_cache_quota",
            )

    pool, raw_total, prelim, fetch_note = await _fetch_radar_pipeline(
        force_gemini=False,
    )
    final = _finalize_week_selection(pool, prelim)
    log.info(
        "Event Radar week: FOUND(raw_api)=%s POOL=%s FINAL=%s note=%s",
        raw_total,
        len(pool),
        len(final),
        fetch_note,
    )

    if not final and fetch_note in ("gemini_quota", "gemini_error", "gemini_overloaded"):
        cached = await get_weekly_events_cache_for_display()
        if cached:
            return (
                cached,
                len(cached),
                len(cached),
                len(cached),
                "weekly_cache_quota",
            )
        fallback, fb_raw = await _fallback_events_from_sports_api()
        if fallback:
            await save_weekly_events_cache(fallback, source="sports_fallback")
            return fallback, fb_raw, 0, len(fallback), "sports_fallback"

    if final:
        await save_weekly_events_cache(final, source="weekly_radar")
    return final, raw_total, raw_total, len(final), fetch_note


async def get_event_radar_now24() -> tuple[list[dict[str, Any]], int, int, int, str | None]:
    """События ближайших 24 ч: API-SPORTS (football) → cache → pipeline."""
    from daily_event import select_now24_events, select_nearest_upcoming
    from next24 import log_next24_window_header
    from now24_sources import fetch_now24_from_api_sports
    from weekly_events_cache import load_weekly_events_cache

    log_next24_window_header()

    api_pool = await fetch_now24_from_api_sports()
    api_n = len(api_pool)
    if api_pool:
        final = select_now24_events(api_pool)
        if final:
            log.info("Event Radar now24 from API-SPORTS: %s", len(final))
            return final, api_n, api_n, len(final), "api_sports_now24"
        log.warning(
            "Event Radar now24: API pool=%s but selected=0 after filters/window",
            api_n,
        )
        return [], api_n, api_n, 0, "api_filter_empty"

    cached = await load_weekly_events_cache()
    if cached:
        final = select_now24_events(cached)
        log.info(
            "Event Radar now24 from weekly cache: pool=%s final=%s",
            len(cached),
            len(final),
        )
        if final:
            return final, len(cached), len(cached), len(final), "weekly_cache"
        if len(cached) > 0:
            return [], len(cached), len(cached), 0, "api_filter_empty"

    pool, raw_total, prelim, fetch_note = await _fetch_radar_pipeline()
    final = select_now24_events(pool)
    if final:
        log.info("Event Radar now24 pipeline final=%s", len(final))
        return final, raw_total, len(prelim), len(final), fetch_note

    if cached:
        upcoming = select_nearest_upcoming(cached, within_days=2)
        final = select_now24_events(upcoming)
        if final:
            log.info("Event Radar now24 nearest from cache: %s", len(final))
            return final, len(cached), len(cached), len(final), "weekly_cache_upcoming"

    log.info("Event Radar now24: empty (cache=%s api=%s pool=%s)", len(cached), len(api_pool), len(pool))
    return [], raw_total, len(prelim), 0, fetch_note


def format_radar_afisha(
    events: list[dict[str, Any]],
    *,
    section_title: str = "🔥 НА ЭТОЙ НЕДЕЛЕ В GASTROBAR",
    apply_grouping: bool = False,
    now24: bool = False,
) -> str:
    """Weekly афиша: подробный список событий (без AI digest / схлопывания)."""
    from event_lock import format_locked_weekly_afisha, lock_events_for_formatter

    prefix = "now24_afisha" if now24 else "weekly_afisha"
    locked = lock_events_for_formatter(events, log_prefix=prefix)
    log.info("FORMATTER RECEIVED EVENTS: count=%s", len(locked))
    return format_locked_weekly_afisha(
        locked,
        section_title=section_title,
        now24=now24,
    )


def format_radar_week_message(events: list[dict[str, Any]]) -> str:
    body = format_radar_afisha(
        events,
        section_title="🔥 НА ЭТОЙ НЕДЕЛЕ В GASTROBAR",
    )
    return f"🔭 Event Radar · Week\n\n{body}"


def format_radar_now24_message(events: list[dict[str, Any]]) -> str:
    from config import TIMEZONE, is_local_run
    from datetime import datetime
    from next24 import resolve_event_local_datetime_vn
    from runtime_messages import build_tag_line
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(TIMEZONE)
    sorted_ev = sorted(
        events,
        key=lambda e: resolve_event_local_datetime_vn(e)
        or datetime.max.replace(tzinfo=tz),
    )
    body = format_radar_afisha(
        sorted_ev,
        section_title="⚡ СОБЫТИЯ В БЛИЖАЙШИЕ 24 ЧАСА",
        now24=True,
    )
    msg = f"⚡ Event Radar · Next 24h\n\n{body}"
    if is_local_run():
        msg += f"\n\n{build_tag_line()}"
    return msg


def _now24_api_sports_source_header(events: list[dict[str, Any]] | None) -> str:
    """Подпись источника для Next24 из API-SPORTS: смешанные виды спорта vs только футбол."""
    from watchability import detect_editorial_type

    hdr = "⚡ Event Radar · Next 24h\nИсточник:"
    if not events:
        return f"{hdr} API-SPORTS."

    kinds: set[str] = set()
    for e in events:
        et = str(e.get("editorial_type") or "").strip().lower()
        if not et:
            et = detect_editorial_type(e)
        kinds.add(et or "other")

    if len(kinds) >= 2:
        return f"{hdr} API-SPORTS / mixed sports."
    if kinds == {"football"}:
        return f"{hdr} API-SPORTS (топ-футбол)."
    return f"{hdr} API-SPORTS."


def radar_fetch_header(
    fetch_note: str | None,
    events: list[dict[str, Any]] | None = None,
) -> str:
    if fetch_note == "search_fallback":
        return "🔭 Event Radar · Gemini (fallback)\nGoogle Search недоступен."
    if fetch_note == "sports_fallback":
        return "🔭 Event Radar · API-SPORTS (резерв)\nЛимит Gemini исчерпан."
    if fetch_note == "weekly_cache":
        return "⚡ Event Radar · Next 24h\nИсточник: афиша недели (кэш)."
    if fetch_note == "weekly_cache_today":
        return (
            "📦 Афиша из кэша (собрана сегодня).\n"
            "Новый Gemini Search не вызывался — лимит free tier."
        )
    if fetch_note == "weekly_cache_quota":
        return "⚠️ Gemini лимит исчерпан. Показываю последнюю сохранённую афишу."
    if fetch_note == "api_sports_now24":
        return _now24_api_sports_source_header(events)
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
