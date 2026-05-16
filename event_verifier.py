"""
Слой проверки Event Radar: API-SPORTS для матчей, Gemini Search для остального.
Финальная дата/время/день недели — только после конвертации в Asia/Ho_Chi_Minh в Python.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, time as dtime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from google import genai
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_MODEL, SPORTS_API_KEY

logger = logging.getLogger(__name__)

TARGET_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_WD_RU = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?$")
_APPROX_TIME_PREFIXES = re.compile(
    r"^(≈|~|around|about|примерно|ориентировочно|circa)\s*",
    re.I,
)
_SHOW_CONFIDENCE = frozenset({"high", "medium"})

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

# Кэш запросов по дню в рамках одного прогона get_event_radar_week
_fetch_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}


def clear_fetch_cache() -> None:
    _fetch_cache.clear()


def bar_event_blob(e: dict[str, Any]) -> str:
    """Нормализованный текст для правил «подходит ли бару»."""
    parts = (e.get("title"), e.get("category"), e.get("subtitle"), e.get("league"), e.get("why"))
    s = " ".join(str(p or "") for p in parts).lower()
    for a, b in (("\u2019", "'"), ("\u2018", "'"), ("\u2013", "-"), ("\u2014", "-")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def gastrobar_hard_reject(e: dict[str, Any]) -> bool:
    """
    События, которые не должны попадать в Gastrobar: One Chicago, procedural finales,
    обычные сериальные финалы без уровня Eurovision/Oscars и т.д.
    """
    b = bar_event_blob(e)

    if re.search(
        r"chicago[\s\-–—]*(med|fire|p\.?\s*d\.?|pd)\b|\bone[\s\-–—]*chicago\b",
        b,
        re.I,
    ):
        logger.info("gastrobar_hard_reject: chicago_one %s", e.get("title"))
        return True

    if re.search(
        r"\b(?:ncis|law\s+and\s+order|grey'?s\s+anatomy|station\s+19|9-1-1)\b",
        b,
    ) and ("finale" in b or "final episode" in b):
        logger.info("gastrobar_hard_reject: procedural_finale %s", e.get("title"))
        return True

    mega_finale_ok = any(
        x in b
        for x in (
            "eurovision",
            "oscar",
            "academy award",
            "grammy",
            "golden globe",
            "super bowl",
            "wrestlemania",
            "champions league",
            "uefa champions",
        )
    )

    if re.search(r"(season|series)\s+finale", b):
        if mega_finale_ok:
            return False
        logger.info("gastrobar_hard_reject: season_series_finale %s", e.get("title"))
        return True

    if "finale" in b or "final episode" in b:
        if mega_finale_ok:
            return False
        if re.search(r"\b(nbc|cbs|abc|fox|the\s+cw)\b", b):
            logger.info("gastrobar_hard_reject: network_finale %s", e.get("title"))
            return True
        if "episode" in b and "finale" in b:
            logger.info("gastrobar_hard_reject: episode_finale %s", e.get("title"))
            return True
        if re.search(r"\b(med|fire|pd)\s*finale\b", b) and "chicago" in b:
            logger.info("gastrobar_hard_reject: chicago_finale_token %s", e.get("title"))
            return True

    return False


VERIFY_PROMPT = """Verify this event for a weekly bar schedule in Nha Trang, Vietnam.
Return JSON only.

Candidate event (from another model pass):
{event_json}

Do not verify US network procedural season/series finales (Chicago Med/Fire/P.D., NCIS, Law & Order, Grey's Anatomy, etc.) as suitable — return verified:false unless it is truly Eurovision/Oscars-level.

You must confirm using reliable web sources:
* exact event title
* exact participants (as in official listings)
* official date (YYYY-MM-DD)
* official start time (HH:MM) in source timezone — NOT converted to Vietnam
* source_timezone as IANA only (e.g. Europe/Zurich for Eurovision, America/New_York for UFC US)
* do NOT return Asia/Ho_Chi_Minh as source_timezone unless the listing is truly Vietnam-local
* whether the time is exact or estimated (for UFC main event when only approximate, use estimated)
* reliable source name (short)

If you cannot verify exact date AND time AND source timezone, return:
{"verified": false, "reason": "..."}

Do not guess.

If verified true, return shape:
{
  "verified": true,
  "title": "Vegas Golden Knights — Anaheim Ducks",
  "category": "NHL",
  "league": "Stanley Cup Playoffs",
  "date": "2026-05-14",
  "time": "21:30",
  "source_timezone": "America/New_York",
  "time_precision": "exact",
  "source_name": "NHL official site"
}

Use "time_precision": "estimated" when the listing is approximate (e.g. UFC main event TBC, about 9pm).

For UFC: if only main card start is official, you may put "Main card" in league. If only main event approximate, use time_precision "estimated" and league like "Main event, ориентировочно".
"""


def _emoji_for_category(cat: str) -> str:
    c = (cat or "").upper()
    if "F1" in c or "FORMULA" in c or "MOTORSPORT" in c or "MOTOR" in c:
        return "🏎"
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


def emoji_for_event(e: dict[str, Any]) -> str:
    """Эмодзи по смыслу события (title/subtitle), не только category от Gemini."""
    b = bar_event_blob(e)
    if re.search(r"formula\s*1|\bf1\b|grand\s+prix", b):
        return "🏎"
    if "eurovision" in b:
        return "🎤"
    if re.search(r"\bufc\b|\bmma\b|boxing", b):
        return "🥊"
    if re.search(r"\bnba\b|basketball", b):
        return "🏀"
    if re.search(r"\bnhl\b|hockey|stanley\s+cup", b):
        return "🏒"
    if re.search(r"champions\s+league|uefa|premier\s+league|\bucl\b", b):
        return "⚽"
    if re.search(r"esports|dota|valorant|cs2|lol worlds", b):
        return "🎮"
    return _emoji_for_category(str(e.get("category", "")))


def _normalize_hhmm(t: str) -> str | None:
    t = str(t).strip()
    t = _APPROX_TIME_PREFIXES.sub("", t).strip()
    t = t.removeprefix("≈").strip()
    t = re.sub(
        r"\s*(UTC|GMT|CET|CEST|EST|EDT|PST|PDT|BST|ICT|IST|MSK)\s*$",
        "",
        t,
        flags=re.I,
    ).strip()
    m = _TIME_RE.match(t)
    if not m:
        return None
    h, mi = int(m.group(1)), m.group(2)
    return f"{h:02d}:{mi}"


def _parse_time_flexible(t: str) -> tuple[str | None, bool]:
    """HH:MM и приблизительные форматы (≈20:00, around 9pm)."""
    raw = str(t or "").strip()
    if not raw:
        return None, False
    low = raw.lower()
    if low in ("tbc", "tba", "tbd", "уточняется", "время уточняется"):
        return None, True
    is_approx = bool(_APPROX_TIME_PREFIXES.match(raw))
    cleaned = _APPROX_TIME_PREFIXES.sub("", raw).strip()
    m12 = re.match(
        r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
        cleaned,
        re.I,
    )
    if m12:
        h = int(m12.group(1)) % 12
        if m12.group(3).lower() == "pm":
            h += 12
        mi = m12.group(2) or "00"
        return f"{h:02d}:{mi}", True
    norm = _normalize_hhmm(cleaned)
    return norm, is_approx


def _is_entertainment_category(category: str) -> bool:
    u = (category or "").upper()
    if _sport_api_branch(category):
        return False
    return any(
        x in u
        for x in (
            "ESPORT",
            "EUROVISION",
            "CONCERT",
            "MUSIC",
            "GAMING",
            "GAME",
            "STREAM",
            "AWARD",
            "GRAMMY",
            "OSCAR",
            "SHOW",
            "WWE",
            "TV",
            "NETFLIX",
            "VIRAL",
            "POP",
        )
    )


def _resolve_zone(name: str) -> ZoneInfo | None:
    from event_time import resolve_zone as _rz

    return _rz(name)


def is_valid_source_timezone(name: str) -> bool:
    from event_time import is_valid_source_timezone as _valid

    return _valid(name)


def convert_to_nha_trang_time(date_s: str, time_s: str, source_timezone: str) -> dict[str, str]:
    from event_time import convert_event_time

    return convert_event_time(date_s, time_s, source_timezone)


def _iso_to_nhatrang(dt_iso: str) -> dict[str, str] | None:
    s = str(dt_iso).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            d = date.fromisoformat(s[:10])
            dt = datetime.combine(d, dtime(0, 0), tzinfo=ZoneInfo("UTC"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    nt = dt.astimezone(TARGET_TZ)
    logger.info(
        "TIME CONVERTED: %s -> %s %s %s",
        dt_iso,
        nt.date().isoformat(),
        nt.strftime("%H:%M"),
        _WD_RU[nt.weekday()],
    )
    return {
        "date": nt.date().isoformat(),
        "time": nt.strftime("%H:%M"),
        "weekday": _WD_RU[nt.weekday()],
    }


def _tokenize(s: str) -> set[str]:
    s = re.sub(r"[^\w\s]", " ", (s or "").lower())
    return {w for w in s.split() if len(w) > 2}


def _similarity(a: str, b: str) -> float:
    sa, sb = _tokenize(a), _tokenize(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _split_sides(title: str) -> tuple[str, str] | None:
    t = (title or "").strip()
    for sep in (" — ", " – ", " —", "— ", " - ", " vs ", " @ ", " v "):
        if sep in t:
            a, b = t.split(sep, 1)
            a, b = a.strip(), b.strip()
            if a and b:
                return a, b
    return None


def _sport_api_branch(category: str) -> str | None:
    u = (category or "").upper()
    if any(x in u for x in ("NBA", "WNBA", "BASKET", "EUROLEAGUE", "NCAA")):
        return "basketball"
    if any(x in u for x in ("NHL", "HOCKEY", "KHL", "AHL")):
        return "hockey"
    if any(x in u for x in ("F1", "FORMULA", "GRAND PRIX", "GP ")):
        return "formula1"
    if any(
        x in u
        for x in (
            "FOOT",
            "SOCCER",
            "UEFA",
            "UCL",
            "EPL",
            "LA LIGA",
            "SERIE A",
            "BUNDES",
            "CHAMPIONS",
            "WORLD CUP",
            "LIGUE 1",
        )
    ):
        return "football"
    return None


async def _http_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    data = r.json()
    if not isinstance(data, dict):
        raise RuntimeError("bad json")
    return data


async def _fetch_football_day(d: date) -> list[dict[str, Any]]:
    if not SPORTS_API_KEY:
        return []
    key = ("football", d.isoformat())
    if key in _fetch_cache:
        return _fetch_cache[key]
    headers = {"x-apisports-key": SPORTS_API_KEY}
    url = f"https://v3.football.api-sports.io/fixtures?date={d.isoformat()}"
    try:
        data = await _http_json(url, headers)
    except Exception as e:
        logger.warning("API-SPORTS football %s: %s", d, e)
        _fetch_cache[key] = []
        return []
    out: list[dict[str, Any]] = []
    for item in data.get("response") or []:
        if not isinstance(item, dict):
            continue
        fixture = item.get("fixture") or {}
        league = item.get("league") or {}
        teams = item.get("teams") or {}
        home = (teams.get("home") or {}).get("name") or ""
        away = (teams.get("away") or {}).get("name") or ""
        title = f"{home} — {away}".strip(" —")
        dt_iso = fixture.get("date") or ""
        league_name = league.get("name") or ""
        rnd = league.get("round") or ""
        league_full = league_name + (f" ({rnd})" if rnd else "")
        out.append({"title": title, "dt_iso": dt_iso, "league": league_full or league_name})
    _fetch_cache[key] = out
    return out


async def _fetch_basketball_day(d: date) -> list[dict[str, Any]]:
    if not SPORTS_API_KEY:
        return []
    key = ("basketball", d.isoformat())
    if key in _fetch_cache:
        return _fetch_cache[key]
    headers = {"x-apisports-key": SPORTS_API_KEY}
    url = f"https://v1.basketball.api-sports.io/games?date={d.isoformat()}"
    try:
        data = await _http_json(url, headers)
    except Exception as e:
        logger.warning("API-SPORTS basketball %s: %s", d, e)
        _fetch_cache[key] = []
        return []
    out: list[dict[str, Any]] = []
    for item in data.get("response") or []:
        if not isinstance(item, dict):
            continue
        league = item.get("league") or {}
        teams = item.get("teams") or {}
        home = (teams.get("home") or {}).get("name") or ""
        away = (teams.get("away") or {}).get("name") or ""
        title = f"{home} — {away}".strip(" —")
        dt_iso = item.get("date") or item.get("time") or ""
        league_name = league.get("name") or ""
        rnd = league.get("round") or ""
        league_full = league_name + (f" ({rnd})" if rnd else "")
        out.append({"title": title, "dt_iso": str(dt_iso), "league": league_full or league_name})
    _fetch_cache[key] = out
    return out


async def _fetch_hockey_day(d: date) -> list[dict[str, Any]]:
    if not SPORTS_API_KEY:
        return []
    key = ("hockey", d.isoformat())
    if key in _fetch_cache:
        return _fetch_cache[key]
    headers = {"x-apisports-key": SPORTS_API_KEY}
    url = f"https://v1.hockey.api-sports.io/games?date={d.isoformat()}"
    try:
        data = await _http_json(url, headers)
    except Exception as e:
        logger.warning("API-SPORTS hockey %s: %s", d, e)
        _fetch_cache[key] = []
        return []
    out: list[dict[str, Any]] = []
    for item in data.get("response") or []:
        if not isinstance(item, dict):
            continue
        league = item.get("league") or {}
        teams = item.get("teams") or {}
        home = (teams.get("home") or {}).get("name") or ""
        away = (teams.get("away") or {}).get("name") or ""
        title = f"{home} — {away}".strip(" —")
        dt_iso = item.get("date") or item.get("time") or ""
        league_name = league.get("name") or ""
        rnd = league.get("round") or ""
        league_full = league_name + (f" ({rnd})" if rnd else "")
        out.append({"title": title, "dt_iso": str(dt_iso), "league": league_full or league_name})
    _fetch_cache[key] = out
    return out


async def _fetch_formula_day(d: date) -> list[dict[str, Any]]:
    if not SPORTS_API_KEY:
        return []
    key = ("formula1", d.isoformat())
    if key in _fetch_cache:
        return _fetch_cache[key]
    headers = {"x-apisports-key": SPORTS_API_KEY}
    url = f"https://v1.formula-1.api-sports.io/races?date={d.isoformat()}"
    try:
        data = await _http_json(url, headers)
    except Exception as e:
        logger.warning("API-SPORTS formula1 %s: %s", d, e)
        _fetch_cache[key] = []
        return []
    out: list[dict[str, Any]] = []
    for item in data.get("response") or []:
        if not isinstance(item, dict):
            continue
        race_name = (
            item.get("raceName")
            or item.get("name")
            or item.get("race")
            or item.get("eventName")
            or ""
        )
        dt_iso = item.get("date") or item.get("time") or ""
        title = str(race_name or "Formula 1").strip()
        if "formula" not in title.lower():
            title = f"Formula 1 {title}"
        out.append({"title": title, "dt_iso": str(dt_iso), "league": "Formula 1"})
    _fetch_cache[key] = out
    return out


async def _match_apisports(event: dict[str, Any], branch: str) -> dict[str, Any] | None:
    title_cand = str(event.get("title", ""))
    try:
        base = date.fromisoformat(str(event.get("date", ""))[:10])
    except ValueError:
        return None

    fetchers = {
        "football": _fetch_football_day,
        "basketball": _fetch_basketball_day,
        "hockey": _fetch_hockey_day,
        "formula1": _fetch_formula_day,
    }
    fn = fetchers.get(branch)
    if not fn:
        return None

    best: tuple[float, dict[str, Any]] | None = None
    for delta in (0, -1, 1):
        d = base + timedelta(days=d)
        rows = await fn(d)
        for row in rows:
            sc = _similarity(title_cand, row.get("title", ""))
            if best is None or sc > best[0]:
                best = (sc, row)

    if not best or best[0] < 0.42:
        return None
    row = best[1]
    sides = _split_sides(title_cand)
    if sides:
        blob = (row.get("title") or "").lower()
        ok_a = any(w in blob for w in _tokenize(sides[0]))
        ok_b = any(w in blob for w in _tokenize(sides[1]))
        if not (ok_a and ok_b) and best[0] < 0.58:
            return None
    conv = _iso_to_nhatrang(row.get("dt_iso") or "")
    if not conv:
        return None
    cat = str(event.get("category", "SPORT"))
    subtitle = str(event.get("subtitle", event.get("league", ""))).strip() or row.get(
        "league", ""
    )
    tm = conv["time"]
    return {
        "date": conv["date"],
        "time": tm,
        "time_display": tm,
        "weekday": conv["weekday"],
        "category": cat,
        "title": row.get("title") or title_cand,
        "subtitle": subtitle,
        "league": subtitle,
        "emoji": emoji_for_event(
            {"title": row.get("title") or title_cand, "category": cat, "subtitle": subtitle}
        ),
        "why": "",
        "time_precision": "exact",
        "verified_via": "API-SPORTS",
        "confidence": "high",
        "verification_reason": "api_sports_match",
    }


def _truthy_verified(v: Any) -> bool:
    if v is True:
        return True
    if isinstance(v, str) and v.strip().lower() in ("true", "yes", "1"):
        return True
    return False


def _extract_json_object(text: str) -> dict[str, Any]:
    t = text.strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t)
        if m:
            t = m.group(1).strip()
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        i0, i1 = t.find("{"), t.rfind("}")
        if i0 == -1 or i1 <= i0:
            raise
        data = json.loads(t[i0 : i1 + 1])
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return data


def _gemini_verify_sync(event: dict[str, Any]) -> dict[str, Any] | None:
    if not GEMINI_API_KEY:
        return None
    payload = VERIFY_PROMPT.replace(
        "{event_json}", json.dumps(event, ensure_ascii=False, indent=2)
    )
    client = genai.Client(api_key=GEMINI_API_KEY)
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=payload,
        config=config,
    )
    text = (response.text or "").strip()
    if not text:
        return None
    try:
        data = _extract_json_object(text)
    except Exception as e:
        logger.info("verify JSON parse failed: %s", e)
        return None
    if not _truthy_verified(data.get("verified")):
        logger.info("verify rejected: %s", data.get("reason", data))
        return None

    from event_time import convert_event_to_vn, extract_source_fields

    merged = dict(event)
    merged["date"] = str(data.get("date", event.get("date", ""))).strip()
    merged["time"] = str(data.get("time", event.get("time", ""))).strip()
    merged["source_timezone"] = str(data.get("source_timezone", "")).strip()
    orig_date, orig_time, orig_tz = extract_source_fields(merged)
    if data.get("source_timezone"):
        merged["source_timezone"] = str(data["source_timezone"]).strip()

    conv, time_precision = convert_event_to_vn(merged)
    if conv is None:
        logger.info("skipped_no_timezone_or_convert: %s", event.get("title"))
        return None

    date_s = orig_date or str(data.get("date", "")).strip()

    tp = str(data.get("time_precision", time_precision)).lower().strip()
    if tp not in ("exact", "estimated", "unknown"):
        tp = time_precision if time_precision in ("exact", "estimated") else "exact"
    tm = conv["time"]
    disp = tm if tp == "exact" else (f"≈{tm}" if tp == "estimated" else "время уточняется")

    title = str(data.get("title", "")).strip() or str(event.get("title", "")).strip()
    cat = str(data.get("category", event.get("category", ""))).strip() or "EVENT"
    subtitle = str(data.get("league", data.get("subtitle", ""))).strip()
    if not subtitle:
        subtitle = str(event.get("subtitle", event.get("league", ""))).strip()

    tl = title.lower()
    if tl in _ABSTRACT_TITLES:
        logger.info("skipped_abstract_title after verify: %s", title)
        return None
    if subtitle and tl == subtitle.lower():
        logger.info("skipped_abstract_title equals subtitle after verify: %s", title)
        return None

    return {
        "date": conv["date"],
        "time": tm,
        "time_display": disp,
        "weekday": conv["weekday"],
        "original_date": date_s,
        "original_time": orig_time,
        "original_timezone": merged.get("source_timezone", orig_tz),
        "source_timezone": merged.get("source_timezone", orig_tz),
        "category": cat,
        "title": title,
        "subtitle": subtitle,
        "league": subtitle,
        "emoji": emoji_for_event(
            {"title": title, "category": cat, "subtitle": subtitle}
        ),
        "why": str(data.get("source_name", "")).strip(),
        "time_precision": tp,
        "verified_via": "Gemini",
        "confidence": "high",
        "verification_reason": "gemini_verify_pass",
    }


def _log_verify_removed(title: str, reason: str, event: dict[str, Any]) -> None:
    missing = []
    if not str(event.get("date", "")).strip():
        missing.append("date")
    if not str(event.get("time", "")).strip():
        missing.append("time")
    if not str(event.get("title", "")).strip():
        missing.append("title")
    logger.info(
        "verify_removed: reason=%s title=%r missing_fields=%s event=%s",
        reason,
        title,
        missing or "none",
        {k: event.get(k) for k in ("date", "time", "title", "category", "source_timezone")},
    )


def event_from_search_candidate(
    event: dict[str, Any],
    *,
    confidence: str = "medium",
    verified_via: str = "Gemini Search",
    verification_reason: str = "search_fields_ok",
) -> dict[str, Any] | None:
    """
    Событие из Gemini Search: конвертация времени только в Python (event_time).
    """
    from event_time import convert_event_to_vn, extract_source_fields

    title = str(event.get("title", "")).strip()
    if not title or len(title) < 3:
        _log_verify_removed(title, "missing_title", event)
        return None

    orig_date, orig_time, orig_tz = extract_source_fields(event)
    if not _DATE_RE.match(orig_date):
        _log_verify_removed(title, "bad_date", event)
        return None

    tl = title.lower()
    if tl in _ABSTRACT_TITLES:
        _log_verify_removed(title, "abstract_title", event)
        return None
    sub0 = str(event.get("subtitle", event.get("league", ""))).strip()
    if sub0 and tl == sub0.lower():
        _log_verify_removed(title, "title_equals_subtitle", event)
        return None

    conv, time_precision = convert_event_to_vn(event)

    if conv is None:
        time_display = "время уточняется"
        out_date = orig_date
        out_time = ""
        out_weekday = ""
        try:
            d_obj = date.fromisoformat(orig_date)
            out_weekday = _WD_RU[d_obj.weekday()]
        except ValueError:
            pass
    else:
        out_date = conv["date"]
        out_time = conv["time"]
        out_weekday = conv["weekday"]
        if time_precision == "unknown":
            time_display = "время уточняется"
        elif time_precision == "estimated":
            time_display = f"≈{out_time}"
        else:
            time_display = out_time

    cat = str(event.get("category", "EVENT")).strip()[:48] or "EVENT"
    subtitle = str(event.get("subtitle", event.get("league", ""))).strip()
    why = str(event.get("why", "")).strip()

    out = {
        "date": out_date,
        "time": out_time,
        "time_display": time_display,
        "weekday": out_weekday,
        "original_date": orig_date,
        "original_time": orig_time,
        "original_timezone": orig_tz,
        "source_timezone": orig_tz,
        "category": cat,
        "title": title,
        "subtitle": subtitle,
        "league": subtitle,
        "emoji": emoji_for_event(
            {"title": title, "category": cat, "subtitle": subtitle}
        ),
        "why": why,
        "time_precision": time_precision,
        "verified_via": verified_via,
        "confidence": confidence,
        "verification_reason": verification_reason,
    }

    if gastrobar_hard_reject(out):
        _log_verify_removed(title, "gastrobar_hard_reject", event)
        return None

    return out


_verify_sem = asyncio.Semaphore(4)


def _strict_second_verify_enabled() -> bool:
    import os

    return os.getenv("RADAR_STRICT_VERIFY", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


async def verify_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """
  Проверка одного события:
  - sports: API-SPORTS → confidence high; иначе medium из Gemini Search
  - entertainment/esports: medium из Gemini Search (без второго verify по умолчанию)
  - RADAR_STRICT_VERIFY=1 — дополнительно старый жёсткий Gemini verify
    """
    async with _verify_sem:
        title = str(event.get("title", "")).strip()
        category = str(event.get("category", "")).strip()
        logger.info("VERIFY INPUT: %s", event)

        if gastrobar_hard_reject(event):
            _log_verify_removed(title, "gastrobar_hard_reject_input", event)
            return None

        out: dict[str, Any] | None = None
        branch = _sport_api_branch(category)
        api_reason = ""

        if branch and SPORTS_API_KEY:
            try:
                out = await _match_apisports(event, branch)
                if out:
                    api_reason = "api_sports_match"
            except Exception:
                logger.exception("API-SPORTS verify failed for %s", title)

        if out is None and _strict_second_verify_enabled():
            try:
                out = await asyncio.to_thread(_gemini_verify_sync, event)
                if out:
                    api_reason = "gemini_strict_verify"
            except Exception:
                logger.exception("Gemini strict verify failed for %s", title)

        if out is None:
            if _is_entertainment_category(category):
                reason = "entertainment_gemini_search"
            elif branch:
                reason = "sport_no_api_match"
            else:
                reason = "gemini_search"
            out = event_from_search_candidate(
                event,
                confidence="medium",
                verified_via="Gemini Search",
                verification_reason=reason,
            )

        if out is None:
            _log_verify_removed(title, "could_not_build_event", event)
            return None

        conf = str(out.get("confidence", "medium")).lower()
        if conf not in _SHOW_CONFIDENCE:
            _log_verify_removed(title, f"confidence_{conf}_hidden", event)
            return None

        if not out.get("time_display"):
            if out.get("time_precision") == "unknown":
                out["time_display"] = "время уточняется"
            elif out.get("time_precision") == "estimated":
                out["time_display"] = f"≈{out['time']}"
            else:
                out["time_display"] = out["time"]

        if not str(out.get("why", "")).strip():
            out["why"] = str(event.get("why", "")).strip()

        if not out.get("verification_reason"):
            out["verification_reason"] = api_reason or "search_fields_ok"

        logger.info(
            "VERIFY RESULT: confidence=%s via=%s title=%r reason=%s",
            out.get("confidence"),
            out.get("verified_via"),
            out.get("title"),
            out.get("verification_reason"),
        )
        return out


def sort_key_verified(item: dict[str, Any]) -> tuple[str, str]:
    return (item.get("date", ""), item.get("time", ""))
