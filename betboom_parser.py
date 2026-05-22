"""
BetBoom-first: BETBOOM_JSON_URL (HTTP GET) или Playwright fallback.

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
    BETBOOM_USE_PLAYWRIGHT,
    TIMEZONE,
    betboom_json_headers,
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
_FETCH_NOTE_EMPTY = "betboom_empty_line"
_FETCH_NOTE_JSON_OK = "betboom_json_ok"
_FETCH_NOTE_JSON_AUTH = "betboom_json_auth"

# Приоритетные виды спорта (укладываемся в BETBOOM_FETCH_TIMEOUT_SEC)
PLAYWRIGHT_SPORT_SLUGS: tuple[tuple[str, str], ...] = (
    ("football", "football"),
    ("hockey", "ice-hockey"),
    ("basketball", "basketball"),
)


@dataclass
class BetBoomFetchResult:
    events: list[dict[str, Any]]
    fetch_note: str | None = None
    raw_found: int = 0
    filtered_found: int = 0
    source: str = "BetBoom"
    used_cache: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass
class BetBoomJsonProbeResult:
    url_set: bool
    status_code: int | None = None
    content_type: str = ""
    root_keys: list[str] = field(default_factory=list)
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    body_preview: str = ""
    auth_required: bool = False


def _today_vn() -> date:
    return datetime.now(VN_TZ).date()


def _event_blob(e: dict[str, Any]) -> str:
    return " ".join(
        str(e.get(k, ""))
        for k in ("title", "league", "sport", "home", "away", "subtitle")
    ).lower()


def _parse_betboom_wall_datetime(ts: Any) -> datetime | None:
    """
    Время из BetBoom JSON: уже локальное (VN). Без повторной конвертации UTC→VN.
    Строка с Z/+offset: берём «настенные» часы, tz = Asia/Ho_Chi_Minh.
    """
    if ts is None or ts == "":
        return None
    if isinstance(ts, (int, float)):
        val = float(ts)
        if val > 1e12:
            val /= 1000.0
        return datetime.fromtimestamp(val, tz=VN_TZ)
    s = str(ts).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1]
    m_off = re.search(r"^(.+?)[+-]\d{2}:?\d{2}$", s)
    if m_off:
        s = m_off.group(1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt.replace(tzinfo=VN_TZ)


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


def _participants_str(home: str, away: str, title: str) -> str:
    if home and away:
        return f"{home} — {away}"
    return title.strip()


def _build_betboom_event(
    *,
    sport: str,
    title: str,
    league: str,
    home: str = "",
    away: str = "",
    local_dt: datetime | None = None,
    source_url: str = "",
    date_s: str = "",
    time_s: str = "",
) -> dict[str, Any] | None:
    title = title.strip()
    if not title:
        return None
    if not home and not away:
        title, home, away = _extract_title_teams(title)
    if local_dt is None:
        local_dt = _parse_local_datetime(date_s, time_s)
    if local_dt is None and sport != "formula1":
        return None
    blob = f"{title} {league}".lower()
    sport = _normalize_sport(sport, blob)
    participants = _participants_str(home, away, title)
    row: dict[str, Any] = {
        "sport": sport,
        "league": league.strip() or sport,
        "title": title,
        "participants": participants,
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
        if home and away:
            pass
        elif sport == "formula1" and re.search(
            r"practice|qualifying|race|sprint|gp", title, re.I
        ):
            pass
        else:
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


def _event_from_json_dict(
    d: dict[str, Any],
    *,
    page_sport: str,
    page_url: str,
) -> dict[str, Any] | None:
    """Парсинг записи из BETBOOM_JSON_URL (время = VN wall)."""
    sport = str(d.get("sport") or page_sport or "").strip()
    title = str(d.get("title") or d.get("name") or d.get("eventName") or "").strip()
    home = _team_name(
        d.get("home")
        or d.get("homeTeam")
        or d.get("team1")
        or d.get("competitor1")
        or d.get("firstParticipant")
    )
    away = _team_name(
        d.get("away")
        or d.get("awayTeam")
        or d.get("team2")
        or d.get("competitor2")
        or d.get("secondParticipant")
    )
    parts = d.get("participants")
    if isinstance(parts, str) and parts.strip():
        if not home and not away:
            t, h, a = _extract_title_teams(parts.strip())
            if h or a:
                title, home, away = t, h, a
            elif not title:
                title = parts.strip()
    elif isinstance(parts, (list, tuple)) and len(parts) >= 2:
        if not home:
            home = _team_name(parts[0])
        if not away:
            away = _team_name(parts[1])
    league = str(
        d.get("league")
        or d.get("tournament")
        or d.get("championship")
        or d.get("competition")
        or ""
    )
    if isinstance(league, dict):
        league = str(league.get("name") or league.get("title") or "")

    local_dt: datetime | None = None
    if d.get("local_datetime"):
        try:
            raw_ld = str(d["local_datetime"])
            local_dt = _parse_betboom_wall_datetime(raw_ld)
        except (ValueError, TypeError):
            local_dt = None

    ts = (
        d.get("startTime")
        or d.get("start_time")
        or d.get("start")
        or d.get("kickoff")
        or d.get("startAt")
        or d.get("dateTime")
        or d.get("matchTime")
    )
    date_s = str(d.get("date") or "")[:10]
    time_s = str(d.get("time") or "")
    if local_dt is None and ts:
        local_dt = _parse_betboom_wall_datetime(ts)
    if local_dt is not None:
        date_s = local_dt.date().isoformat()
        time_s = local_dt.strftime("%H:%M")

    return _build_betboom_event(
        sport=sport,
        title=title,
        league=league,
        home=home,
        away=away,
        local_dt=local_dt,
        source_url=str(d.get("url") or page_url),
        date_s=date_s,
        time_s=time_s,
    )


def _event_from_loose_dict(d: dict[str, Any], *, page_sport: str, page_url: str) -> dict[str, Any] | None:
    """Эвристика для JSON из Playwright capture."""
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
    ts = (
        d.get("startTime")
        or d.get("start_time")
        or d.get("start")
        or d.get("kickoff")
        or d.get("startAt")
        or d.get("start_at")
        or d.get("dateTime")
        or d.get("matchTime")
    )
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
    json_endpoint: bool = False,
) -> None:
    if depth > 14:
        return
    if isinstance(obj, dict):
        if json_endpoint:
            ev = _event_from_json_dict(obj, page_sport=page_sport, page_url=page_url)
        else:
            ev = _event_from_loose_dict(obj, page_sport=page_sport, page_url=page_url)
        if ev:
            out.append(ev)
        for v in obj.values():
            _walk_json_for_events(
                v,
                page_sport=page_sport,
                page_url=page_url,
                out=out,
                depth=depth + 1,
                json_endpoint=json_endpoint,
            )
    elif isinstance(obj, list):
        for item in obj[:500]:
            _walk_json_for_events(
                item,
                page_sport=page_sport,
                page_url=page_url,
                out=out,
                depth=depth + 1,
                json_endpoint=json_endpoint,
            )


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


def _json_root_keys(data: Any) -> list[str]:
    if isinstance(data, dict):
        return [str(k) for k in list(data.keys())[:30]]
    if isinstance(data, list):
        return [f"list(len={len(data)})"]
    return [type(data).__name__]


def _in_hours_window(e: dict[str, Any], *, hours: int = 72) -> bool:
    ld = str(e.get("local_datetime") or "")
    dt: datetime | None = None
    if ld:
        try:
            dt = datetime.fromisoformat(ld)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=VN_TZ)
        except ValueError:
            dt = None
    if dt is None:
        dt = _parse_local_datetime(str(e.get("date", "")), str(e.get("time", "")))
    if dt is None:
        return False
    now = datetime.now(VN_TZ)
    return now <= dt <= now + timedelta(hours=hours)


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


def _is_line_api_url(url: str) -> bool:
    u = (url or "").lower()
    skip = (
        ".png",
        ".jpg",
        ".jpeg",
        ".svg",
        ".woff",
        ".css",
        ".js",
        "analytics",
        "google",
        "yandex",
        "facebook",
        "hotjar",
    )
    if any(x in u for x in skip):
        return False
    if any(
        x in u
        for x in (
            "siteapi",
            "site_api",
            "sporthub",
            "betboom.ru",
            "betboompass",
            "ws.betboom",
        )
    ):
        return "/api/" in u or "site_api" in u or "_ws" in u or "socket" in u
    return False


def _ingest_json_payload(
    data: Any,
    *,
    page_sport: str,
    page_url: str,
    end_day: date,
    captured: list[dict[str, Any]],
) -> int:
    buf: list[dict[str, Any]] = []
    _walk_json_for_events(data, page_sport=page_sport, page_url=page_url, out=buf)
    added = 0
    for ev in buf:
        try:
            d = date.fromisoformat(str(ev.get("date", "")))
        except ValueError:
            continue
        if _today_vn() <= d <= end_day:
            captured.append(ev)
            added += 1
    return added


def _ingest_response_body(
    body: Any,
    *,
    url: str,
    page_sport: str,
    end_day: date,
    captured: list[dict[str, Any]],
) -> int:
    if isinstance(body, (dict, list)):
        return _ingest_json_payload(
            body,
            page_sport=page_sport,
            page_url=url,
            end_day=end_day,
            captured=captured,
        )
    return 0


async def fetch_betboom_json_http(*, max_hours: int = 72) -> BetBoomJsonProbeResult:
    """HTTP GET BETBOOM_JSON_URL → события (без Playwright)."""
    if not BETBOOM_JSON_URL:
        return BetBoomJsonProbeResult(url_set=False, error="BETBOOM_JSON_URL not set")

    try:
        headers = betboom_json_headers()
    except ValueError as exc:
        return BetBoomJsonProbeResult(url_set=True, error=str(exc))

    log.info("BETBOOM_JSON_FETCH url=%s", BETBOOM_JSON_URL[:120])
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            r = await client.get(BETBOOM_JSON_URL, headers=headers)
    except httpx.HTTPError as exc:
        log.exception("BETBOOM_JSON_FETCH http error", exc_info=True)
        return BetBoomJsonProbeResult(
            url_set=True,
            error=f"{type(exc).__name__}: {exc}",
        )

    content_type = (r.headers.get("content-type") or "").split(";")[0].strip()
    body_preview = (r.text or "")[:500]
    status = r.status_code

    if status in (401, 403):
        log.warning("BETBOOM_JSON_FETCH auth required status=%s", status)
        return BetBoomJsonProbeResult(
            url_set=True,
            status_code=status,
            content_type=content_type,
            error=(
                "BetBoom JSON endpoint требует cookie/header. "
                "Скопируйте headers из DevTools."
            ),
            body_preview=body_preview,
            auth_required=True,
        )

    if status >= 400:
        log.warning(
            "BETBOOM_JSON_FETCH http %s body=%s",
            status,
            body_preview[:200],
        )
        return BetBoomJsonProbeResult(
            url_set=True,
            status_code=status,
            content_type=content_type,
            error=f"HTTP {status}",
            body_preview=body_preview,
        )

    try:
        data = r.json()
    except json.JSONDecodeError as exc:
        log.error(
            "BETBOOM_JSON_PARSE failed: %s body_preview=%s",
            exc,
            body_preview,
        )
        return BetBoomJsonProbeResult(
            url_set=True,
            status_code=status,
            content_type=content_type,
            error=f"JSON parse failed: {exc}",
            body_preview=body_preview,
        )

    found: list[dict[str, Any]] = []
    _walk_json_for_events(
        data,
        page_sport="football",
        page_url=BETBOOM_JSON_URL,
        out=found,
        json_endpoint=True,
    )
    deduped = _dedupe_raw(found)
    in_window = [e for e in deduped if _in_hours_window(e, hours=max_hours)]
    log.info(
        "BETBOOM_JSON_FETCH ok status=%s root_keys=%s raw=%s in_%sh=%s",
        status,
        _json_root_keys(data),
        len(deduped),
        max_hours,
        len(in_window),
    )
    return BetBoomJsonProbeResult(
        url_set=True,
        status_code=status,
        content_type=content_type,
        root_keys=_json_root_keys(data),
        raw_events=in_window,
    )


def format_debug_betboom_json_report(probe: BetBoomJsonProbeResult) -> str:
    lines = [
        "🔎 BetBoom JSON debug",
        f"BETBOOM_JSON_URL set: {'yes' if probe.url_set else 'no'}",
    ]
    if not probe.url_set:
        lines.append("")
        lines.append("Задайте BETBOOM_JSON_URL в Railway Variables (URL из DevTools → Network).")
        return "\n".join(lines)

    if probe.error and probe.status_code is None:
        lines.append(f"error: {probe.error}")
        return "\n".join(lines)

    lines.append(f"status code: {probe.status_code or '—'}")
    lines.append(f"content-type: {probe.content_type or '—'}")
    if probe.root_keys:
        lines.append(f"json root keys: {', '.join(probe.root_keys[:15])}")
    else:
        lines.append("json root keys: —")
    lines.append(f"найдено raw events: {len(probe.raw_events)}")

    if probe.error:
        lines.append("")
        lines.append(f"⚠️ {probe.error}")
        if probe.body_preview and probe.auth_required:
            lines.append(f"preview: {probe.body_preview[:200]}")

    if probe.raw_events:
        lines.append("")
        lines.append("Первые 5 событий:")
        for i, ev in enumerate(probe.raw_events[:5], 1):
            lines.append(
                f"{i}. {ev.get('sport', '?')} | {ev.get('league', '')} | "
                f"{ev.get('participants') or ev.get('title', '?')} | "
                f"{ev.get('local_datetime') or ev.get('date', '')} {ev.get('time', '')}"
            )
    return "\n".join(lines)


async def debug_betboom_json() -> str:
    probe = await fetch_betboom_json_http(max_hours=72)
    return format_debug_betboom_json_report(probe)


def _playwright_fetch_sync(*, days_ahead: int) -> list[dict[str, Any]]:
    """Playwright: HTTP JSON + WebSocket sporthub/tree_ws (линия BetBoom)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("playwright not installed") from exc

    captured: list[dict[str, Any]] = []
    end_day = _today_vn() + timedelta(days=max(0, days_ahead - 1))
    json_urls: list[str] = []
    ws_frames = 0

    def _sport_for_url(url: str, referer: str = "") -> str:
        blob = f"{url} {referer}".lower()
        for sp, slug in PLAYWRIGHT_SPORT_SLUGS:
            if slug in blob:
                return sp
        return "football"

    def on_response(response) -> None:
        url = response.url or ""
        if not _is_line_api_url(url):
            return
        try:
            if response.status != 200:
                return
            body = response.json()
        except Exception:
            return
        sp = _sport_for_url(url, response.request.headers.get("referer") or "")
        n = _ingest_response_body(
            body, url=url, page_sport=sp, end_day=end_day, captured=captured
        )
        if n:
            json_urls.append(url[:160])

    def on_websocket(ws) -> None:
        nonlocal ws_frames

        def on_frame(frame) -> None:
            nonlocal ws_frames
            payload = getattr(frame, "payload", frame)
            if isinstance(payload, bytes):
                try:
                    text = payload.decode("utf-8", errors="ignore")
                except Exception:
                    return
            else:
                text = str(payload or "")
            text = text.strip()
            if not text or text[0] not in "{[":
                return
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return
            sp = _sport_for_url(ws.url or "")
            n = _ingest_json_payload(
                data,
                page_sport=sp,
                page_url=ws.url or "ws",
                end_day=end_day,
                captured=captured,
            )
            if n:
                ws_frames += 1

        ws.on("framereceived", on_frame)

    per_page_ms = max(
        8000,
        int((BETBOOM_FETCH_TIMEOUT_SEC * 1000 - 3000) / max(len(PLAYWRIGHT_SPORT_SLUGS), 1)),
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="ru-RU",
            timezone_id=TIMEZONE,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9"},
        )
        page = context.new_page()
        page.on("response", on_response)
        page.on("websocket", on_websocket)
        for _sp, slug in PLAYWRIGHT_SPORT_SLUGS:
            if len(captured) >= 12:
                break
            url = f"{BETBOOM_BASE_URL}/sport/{slug}"
            try:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=per_page_ms,
                )
                page.wait_for_timeout(3500)
            except Exception as exc:
                log.warning("BETBOOM_PARSE_ERROR page=%s: %s", slug, exc)
        browser.close()

    out = _dedupe_raw(captured)
    log.info(
        "BETBOOM_PLAYWRIGHT done raw=%s json_urls=%s ws_frames_with_events=%s sample_urls=%s",
        len(out),
        len(json_urls),
        ws_frames,
        json_urls[:5],
    )
    return out


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
    max_hours = max(72, days_ahead * 24)

    if BETBOOM_JSON_URL:
        probe = await fetch_betboom_json_http(max_hours=max_hours)
        if probe.error:
            errors.append(probe.error)
            if probe.auth_required:
                result.fetch_note = _FETCH_NOTE_JSON_AUTH
            else:
                result.fetch_note = _FETCH_NOTE_PARSE_ERROR
            log.warning("BETBOOM_JSON_FETCH failed: %s", probe.error)
        else:
            raw = list(probe.raw_events)
            log.info("BETBOOM_JSON_ONLY raw=%s (Playwright skipped)", len(raw))
    else:
        if BETBOOM_USE_PLAYWRIGHT:
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
        if errors:
            result.fetch_note = _FETCH_NOTE_UNAVAILABLE
        else:
            result.fetch_note = _FETCH_NOTE_EMPTY
        result.errors = errors
        log.warning(
            "BETBOOM_PARSE_ERROR no events note=%s errors=%s",
            result.fetch_note,
            errors,
        )
        return result

    filtered = filter_betboom_events(raw)
    result.events = filtered
    result.filtered_found = len(filtered)
    result.fetch_note = (
        _FETCH_NOTE_JSON_OK if BETBOOM_JSON_URL else _FETCH_NOTE_OK
    )
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
    return note in (
        _FETCH_NOTE_UNAVAILABLE,
        _FETCH_NOTE_PARSE_ERROR,
        _FETCH_NOTE_EMPTY,
        _FETCH_NOTE_JSON_AUTH,
    )


def format_betboom_unavailable_message(note: str | None = None) -> str:
    if note == _FETCH_NOTE_EMPTY:
        body = (
            "BetBoom: линия пуста (Playwright не получил матчи по WebSocket/API).\n"
            "Часто на сервере вне РФ. Показан кэш или резерв API-SPORTS (если включён).\n"
            "Можно задать BETBOOM_JSON_URL из DevTools → Network."
        )
    elif note == _FETCH_NOTE_JSON_AUTH:
        body = (
            "BetBoom JSON endpoint требует cookie/header.\n"
            "Скопируйте headers из DevTools → Network → Copy as cURL "
            "и задайте BETBOOM_HEADERS_JSON в Railway."
        )
    elif note == _FETCH_NOTE_PARSE_ERROR:
        body = (
            "Источник BetBoom: ошибка парсинга JSON линии.\n"
            "Проверьте /debug_betboom_json и BETBOOM_JSON_URL."
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

    from config import BETBOOM_EMERGENCY_API_FALLBACK

    if BETBOOM_JSON_URL:
        log.info("BETBOOM_JSON_URL set — API-SPORTS fallback skipped")
        return [], note, bb.raw_found

    if not BETBOOM_API_FALLBACK and not BETBOOM_EMERGENCY_API_FALLBACK:
        log.info(
            "BETBOOM empty, API-SPORTS fallback disabled "
            "(BETBOOM_API_FALLBACK=0, BETBOOM_EMERGENCY_API_FALLBACK=0)"
        )
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

    log.warning(
        "BETBOOM empty — API-SPORTS emergency fallback (days=%s note=%s)",
        days_ahead,
        note,
    )
    api = await _merge_raw_safe_72h_events(days_ahead=days_ahead)
    if api.fetch_note in ("api_suspended", "api_rate_limit"):
        log.error("API-SPORTS fallback aborted: %s", api.fetch_note)
        return [], note, 0
    if api.events:
        return api.events, "api_sports_fallback", len(api.events)
    return [], note, 0
