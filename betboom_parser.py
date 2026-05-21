"""
BetBoom-first: парсинг линии (Playwright network capture + optional JSON URL).

Логи:
  BETBOOM_FETCH_STARTED
  BETBOOM_EVENTS_FOUND
  BETBOOM_PARSE_ERROR
  BETBOOM_FILTERED_EVENTS
  BETBOOM_CACHE_USED
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from config import (
    BETBOOM_API_FALLBACK,
    BETBOOM_BASE_URL,
    BETBOOM_FETCH_TIMEOUT_SEC,
    BETBOOM_JSON_URL,
    BETBOOM_PAGE_TIMEOUT_MS,
    BETBOOM_SITE_API,
    BETBOOM_USE_PLAYWRIGHT,
    TIMEZONE,
)

log = logging.getLogger(__name__)

VN_TZ = ZoneInfo(TIMEZONE)

# slug на betboom.ru/sport/{slug}
SPORT_PAGES: tuple[tuple[str, str], ...] = (
    ("football", "football"),
    ("hockey", "ice-hockey"),
    ("basketball", "basketball"),
    ("formula1", "formula-1"),
    ("esports", "esport"),
    ("mma", "boxing"),
    ("mma2", "martial-arts"),
)

_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_VS_RE = re.compile(
    r"^\s*(.+?)\s*(?:—|–|-| vs\.?| v )\s*(.+?)\s*$",
    re.I,
)

_FETCH_NOTE_OK = "betboom_ok"
_FETCH_NOTE_CACHE = "betboom_cache"
_FETCH_NOTE_UNAVAILABLE = "betboom_unavailable"
_FETCH_NOTE_PARSE_ERROR = "betboom_parse_error"


@dataclass
class BetBoomFetchResult:
    events: list[dict[str, Any]]
    fetch_note: str | None = None
    raw_found: int = 0
    filtered_found: int = 0
    source: str = "BetBoom"
    used_cache: bool = False
    errors: list[str] = field(default_factory=list)


def _today_vn() -> date:
    return datetime.now(VN_TZ).date()


def _event_blob(e: dict[str, Any]) -> str:
    return " ".join(
        str(e.get(k, ""))
        for k in ("title", "league", "sport", "home", "away", "subtitle")
    ).lower()


def _parse_local_datetime(date_s: str, time_s: str) -> datetime | None:
    if not _DATE_RE.match(date_s):
        return None
    m = _TIME_RE.search(time_s or "")
    if not m:
        return None
    try:
        d = date.fromisoformat(date_s)
        hh, mm = m.group(0).split(":")
        return datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=VN_TZ)
    except ValueError:
        return None


def _normalize_sport(raw: str, blob: str) -> str:
    s = (raw or "").lower()
    if s in ("ice-hockey", "ice_hockey", "hockey"):
        return "hockey"
    if s in ("formula-1", "formula1", "f1"):
        return "formula1"
    if s in ("esport", "esports", "cyber"):
        return "esports"
    if s in ("basketball", "nba"):
        return "basketball"
    if s in ("boxing", "martial-arts", "mma", "ufc"):
        return "mma"
    if "hockey" in blob or "nhl" in blob or "khl" in blob:
        return "hockey"
    if "formula" in blob or "grand prix" in blob or re.search(r"\bfp[123]\b", blob):
        return "formula1"
    if re.search(r"\b(cs2|dota|valorant|esports|iem|blast)\b", blob):
        return "esports"
    if "nba" in blob:
        return "basketball"
    if re.search(r"\bufc\b|boxing|бокс", blob):
        return "mma"
    return "football" if "football" in s or "футбол" in blob else (s or "football")


def _looks_like_match_title(title: str) -> bool:
    t = (title or "").strip()
    if len(t) < 5:
        return False
    return bool(_VS_RE.match(t) or re.search(r"\bvs\.?\b", t, re.I))


def _extract_title_teams(title: str) -> tuple[str, str, str]:
    m = _VS_RE.match(title.strip())
    if m:
        home, away = m.group(1).strip(), m.group(2).strip()
        return f"{home} — {away}", home, away
    return title.strip(), "", ""


def _coerce_event(
    *,
    sport: str,
    title: str,
    league: str,
    date_s: str,
    time_s: str,
    source_url: str = "",
    home: str = "",
    away: str = "",
) -> dict[str, Any] | None:
    title = title.strip()
    if not title:
        return None
    if not _looks_like_match_title(title) and sport not in ("formula1",):
        if sport == "formula1" and not re.search(r"practice|qualifying|race|sprint|gp", title, re.I):
            return None
        elif sport != "formula1":
            return None
    if not home and not away:
        title, home, away = _extract_title_teams(title)
    local_dt = _parse_local_datetime(date_s, time_s)
    if local_dt is None and sport != "formula1":
        return None
    blob = f"{title} {league}".lower()
    sport = _normalize_sport(sport, blob)
    row: dict[str, Any] = {
        "sport": sport,
        "title": title,
        "league": league.strip() or sport,
        "home": home,
        "away": away,
        "date": date_s,
        "time": time_s,
        "source": "BetBoom",
        "source_url": source_url or BETBOOM_BASE_URL,
        "importance": "medium",
    }
    if local_dt is not None:
        row["local_datetime"] = local_dt.isoformat()
        row["date"] = local_dt.date().isoformat()
        row["time"] = local_dt.strftime("%H:%M")
    return row


def _team_name(val: Any) -> str:
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        return str(val.get("name") or val.get("title") or "").strip()
    return ""


def _event_from_loose_dict(d: dict[str, Any], *, page_sport: str, page_url: str) -> dict[str, Any] | None:
    """Эвристика для JSON из network capture."""
    title = str(d.get("title") or d.get("name") or "").strip()
    home = _team_name(d.get("home") or d.get("homeTeam") or d.get("team1"))
    away = _team_name(d.get("away") or d.get("awayTeam") or d.get("team2"))
    if not title and home and away:
        title = f"{home} — {away}"
    league = str(
        d.get("league")
        or d.get("tournament")
        or d.get("championship")
        or d.get("competition")
        or d.get("category")
        or ""
    )
    if isinstance(league, dict):
        league = str(league.get("name") or league.get("title") or "")
    ts = d.get("startTime") or d.get("start_time") or d.get("start") or d.get("kickoff")
    date_s = str(d.get("date") or "")[:10]
    time_s = str(d.get("time") or "")
    if ts and not date_s:
        try:
            if isinstance(ts, (int, float)):
                dt = datetime.fromtimestamp(ts, tz=VN_TZ)
            else:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=VN_TZ)
                else:
                    dt = dt.astimezone(VN_TZ)
            date_s = dt.date().isoformat()
            time_s = dt.strftime("%H:%M")
        except (ValueError, OSError):
            pass
    if not date_s:
        date_s = _today_vn().isoformat()
    return _coerce_event(
        sport=page_sport,
        title=title,
        league=league,
        date_s=date_s,
        time_s=time_s,
        source_url=str(d.get("url") or page_url),
    )


def _walk_json_for_events(
    obj: Any,
    *,
    page_sport: str,
    page_url: str,
    out: list[dict[str, Any]],
    depth: int = 0,
) -> None:
    if depth > 14:
        return
    if isinstance(obj, dict):
        ev = _event_from_loose_dict(obj, page_sport=page_sport, page_url=page_url)
        if ev:
            out.append(ev)
        for v in obj.values():
            _walk_json_for_events(v, page_sport=page_sport, page_url=page_url, out=out, depth=depth + 1)
    elif isinstance(obj, list):
        for item in obj[:500]:
            _walk_json_for_events(item, page_sport=page_sport, page_url=page_url, out=out, depth=depth + 1)


def _dedupe_raw(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for e in events:
        key = (
            e.get("date", ""),
            e.get("time", ""),
            str(e.get("title", "")).lower()[:120],
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def filter_betboom_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from sports_events import is_gastrobar_api_sport_worthy

    out: list[dict[str, Any]] = []
    for e in events:
        if is_gastrobar_api_sport_worthy(e):
            out.append(e)
    log.info("BETBOOM_FILTERED_EVENTS raw=%s worthy=%s", len(events), len(out))
    return out


def _in_days_window(e: dict[str, Any], *, days_ahead: int) -> bool:
    date_s = str(e.get("date", ""))
    if not _DATE_RE.match(date_s):
        return False
    try:
        d = date.fromisoformat(date_s)
    except ValueError:
        return False
    start = _today_vn()
    end = start + timedelta(days=max(0, days_ahead - 1))
    return start <= d <= end


async def _fetch_json_url() -> list[dict[str, Any]]:
    if not BETBOOM_JSON_URL:
        return []
    headers = {
        "User-Agent": "Mozilla/5.0 GastrobarBot/1.0",
        "Accept": "application/json",
        "Referer": BETBOOM_BASE_URL,
    }
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        r = await client.get(BETBOOM_JSON_URL, headers=headers)
        r.raise_for_status()
        data = r.json()
    found: list[dict[str, Any]] = []
    _walk_json_for_events(data, page_sport="football", page_url=BETBOOM_JSON_URL, out=found)
    return _dedupe_raw(found)


def _playwright_fetch_sync(*, days_ahead: int) -> list[dict[str, Any]]:
    """Sync Playwright: перехват JSON ответов siteapi при открытии страниц линии."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("playwright not installed") from exc

    captured: list[dict[str, Any]] = []
    end_day = _today_vn() + timedelta(days=max(0, days_ahead - 1))

    def on_response(response) -> None:
        url = response.url or ""
        if "siteapi" not in url and "site_api" not in url and "/api/" not in url:
            return
        try:
            if response.status != 200:
                return
            body = response.json()
        except Exception:
            return
        page_sport = "football"
        for sp, slug in SPORT_PAGES:
            if slug in url or slug in (response.request.headers.get("referer") or ""):
                page_sport = sp
                break
        buf: list[dict[str, Any]] = []
        _walk_json_for_events(body, page_sport=page_sport, page_url=url, out=buf)
        for ev in buf:
            try:
                d = date.fromisoformat(str(ev.get("date", "")))
            except ValueError:
                continue
            if _today_vn() <= d <= end_day:
                captured.append(ev)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="ru-RU",
            timezone_id=TIMEZONE,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        page = context.new_page()
        page.on("response", on_response)
        for _sp, slug in SPORT_PAGES:
            url = f"{BETBOOM_BASE_URL}/sport/{slug}"
            try:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=BETBOOM_PAGE_TIMEOUT_MS,
                )
                page.wait_for_timeout(800)
            except Exception as exc:
                log.warning("BETBOOM_PARSE_ERROR page=%s: %s", slug, exc)
        browser.close()
    return _dedupe_raw(captured)


async def _fetch_via_playwright(*, days_ahead: int) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_playwright_fetch_sync, days_ahead=days_ahead)


async def fetch_betboom_line(*, days_ahead: int = 3) -> BetBoomFetchResult:
    """
    Основной ingest BetBoom (до 3 календарных дней, VN).
    При ошибке — cache; опционально API-SPORTS fallback снаружи.
    """
    log.info("BETBOOM_FETCH_STARTED days_ahead=%s", days_ahead)
    try:
        return await asyncio.wait_for(
            _fetch_betboom_line_inner(days_ahead=days_ahead),
            timeout=BETBOOM_FETCH_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        log.warning(
            "EVENT_RADAR_TIMEOUT_PREVENTED=true BETBOOM_FETCH timeout after %.0fs",
            BETBOOM_FETCH_TIMEOUT_SEC,
        )
        return await _betboom_timeout_result(days_ahead=days_ahead)


async def _betboom_timeout_result(*, days_ahead: int) -> BetBoomFetchResult:
    from betboom_cache import load_betboom_cache

    result = BetBoomFetchResult(events=[], fetch_note=None)
    cached = await load_betboom_cache(allow_stale=True)
    if cached:
        filtered = filter_betboom_events(
            [e for e in cached if _in_days_window(e, days_ahead=days_ahead)]
        )
        result.events = filtered
        result.filtered_found = len(filtered)
        result.fetch_note = _FETCH_NOTE_CACHE
        result.used_cache = True
        result.errors = [f"timeout:{BETBOOM_FETCH_TIMEOUT_SEC}s"]
        log.info("BETBOOM_CACHE_USED after timeout events=%s", len(filtered))
        return result
    result.fetch_note = _FETCH_NOTE_UNAVAILABLE
    result.errors = [f"timeout:{BETBOOM_FETCH_TIMEOUT_SEC}s"]
    return result


async def _fetch_betboom_line_inner(*, days_ahead: int = 3) -> BetBoomFetchResult:
    from betboom_cache import load_betboom_cache, save_betboom_cache

    result = BetBoomFetchResult(events=[], fetch_note=None)
    raw: list[dict[str, Any]] = []
    errors: list[str] = []

    try:
        if BETBOOM_JSON_URL:
            raw.extend(await _fetch_json_url())
    except Exception as exc:
        errors.append(f"json_url:{exc}")
        log.exception("BETBOOM_PARSE_ERROR json_url", exc_info=True)

    if BETBOOM_USE_PLAYWRIGHT and len(raw) < 3:
        try:
            pw = await _fetch_via_playwright(days_ahead=days_ahead)
            raw.extend(pw)
        except Exception as exc:
            errors.append(f"playwright:{exc}")
            log.exception("BETBOOM_PARSE_ERROR playwright", exc_info=True)

    raw = [e for e in raw if _in_days_window(e, days_ahead=days_ahead)]
    raw = _dedupe_raw(raw)
    result.raw_found = len(raw)
    log.info("BETBOOM_EVENTS_FOUND raw=%s", result.raw_found)

    if not raw:
        cached = await load_betboom_cache(allow_stale=True)
        if cached:
            filtered = filter_betboom_events(
                [e for e in cached if _in_days_window(e, days_ahead=days_ahead)]
            )
            result.events = filtered
            result.filtered_found = len(filtered)
            result.fetch_note = _FETCH_NOTE_CACHE
            result.used_cache = True
            result.errors = errors
            log.info("BETBOOM_CACHE_USED events=%s", len(filtered))
            return result
        result.fetch_note = _FETCH_NOTE_UNAVAILABLE if errors else _FETCH_NOTE_PARSE_ERROR
        result.errors = errors
        log.warning("BETBOOM_PARSE_ERROR no events errors=%s", errors)
        return result

    filtered = filter_betboom_events(raw)
    result.events = filtered
    result.filtered_found = len(filtered)
    result.fetch_note = _FETCH_NOTE_OK
    result.errors = errors
    await save_betboom_cache(raw, meta={"days_ahead": days_ahead, "worthy": len(filtered)})
    log.info(
        "BETBOOM fetch ok: raw=%s worthy=%s days=%s",
        result.raw_found,
        result.filtered_found,
        days_ahead,
    )
    return result


def is_betboom_failure_note(note: str | None) -> bool:
    return note in (_FETCH_NOTE_UNAVAILABLE, _FETCH_NOTE_PARSE_ERROR)


def format_betboom_unavailable_message(note: str | None = None) -> str:
    if note == _FETCH_NOTE_PARSE_ERROR:
        body = (
            "Источник BetBoom: ошибка парсинга линии.\n"
            "Показан сохранённый кэш (если есть) или попробуйте позже."
        )
    else:
        body = (
            "Источник BetBoom сейчас недоступен.\n"
            "Нет сохранённой афиши — это не «пустой список матчей»."
        )
    return body


async def merge_betboom_with_api_fallback(
    *,
    days_ahead: int = 3,
) -> tuple[list[dict[str, Any]], str | None, int]:
    """
    Только BetBoom (+ betboom_cache). API-SPORTS — только если BETBOOM_API_FALLBACK=1
    и аккаунт не suspended/rateLimit (один probe, без 30 запросов).
    """
    bb = await fetch_betboom_line(days_ahead=days_ahead)
    if bb.events:
        return bb.events, bb.fetch_note, bb.raw_found

    note = bb.fetch_note or _FETCH_NOTE_UNAVAILABLE

    if not BETBOOM_API_FALLBACK:
        log.info("BETBOOM empty, API-SPORTS fallback disabled (BETBOOM_API_FALLBACK=0)")
        return [], note, bb.raw_found

    from api_sports_status import get_last_sport_status, probe_sport
    from config import SPORTS_API_KEY

    if not SPORTS_API_KEY:
        return [], note, 0

    status = get_last_sport_status()
    worst = "ok"
    for st in status.values():
        h = str(st).split(":")[0]
        if h in ("suspended", "rateLimit", "unavailable"):
            worst = h
    if worst in ("suspended", "rateLimit"):
        log.error(
            "API-SPORTS fallback skipped: account %s (no mass fetch)",
            worst,
        )
        return [], note, 0

    if not status:
        today = datetime.now(VN_TZ).date().isoformat()
        headers = {"x-apisports-key": SPORTS_API_KEY}
        health, _ = await probe_sport(
            "football",
            f"https://v3.football.api-sports.io/fixtures?date={today}",
            headers=headers,
        )
        if health in ("suspended", "rateLimit"):
            log.error("API-SPORTS probe %s — fallback disabled this run", health)
            return [], note, 0

    from sports_events import _merge_raw_safe_72h_events

    log.warning("BETBOOM empty — optional API-SPORTS fallback (days=%s)", days_ahead)
    api = await _merge_raw_safe_72h_events(days_ahead=days_ahead)
    if api.fetch_note in ("api_suspended", "api_rate_limit"):
        log.error("API-SPORTS fallback aborted: %s", api.fetch_note)
        return [], note, 0
    if api.events:
        return api.events, "api_sports_fallback", len(api.events)
    return [], note, 0
