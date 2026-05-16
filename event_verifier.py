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
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

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
* official start time (HH:MM in the timezone you specify)
* source timezone as IANA (e.g. America/Los_Angeles, Europe/London, UTC)
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


def _normalize_hhmm(t: str) -> str | None:
    t = str(t).strip().removeprefix("≈").strip()
    m = _TIME_RE.match(t)
    if not m:
        return None
    h, mi = int(m.group(1)), m.group(2)
    return f"{h:02d}:{mi}"


def _resolve_zone(name: str) -> ZoneInfo | None:
    n = str(name).strip()
    if not n:
        return None
    aliases = {
        "ICT": "Asia/Bangkok",
        "GMT+7": "Asia/Bangkok",
        "UTC+7": "Asia/Bangkok",
        "HO_CHI_MINH": "Asia/Ho_Chi_Minh",
        "VIETNAM": "Asia/Ho_Chi_Minh",
        "EST": "America/New_York",
        "PST": "America/Los_Angeles",
        "CST": "America/Chicago",
        "CET": "Europe/Paris",
        "UK": "Europe/London",
    }
    key = n.upper().replace(" ", "_")
    if key in aliases:
        n = aliases[key]
    try:
        return ZoneInfo(n)
    except Exception:
        return None


def is_valid_source_timezone(name: str) -> bool:
    return _resolve_zone(name) is not None


def convert_to_nha_trang_time(date_s: str, time_s: str, source_timezone: str) -> dict[str, str]:
    """
    Локальная дата+время в source_timezone → дата/время/день недели в Asia/Ho_Chi_Minh.
    """
    if not _DATE_RE.match(date_s):
        raise ValueError("bad date")
    time_clean = _normalize_hhmm(time_s)
    if not time_clean:
        raise ValueError("bad time")
    zi = _resolve_zone(source_timezone)
    if zi is None:
        raise ValueError("bad timezone")

    d = date.fromisoformat(date_s)
    hh, mm = map(int, time_clean.split(":"))
    local_src = datetime.combine(d, dtime(hh, mm), tzinfo=zi)
    nt = local_src.astimezone(TARGET_TZ)
    logger.info(
        "TIME CONVERTED: %s %s %s -> %s %s %s",
        date_s,
        time_clean,
        source_timezone,
        nt.date().isoformat(),
        nt.strftime("%H:%M"),
        _WD_RU[nt.weekday()],
    )
    return {
        "date": nt.date().isoformat(),
        "time": nt.strftime("%H:%M"),
        "weekday": _WD_RU[nt.weekday()],
    }


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
        "emoji": _emoji_for_category(cat),
        "why": "",
        "time_precision": "exact",
        "verified_via": "API-SPORTS",
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

    src_tz = str(data.get("source_timezone", "")).strip()
    if not src_tz:
        logger.info("skipped_no_timezone: %s", data)
        return None

    date_s = str(data.get("date", "")).strip()
    time_s = str(data.get("time", "")).strip()
    if not _DATE_RE.match(date_s):
        return None
    time_norm = _normalize_hhmm(time_s)
    if not time_norm:
        return None

    try:
        conv = convert_to_nha_trang_time(date_s, time_norm, src_tz)
    except Exception as e:
        logger.info("convert_to_nha_trang_time failed: %s", e)
        return None

    tp = str(data.get("time_precision", "exact")).lower().strip()
    if tp not in ("exact", "estimated"):
        tp = "exact"
    tm = conv["time"]
    disp = tm if tp == "exact" else f"≈{tm}"

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
        "category": cat,
        "title": title,
        "subtitle": subtitle,
        "league": subtitle,
        "emoji": _emoji_for_category(cat),
        "why": str(data.get("source_name", "")).strip(),
        "time_precision": tp,
        "verified_via": "Gemini",
    }


_verify_sem = asyncio.Semaphore(4)


async def verify_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """
    Проверка одного события: API-SPORTS (если подходит категория) иначе Gemini Search.
    """
    async with _verify_sem:
        logger.info("VERIFY INPUT: %s", event)

        date_s = str(event.get("date", "")).strip()
        time_s = str(event.get("time", "")).strip()
        title = str(event.get("title", "")).strip()
        category = str(event.get("category", "")).strip()

        if not date_s or not _DATE_RE.match(date_s):
            logger.info("VERIFY RESULT: None")
            return None
        if not time_s or not _normalize_hhmm(time_s):
            logger.info("VERIFY RESULT: None")
            return None
        if not title or len(title) < 3:
            logger.info("VERIFY RESULT: None")
            return None
        if not category:
            logger.info("VERIFY RESULT: None")
            return None

        tl = title.lower()
        if tl in _ABSTRACT_TITLES:
            logger.info("VERIFY RESULT: None")
            return None
        sub0 = str(event.get("subtitle", event.get("league", ""))).strip()
        if sub0 and tl == sub0.lower():
            logger.info("VERIFY RESULT: None")
            return None

        out: dict[str, Any] | None = None
        branch = _sport_api_branch(category)
        if branch and SPORTS_API_KEY:
            try:
                out = await _match_apisports(event, branch)
            except Exception:
                logger.exception("API-SPORTS verify failed")

        if out is None:
            try:
                out = await asyncio.to_thread(_gemini_verify_sync, event)
            except Exception:
                logger.exception("Gemini verify failed")
                out = None

        if out is None:
            logger.info("VERIFY RESULT: None")
            return None

        if gastrobar_hard_reject(out):
            logger.info("VERIFY RESULT: None (gastrobar_hard_reject)")
            return None

        if not out.get("time_display"):
            out["time_display"] = (
                f"≈{out['time']}" if out.get("time_precision") == "estimated" else out["time"]
            )

        if not str(out.get("why", "")).strip():
            out["why"] = str(event.get("why", "")).strip()

        logger.info("VERIFY RESULT: %s", out)
        return out


def sort_key_verified(item: dict[str, Any]) -> tuple[str, str]:
    return (item.get("date", ""), item.get("time", ""))
