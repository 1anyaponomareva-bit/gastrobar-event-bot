"""
Event Radar — недельное расписание для бара (Gemini Search, несколько узких запросов + verify).

Приоритет: UCL / UFC / F1 / Eurovision / плей-офф NBA·NHL / топ-кибер / крупные концерты·премии (см. код).
Отсекаются сериальные финалы вроде Chicago Med/Fire/P.D. и прочий «TV noise» без международного масштаба.

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

from google import genai
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_MODEL

from event_verifier import (
    bar_event_blob,
    clear_fetch_cache,
    gastrobar_hard_reject,
    is_valid_source_timezone,
    sort_key_verified,
    verify_event,
)

log = logging.getLogger(__name__)

RADAR_MAX_ITEMS = 7
# Сколько кандидатов запросить у Gemini до verify (для разнообразия категорий).
RADAR_PER_SEARCH_MAX = 4
# Жёсткий минимум сильных событий не форсируем; максимум 7, лучше меньше слабых P2 в хвосте.
RADAR_SOFT_CAP_WEAK_P2 = 5

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


def _bar_priority(e: dict[str, Any]) -> int:
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


def _radar_schema_instructions(max_n: int) -> str:
    today = _today_iso()
    week = _week_range_human()
    return f"""
Use Google Search. Return ONLY a JSON array (max {max_n} objects), no markdown, no prose outside JSON.
Each item: date (YYYY-MM-DD), time (24h HH:MM), source_timezone (IANA), category, title,
optional subtitle, why_it_matters (one line for a bar big-screen audience in Nha Trang, Vietnam).

Wall-clock date/time MUST be in source_timezone (not Vietnam).
If the listing does not name a zone but gives a universal/world feed time, set source_timezone to "UTC".
Prefer returning a concrete row over skipping: only skip a row if you cannot find any credible date+time.

BAR FILTER — NEVER include:
- Chicago Med / Chicago Fire / Chicago P.D. / One Chicago (any spelling: PD, P.D., etc.)
- US network procedural season/series finales (NBC/CBS/ABC/Fox/CW), ordinary episodic TV finales
- anything not suitable for a crowded bar TV night

Today: {today}
Week window: {week}
"""


def _radar_combined_prompt(max_n: int) -> str:
    year = date.today().year
    schema = _radar_schema_instructions(max_n)
    return f"""You compile ONE weekly TV/sports digest for a bar audience.

Use Google Search. Return ONLY one JSON array (max {max_n} objects), no markdown.
Cover DISTINCT high-draw events scheduled in the week window above — include wherever credible:
UEFA Champions League / major football fixtures, NBA or NHL playoff games, UFC cards with concrete times,
Formula 1 sessions, Eurovision Song Contest {year} if it airs this week, tier-1 esports finals,
major concerts or arena shows.

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
            f"""Search focus: biggest international sports events this week (UTC-friendly listings).
Suggested web queries: "biggest sports events this week UTC", "Champions League this week fixtures time".

PRIORITY: UEFA Champions League, major international football, NBA Playoffs/Finals, Stanley Cup Playoffs, major boxing.
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
        return None

    date_s = str(raw.get("date", "")).strip()
    if not _DATE_RE.match(date_s):
        log.info("skipped_no_date: %s", raw)
        return None

    try:
        d_obj = date.fromisoformat(date_s)
    except ValueError:
        log.info("skipped_no_date invalid: %s", raw)
        return None

    time_raw = raw.get("time")
    if time_raw is None or str(time_raw).strip() == "":
        log.info("skipped_no_time: %s", raw)
        return None

    time_s = _normalize_hhmm(str(time_raw))
    if not time_s:
        log.info("skipped_invalid_time: %s", raw)
        return None

    title = str(raw.get("title", "")).strip()
    if not title or len(title) < 3:
        log.info("skipped_no_title: %s", raw)
        return None

    subtitle = str(raw.get("subtitle", raw.get("league", ""))).strip()
    source_timezone = str(raw.get("source_timezone", "")).strip()
    if not source_timezone:
        source_timezone = "UTC"
    elif not is_valid_source_timezone(source_timezone):
        log.info("skipped_bad_timezone: %s", raw)
        return None

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
    emoji = _emoji_for_category(category)

    cand_pre = {
        "title": title,
        "category": category,
        "subtitle": subtitle,
        "league": subtitle,
        "why": why,
    }
    if gastrobar_hard_reject(cand_pre):
        return None

    return {
        "date": date_s,
        "time": time_s,
        "weekday": _weekday_ru_for_date(d_obj),
        "category": category,
        "title": title,
        "subtitle": subtitle,
        "league": subtitle,
        "why": why,
        "emoji": emoji,
        "source_timezone": source_timezone,
    }


def _event_dedupe_key(e: dict[str, Any]) -> tuple[str, str, str]:
    t = re.sub(r"\s+", " ", (e.get("title") or "").lower().strip())
    return (str(e.get("date", "")), str(e.get("time", "")), t)


def _gemini_fetch_validated_sync(
    prompt: str,
    *,
    log_label: str,
    max_attempts: int = 3,
) -> tuple[list[dict[str, Any]], int]:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY обязателен для Event Radar")

    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
            config = types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            )
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=config,
            )
            text = (response.text or "").strip()
            if not text:
                raise RuntimeError("Пустой ответ Gemini (Event Radar)")

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
                "%s: raw=%s pre_verify=%s sample=%s (attempt %s)",
                log_label,
                raw_total,
                len(validated),
                [(x.get("date"), x.get("time"), x.get("title")) for x in validated[:3]],
                attempt,
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
            last_err = e
            log.warning(
                "Event Radar shard %s attempt %s/%s failed: %s",
                log_label,
                attempt,
                max_attempts,
                e,
            )
            if attempt < max_attempts:
                time.sleep(1.8 * attempt)
    if last_err:
        raise last_err
    raise RuntimeError("Event Radar shard exhausted retries")


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
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(
                _gemini_fetch_validated_sync,
                prompt,
                log_label=label,
                max_attempts=1,
            ): label
            for label, prompt in prompts
        }
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                prelim, raw = fut.result()
            except Exception as e:
                if _is_gemini_free_quota_error(e):
                    log.error("Event Radar shard %s: Gemini quota: %s", label, e)
                    return [], 0, "gemini_quota"
                log.error("Event Radar shard %s failed: %s", label, e)
                shard_failures += 1
                continue
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
    return deduped, total_raw, None


def _fetch_radar_combined_once() -> tuple[list[dict[str, Any]], int, str | None]:
    max_n = min(max(RADAR_PER_SEARCH_MAX * 3, 12), 18)
    try:
        prelim, raw_total = _gemini_fetch_validated_sync(
            _radar_combined_prompt(max_n),
            log_label="radar_combined",
            max_attempts=2,
        )
    except Exception as e:
        if _is_gemini_free_quota_error(e):
            log.error("Event Radar combined: Gemini quota exhausted: %s", e)
            return [], 0, "gemini_quota"
        log.exception("Event Radar combined failed: %s", e)
        return [], 0, "gemini_error"

    deduped = _dedupe_radar_candidates(prelim)
    log.info(
        "Event Radar combined: merged_pre=%s raw_sum=%s",
        len(deduped),
        raw_total,
    )
    return deduped, raw_total, None


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


def _select_final_radar_events(verified: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """P1 сначала, затем P2; Eurovision обязателен если есть в verified; мягкий срез слабого хвоста."""
    for e in verified:
        if _is_eurovision_event(e) and not str(e.get("subtitle", "")).strip():
            e["subtitle"] = "Music / Live show"
            e["league"] = e["subtitle"]

    p1 = [e for e in verified if e.get("radar_priority") == 1]
    p2 = [e for e in verified if e.get("radar_priority") == 2]
    p1.sort(key=sort_key_verified)
    p2.sort(key=sort_key_verified)

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for bucket in (p1, p2):
        for e in bucket:
            k = _event_dedupe_key(e)
            if k in seen:
                continue
            seen.add(k)
            out.append(e)
            if len(out) >= RADAR_MAX_ITEMS:
                break
        if len(out) >= RADAR_MAX_ITEMS:
            break

    euro_pool = [e for e in verified if _is_eurovision_event(e)]
    if euro_pool:
        best_e = sorted(euro_pool, key=sort_key_verified)[0]
        k0 = _event_dedupe_key(best_e)
        if not any(_event_dedupe_key(x) == k0 for x in out):
            if len(out) < RADAR_MAX_ITEMS:
                out.append(best_e)
            else:
                replaced = False
                for i in range(len(out) - 1, -1, -1):
                    if out[i].get("radar_priority") == 2:
                        out[i] = best_e
                        replaced = True
                        break
                if not replaced:
                    out[-1] = best_e

    out.sort(key=sort_key_verified)

    if len(out) > RADAR_SOFT_CAP_WEAK_P2:
        n_p1 = sum(1 for x in out if x.get("radar_priority") == 1)
        if n_p1 >= 2:
            while len(out) > RADAR_SOFT_CAP_WEAK_P2 and out and out[-1].get("radar_priority") == 2:
                out.pop()

    out.sort(key=sort_key_verified)
    return out[:RADAR_MAX_ITEMS]


async def get_event_radar_week() -> tuple[list[dict[str, Any]], int, int, int, str | None]:
    """
    (события после verify, суммарно сырых JSON из Gemini за все шард-поиски,
    суммарно кандидатов после локальной валидации, финальных, причина пустого fetch).
    """
    clear_fetch_cache()
    prelim, raw_total, fetch_note = await asyncio.to_thread(fetch_radar_multi_search_sync)
    results = await asyncio.gather(*[verify_event(e) for e in prelim])
    verified_all = [r for r in results if r]
    for v in verified_all:
        v["radar_priority"] = _bar_priority(v)

    verified_prio = [v for v in verified_all if v.get("radar_priority", 0) >= 1]
    if verified_prio:
        pool = verified_prio
    else:
        pool = [v for v in verified_all if not gastrobar_hard_reject(v)]
        if pool:
            log.warning(
                "Event Radar: no P1/P2 after verify; using %s verified rows "
                "(bar hard filter only, treated as P2)",
                len(pool),
            )
            for v in pool:
                if v.get("radar_priority", 0) < 1:
                    v["radar_priority"] = 2

    final = _select_final_radar_events(pool)
    final = [e for e in final if not gastrobar_hard_reject(e)]

    log.info(
        "Event Radar verified: raw_total=%s pre_verify=%s final=%s",
        raw_total,
        len(prelim),
        len(final),
    )
    return final, raw_total, len(prelim), len(final), fetch_note


def format_radar_afisha(events: list[dict[str, Any]]) -> str:
    """Одна аккуратная афиша для Telegram (без второго «рекламного» блока)."""
    if not events:
        return "Пока нет событий в подборке."
    lines = ["🔥 НА ЭТОЙ НЕДЕЛЕ В GASTROBAR", ""]
    for e in events:
        em = str(e.get("emoji", "🏟")).strip()
        wd = str(e.get("weekday", "")).strip()
        tm = str(e.get("time_display") or e.get("time", "")).strip()
        title = str(e.get("title", "")).strip()
        sub = str(e.get("subtitle", e.get("league", ""))).strip()
        lines.append(f"{em} {wd} {tm}")
        lines.append(title)
        if sub and sub.lower() != title.lower():
            lines.append(sub)
        lines.append("")
    lines.append("📍Океанус, улица с траками")
    return "\n".join(lines).strip()


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
        tm = str(e.get("time_display") or e.get("time", "")).strip()
        lines.append(
            f"{e.get('emoji', '•')} {e.get('weekday', '')} {tm} — "
            f"{e.get('title', '')}" + (f" ({sub})" if str(sub).strip() else "")
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
        f"{e.get('weekday', '')} {e.get('time_display') or e.get('time', '')} {e.get('title', '')}"
        for e in events[:4]
    ]
    return "; ".join(p for p in parts if p.strip())
