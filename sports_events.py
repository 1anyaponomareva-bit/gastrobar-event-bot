"""
Спортивные события на неделю.

Позже: подключение API-SPORTS / TheSportsDB (SPORTS_API_KEY из .env).
Сейчас: заглушка + фильтрация «интересного для бара».
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, time as dtime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx

from config import SPORTS_API_KEY, TIMEZONE


def _today() -> date:
    """Календарная дата в часовом поясе бара (Railway контейнер часто UTC)."""
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def _week_dates() -> tuple[date, date]:
    start = _today()
    return start, start + timedelta(days=7)


_IMPORTANCE_ORDER = {"high": 0, "medium": 1, "low": 2}


def _interesting_keywords() -> tuple[re.Pattern[str], ...]:
    """Жёсткие и мягкие маркеры интересных турниров/этапов."""
    patterns = (
        r"champions\s*league|лига\s*чемпионов|ucl\b",
        r"world\s*cup|чм\b|mundial",
        r"\beuro\b|евро\s*\d{4}|чемпионат\s*европы",
        r"полуфинал|semifinal|semi-final|финал|final",
        r"дерби|derby",
        r"nba\s*playoffs|плей-?офф\s*nba",
        r"nhl\s*playoffs|плей-?офф\s*nhl|stanley\s*cup",
        r"main\s*card|ufc\s*\d+",
        r"formula\s*1|\bf1\b|гран-?при|grand\s*prix",
        r"qualifying|квалификац",
        r"nfl\s*playoffs|super\s*bowl|супербоул",
        r"wimbledon|roland\s*garros|us\s*open|australian\s*open|masters\s*\d{4}",
        r"\b(iem|esl|blast|valorant\s*champions|lol\s*worlds|the\s*international|ti\d+)\b",
        r"olympic|олимпи",
        r"wsop|world\s*series\s*of\s*poker|ept\b|покер.*финал",
    )
    return tuple(re.compile(p, re.I) for p in patterns)


_KEYWORD_PATTERNS = _interesting_keywords()


def _matches_interest(league: str, title: str, reason: str) -> bool:
    blob = f"{league} {title} {reason}"
    return any(p.search(blob) for p in _KEYWORD_PATTERNS)


def filter_guest_friendly(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Оставляем события, которые могут зайти гостям бара:
    топ-турниры, финалы/полуфиналы, дерби, плей-оффы, main card UFC, F1 и т.д.
    """
    out: list[dict[str, Any]] = []
    for e in events:
        imp = (e.get("importance") or "low").lower()
        league = str(e.get("league", ""))
        title = str(e.get("title", ""))
        reason = str(e.get("reason", ""))
        if imp == "high":
            out.append(e)
            continue
        if imp == "medium" and _matches_interest(league, title, reason):
            out.append(e)
            continue
        if _matches_interest(league, title, reason):
            out.append(e)
    # без дублей по (date, time, title)
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for e in out:
        key = (e.get("date", ""), e.get("time", ""), e.get("title", ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    unique.sort(
        key=lambda x: (
            x.get("date", ""),
            x.get("time", ""),
            _IMPORTANCE_ORDER.get((x.get("importance") or "low").lower(), 3),
        )
    )
    return unique


def _stub_raw_events() -> list[dict[str, Any]]:
    """Тестовые события на ~7 дней от сегодняшней даты."""
    d0 = _today()
    days = [d0 + timedelta(days=i) for i in range(8)]

    return [
        {
            "sport": "football",
            "title": "PSG — Bayern",
            "league": "UEFA Champions League",
            "date": days[0].isoformat(),
            "time": "02:00",
            "importance": "high",
            "reason": "полуфинал Лиги чемпионов",
        },
        {
            "sport": "football",
            "title": "Спартак — ЦСКА",
            "league": "РПЛ",
            "date": days[1].isoformat(),
            "time": "19:30",
            "importance": "medium",
            "reason": "московское дерби",
        },
        {
            "sport": "basketball",
            "title": "Lakers — Celtics",
            "league": "NBA Playoffs",
            "date": days[2].isoformat(),
            "time": "19:30",
            "importance": "high",
            "reason": "плей-офф NBA",
        },
        {
            "sport": "mma",
            "title": "UFC Fight Night: главная карта",
            "league": "UFC",
            "date": days[2].isoformat(),
            "time": "05:00",
            "importance": "medium",
            "reason": "main card",
        },
        {
            "sport": "hockey",
            "title": "Rangers — Hurricanes",
            "league": "NHL Playoffs",
            "date": days[3].isoformat(),
            "time": "03:00",
            "importance": "high",
            "reason": "плей-офф NHL",
        },
        {
            "sport": "formula1",
            "title": "Гран-при: гонка",
            "league": "Formula 1",
            "date": days[4].isoformat(),
            "time": "16:00",
            "importance": "high",
            "reason": "Formula 1 race",
        },
        {
            "sport": "tennis",
            "title": "Финал, мужской одиночный",
            "league": "Masters 1000",
            "date": days[5].isoformat(),
            "time": "18:00",
            "importance": "high",
            "reason": "крупный финал",
        },
        {
            "sport": "esports",
            "title": "BLAST Premier: полуфинал",
            "league": "CS2",
            "date": days[5].isoformat(),
            "time": "14:00",
            "importance": "medium",
            "reason": "крупный esports-турнир",
        },
        {
            "sport": "football",
            "title": "Случайный матч 12-го тура",
            "league": "Сегунда",
            "date": days[6].isoformat(),
            "time": "22:00",
            "importance": "low",
            "reason": "обычный тур",
        },
        {
            "sport": "boxing",
            "title": "Титульный бой вечера",
            "league": "Boxing",
            "date": days[6].isoformat(),
            "time": "07:00",
            "importance": "high",
            "reason": "топовый бой",
        },
        {
            "sport": "nfl",
            "title": "Chiefs — Bills",
            "league": "NFL Playoffs",
            "date": days[0].isoformat(),
            "time": "04:15",
            "importance": "high",
            "reason": "плей-офф NFL",
        },
        {
            "sport": "baseball",
            "title": "Yankees — Red Sox",
            "league": "MLB",
            "date": days[3].isoformat(),
            "time": "01:00",
            "importance": "medium",
            "reason": "дерби MLB",
        },
        {
            "sport": "poker",
            "title": "Финальный стол Main Event",
            "league": "WSOP",
            "date": days[4].isoformat(),
            "time": "23:00",
            "importance": "high",
            "reason": "финал крупного покерного турнира",
        },
        {
            "sport": "olympics",
            "title": "Финал, хоккей",
            "league": "Olympic Games",
            "date": days[5].isoformat(),
            "time": "15:30",
            "importance": "high",
            "reason": "олимпийский финал",
        },
    ]


log = logging.getLogger(__name__)
logger = log

TZ = ZoneInfo("Asia/Ho_Chi_Minh")

_EXCLUDE_KEYWORDS = (
    "concacaf champions league",
    "asia champions league",
    "basketball champions league",
    "u21",
    "u20",
    "u19",
    "u18",
    "youth",
    "reserve",
    "local cup",
    "regional cup",
    "regular season",
)

_TOP_EPL = (
    "manchester city",
    "man city",
    "manchester united",
    "man united",
    "liverpool",
    "arsenal",
    "chelsea",
    "tottenham",
    "spurs",
    "newcastle",
    "aston villa",
    "west ham",
    "brighton",
    "bournemouth",
)
_TOP_LALIGA = ("real madrid", "barcelona", "atletico", "atletico madrid")
_TOP_BUNDES = ("bayern", "dortmund", "leipzig", "leverkusen")
_TOP_SERIEA = ("inter", "ac milan", "milan", "juventus", "napoli", "roma", "atalanta")
_TOP_NBA = (
    "lakers",
    "celtics",
    "warriors",
    "knicks",
    "bulls",
    "mavericks",
    "nuggets",
    "heat",
)

_BOXING_TOP_NAMES = (
    "canelo",
    "tyson fury",
    "usyk",
    "anthony joshua",
    "bivol",
    "beterbiev",
    "gervonta",
)


def _parse_dt_to_local(dt_str: str) -> tuple[str, str]:
    """
    API обычно возвращает ISO datetime в UTC (или unix через _row_from_api_dt).
    Возвращаем (YYYY-MM-DD, HH:MM) в Asia/Ho_Chi_Minh.
    """
    from event_datetime_norm import normalize_event_datetime, utc_from_iso, utc_from_timestamp

    if not dt_str:
        return "", "00:00"
    s = str(dt_str).strip()
    utc_dt = utc_from_timestamp(s) if s.isdigit() else utc_from_iso(s)
    if utc_dt is None:
        try:
            d = date.fromisoformat(s[:10])
            utc_dt = datetime.combine(d, dtime(0, 0), tzinfo=ZoneInfo("UTC"))
        except Exception:
            return "", "00:00"
    local = utc_dt.astimezone(TZ)
    return local.date().isoformat(), local.strftime("%H:%M")


def _row_from_api_dt(
    *,
    dt_iso: str = "",
    timestamp: Any = None,
    timezone_name: str = "",
) -> dict[str, str | int | None]:
    """Поля datetime для raw row: timestamp — source of truth."""
    from event_datetime_norm import utc_from_iso, utc_from_timestamp

    utc_dt = None
    if timestamp is not None and timestamp != "":
        utc_dt = utc_from_timestamp(timestamp)
    if utc_dt is None and dt_iso:
        utc_dt = utc_from_iso(str(dt_iso))
    out: dict[str, str | int | None] = {
        "fixture_utc_iso": str(dt_iso) if dt_iso else "",
        "fixture_timestamp": None,
        "api_timezone": str(timezone_name or "").strip() or None,
    }
    if utc_dt is not None:
        local = utc_dt.astimezone(TZ)
        out["date"] = local.date().isoformat()
        out["time"] = local.strftime("%H:%M")
        out["fixture_utc_iso"] = utc_dt.isoformat()
        if timestamp is not None and timestamp != "":
            try:
                out["fixture_timestamp"] = int(timestamp)
            except (TypeError, ValueError):
                pass
    elif dt_iso:
        d_str, t_str = _parse_dt_to_local(dt_iso)
        out["date"] = d_str
        out["time"] = t_str
    return out


def _clean_league_name(league: str) -> str:
    # Срезаем шум типа "Regular Season - 21"
    s = re.sub(r"(?i)\bregular season\b.*", "", league).strip(" -")
    s = re.sub(r"(?i)\bgroup stage\b.*", "", s).strip(" -")
    s = re.sub(r"(?i)\bquarter-?finals?\b.*", "", s).strip(" -")
    s = re.sub(r"\s{2,}", " ", s)
    return s or league


def _event_blob(e: dict[str, Any]) -> str:
    return f"{e.get('title', '')} {e.get('league', '')}".lower()


def _is_excluded_event(e: dict[str, Any]) -> bool:
    blob = _event_blob(e)
    return any(k in blob for k in _EXCLUDE_KEYWORDS)


def _contains_any(blob: str, words: tuple[str, ...]) -> bool:
    return any(w in blob for w in words)


def _is_top_football_match(blob: str) -> bool:
    return _contains_any(blob, _TOP_EPL + _TOP_LALIGA + _TOP_BUNDES + _TOP_SERIEA)


def _is_priority_event(e: dict[str, Any]) -> bool:
    sport = str(e.get("sport", "")).lower()
    blob = _event_blob(e)

    if sport == "football":
        if "uefa champions league" in blob:
            return True
        if "uefa europa league" in blob:
            return True
        if "uefa europa conference league" in blob:
            return True
        if "fifa world cup" in blob or "uefa euro" in blob:
            return True
        if "premier league" in blob:
            if _contains_any(blob, _TOP_EPL):
                return True
            if re.search(r"matchday\s+38|round\s+38|final\s+day", blob):
                return True
        if ("la liga" in blob or "laliga" in blob) and _contains_any(blob, _TOP_LALIGA):
            return True
        if "bundesliga" in blob and _contains_any(blob, _TOP_BUNDES):
            return True
        if "serie a" in blob and _contains_any(blob, _TOP_SERIEA):
            return True
        if "ligue 1" in blob and _contains_any(
            blob, ("psg", "paris", "marseille", "lyon", "monaco")
        ):
            return True
        if _is_top_clash(blob):
            return True
        return False

    if sport == "basketball":
        if "nba" not in blob:
            return False
        if "playoffs" in blob or "finals" in blob:
            return True
        return _contains_any(blob, _TOP_NBA)

    if sport == "hockey":
        if "nhl" in blob and ("playoffs" in blob or "stanley cup" in blob):
            return True
        if _is_world_championship_hockey(e):
            return True
        return False

    if sport == "formula1":
        return ("formula 1" in blob) or ("grand prix" in blob) or ("qualifying" in blob) or ("race" in blob)

    if sport == "mma":
        return ("ufc" in blob) or ("fight night" in blob) or ("main card" in blob)

    if sport == "boxing":
        return ("title fight" in blob) or ("championship" in blob) or _contains_any(blob, _BOXING_TOP_NAMES)

    if sport == "tennis":
        if not any(gs in blob for gs in ("wimbledon", "roland garros", "us open", "australian open")):
            return False
        return ("final" in blob) or ("semi-final" in blob) or ("semifinal" in blob)

    return False


def _importance_score(e: dict[str, Any]) -> str:
    blob = _event_blob(e)
    if "uefa champions league" in blob:
        return "high"
    if "ufc" in blob:
        return "high"
    if "formula 1" in blob or "grand prix" in blob or "qualifying" in blob:
        return "high"
    if "nba" in blob and ("playoffs" in blob or "finals" in blob):
        return "high"
    if "fifa world cup" in blob:
        return "high"
    if "uefa europa league" in blob:
        return "medium"
    if "nhl" in blob and ("playoffs" in blob or "stanley cup" in blob):
        return "medium"
    return "medium"


def _priority_rank(e: dict[str, Any]) -> int:
    blob = _event_blob(e)
    if "uefa champions league" in blob:
        return 0
    if "ufc" in blob:
        return 1
    if "formula 1" in blob or "grand prix" in blob or "qualifying" in blob:
        return 2
    if "nba" in blob and ("playoffs" in blob or "finals" in blob):
        return 3
    if "uefa europa league" in blob:
        return 4
    if "nhl" in blob and ("playoffs" in blob or "stanley cup" in blob):
        return 5
    return 6


def _curate_for_gastrobar(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    curated: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for e in events:
        if _is_excluded_event(e):
            continue
        if not _is_priority_event(e):
            continue
        x = dict(e)
        x["league"] = _clean_league_name(str(x.get("league", "")))
        x["importance"] = _importance_score(x)
        key = (str(x.get("date", "")), str(x.get("time", "")), str(x.get("title", "")))
        if key in seen:
            continue
        seen.add(key)
        curated.append(x)

    curated.sort(
        key=lambda x: (
            _priority_rank(x),
            str(x.get("date", "")),
            str(x.get("time", "")),
        )
    )
    return curated[:7]


# --- Редакторская программа афиши Gastrobar (не «дамп API») ---

AFISHA_MAX_ITEMS = 6

_BIG_CLUB_KEYS = (
    "bayern",
    "munich",
    "psg",
    "paris",
    "real madrid",
    "real ",
    "barcelona",
    "barca",
    "manchester city",
    "manchester united",
    "man city",
    "man united",
    "liverpool",
    "arsenal",
    "chelsea",
    "tottenham",
    "juventus",
    "inter",
    "ac milan",
    "milan",
    "napoli",
    "atalanta",
    "roma",
    "lazio",
    "atletico",
    "dortmund",
    "leipzig",
    "benfica",
    "porto",
    "ajax",
    "psv",
    "celtic",
    "rangers",
    "galatasaray",
    "monaco",
    "lyon",
    "sevilla",
    "villarreal",
    "sporting cp",
    "shakhtar",
)


def _fragment_maybe_big_club(s: str) -> bool:
    t = s.lower().strip()
    if len(t) < 2:
        return False
    return any(k in t for k in _BIG_CLUB_KEYS)


def _is_top_clash(title_l: str) -> bool:
    parts = re.split(r"\s+vs\.?\s+|\s+—\s+", title_l, maxsplit=1, flags=re.I)
    if len(parts) < 2:
        return False
    return _fragment_maybe_big_club(parts[0]) and _fragment_maybe_big_club(parts[1])


def _football_competition_tier(league_l: str) -> Literal["ucl", "uel", "uecl"] | None:
    if "concacaf" in league_l:
        return None
    if "europa conference league" in league_l or (
        "conference league" in league_l and "uefa" in league_l
    ):
        return "uecl"
    if "europa league" in league_l:
        return "uel"
    if "champions league" in league_l:
        return "ucl"
    return None


def _football_should_ignore(e: dict[str, Any]) -> bool:
    league_l = (e.get("league") or "").lower()
    title_l = (e.get("title") or "").lower()
    blob = f"{league_l} {title_l}"

    if "concacaf" in blob:
        return True
    if "asia champions league" in blob or "afc champions" in blob:
        return True
    if "u21" in blob or "u19" in blob or "u18" in blob:
        return True
    if "youth" in blob:
        return True
    if "local cup" in blob or "regional cup" in blob:
        return True
    if "women" in blob or "female" in blob:
        return True
    if any(x in blob for x in ("semi-final", "semifinal", "quarter-final")):
        if _football_competition_tier(league_l) is None:
            return True
    if "regular season" in league_l and _football_competition_tier(league_l) is None:
        return True
    return False


def _football_match_editor_worthy(e: dict[str, Any], _comp: str) -> bool:
    league_l = (e.get("league") or "").lower()
    title_l = (e.get("title") or "").lower()

    if "group" in league_l and "stage" in league_l:
        return _is_top_clash(title_l)
    return True


def _league_label_ru(league: str) -> str:
    ll = league.lower()
    if "champions league" in ll:
        return "Лига чемпионов"
    if "europa conference league" in ll:
        return "Лига конференций"
    if "europa league" in ll:
        return "Лига Европы"
    return league


def _pretty_match_title(title: str) -> str:
    return title.replace(" vs ", " — ").replace(" Vs ", " — ").strip()


def _match_item_from_football(e: dict[str, Any], tier: str) -> dict[str, Any]:
    league_raw = _clean_league_name(str(e.get("league", "")))
    title = _pretty_match_title(str(e.get("title", "")))
    item: dict[str, Any] = {
        "kind": "match",
        "sport": "football",
        "title": title,
        "league_label_ru": _league_label_ru(league_raw),
        "league_raw": league_raw,
        "date": str(e.get("date", "")),
        "time": str(e.get("time", "")),
        "tier": tier,
    }
    iso = str(e.get("fixture_utc_iso") or "").strip()
    if iso:
        item["fixture_utc_iso"] = iso
    if e.get("fixture_timestamp") is not None:
        item["fixture_timestamp"] = e.get("fixture_timestamp")
    return item


def _editor_block_defs() -> dict[str, dict[str, Any]]:
    return {
        "nba_playoffs": {
            "kind": "block",
            "key": "nba_playoffs",
            "emoji": "🏀",
            "line": "NBA Playoffs",
            "tier": "high",
        },
        "nhl_stanley": {
            "kind": "block",
            "key": "nhl_stanley",
            "emoji": "🏒",
            "line": "Stanley Cup Playoffs",
            "tier": "high",
        },
        "ufc": {
            "kind": "block",
            "key": "ufc",
            "emoji": "🥊",
            "line": "UFC Fight Night",
            "tier": "high",
        },
        "f1_gp": {
            "kind": "block",
            "key": "f1_gp",
            "emoji": "🏎",
            "line": "Formula 1 Grand Prix",
            "tier": "high",
        },
    }


def _week_signals_nba_playoffs(merged: list[dict[str, Any]]) -> bool:
    for e in merged:
        if str(e.get("sport", "")).lower() != "basketball":
            continue
        ll = (e.get("league") or "").lower()
        tl = (e.get("title") or "").lower()
        bl = f"{ll} {tl}"
        if "nba" not in ll and "nba" not in tl:
            continue
        if "regular season" in ll and "playoff" not in bl:
            continue
        if "playoff" in bl or "finals" in bl or "final four" in bl:
            return True
        if "playoffs" in ll:
            return True
    return False


def _week_signals_nhl_playoffs(merged: list[dict[str, Any]]) -> bool:
    for e in merged:
        if str(e.get("sport", "")).lower() != "hockey":
            continue
        ll = (e.get("league") or "").lower()
        tl = (e.get("title") or "").lower()
        bl = f"{ll} {tl}"
        if "nhl" not in bl:
            continue
        if "regular season" in ll and "playoff" not in bl and "stanley" not in bl:
            continue
        if "playoff" in bl or "stanley" in bl:
            return True
        if "playoffs" in ll:
            return True
    return False


def _week_signals_ufc(merged: list[dict[str, Any]]) -> bool:
    for e in merged:
        sp = str(e.get("sport", "")).lower()
        blob = f"{e.get('title', '')} {e.get('league', '')}".lower()
        if sp in ("mma", "ufc") or "ufc" in blob:
            return True
    return False


def _week_signals_f1(merged: list[dict[str, Any]]) -> bool:
    for e in merged:
        sp = str(e.get("sport", "")).lower()
        ll = (e.get("league") or "").lower()
        if sp in ("formula1", "f1") or "formula 1" in ll or "grand prix" in ll:
            return True
    return False


def _rank_ucl_match(e: dict[str, Any]) -> tuple[int, str, str]:
    league_l = (e.get("league") or "").lower()
    bump = 0
    if any(k in league_l for k in ("final", "semi", "quarter", "knockout", "round of 16")):
        bump = -1
    return (bump, str(e.get("date", "")), str(e.get("time", "")))


def build_gastrobar_weekly_program(
    merged: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    blocks = _editor_block_defs()

    ucl: list[dict[str, Any]] = []
    uel_med: list[dict[str, Any]] = []

    for e in merged:
        if str(e.get("sport", "")).lower() != "football":
            continue
        if _football_should_ignore(e):
            continue
        league_l = (e.get("league") or "").lower()
        comp = _football_competition_tier(league_l)
        if comp == "ucl":
            if _football_match_editor_worthy(e, "ucl"):
                ucl.append(dict(e))
        elif comp in ("uel", "uecl"):
            if _football_match_editor_worthy(e, comp):
                uel_med.append(dict(e))

    ucl.sort(key=_rank_ucl_match)
    uel_med.sort(
        key=lambda x: (str(x.get("date", "")), str(x.get("time", ""))),
    )

    seen_ucl: set[tuple[str, str, str]] = set()
    ucl_unique: list[dict[str, Any]] = []
    for e in ucl:
        key = (str(e.get("date", "")), str(e.get("time", "")), str(e.get("title", "")))
        if key in seen_ucl:
            continue
        seen_ucl.add(key)
        ucl_unique.append(e)

    seen_um: set[tuple[str, str, str]] = set()
    uel_unique: list[dict[str, Any]] = []
    for e in uel_med:
        key = (str(e.get("date", "")), str(e.get("time", "")), str(e.get("title", "")))
        if key in seen_um:
            continue
        seen_um.add(key)
        uel_unique.append(e)

    want_nba = _week_signals_nba_playoffs(merged)
    want_nhl = _week_signals_nhl_playoffs(merged)
    want_ufc = _week_signals_ufc(merged)
    want_f1 = _week_signals_f1(merged)

    out: list[dict[str, Any]] = []

    for e in ucl_unique[:2]:
        out.append(_match_item_from_football(e, "high"))

    if want_nba:
        out.append(dict(blocks["nba_playoffs"]))
    if want_nhl:
        out.append(dict(blocks["nhl_stanley"]))
    if want_ufc:
        out.append(dict(blocks["ufc"]))
    if want_f1:
        out.append(dict(blocks["f1_gp"]))

    for e in uel_unique:
        if len(out) >= AFISHA_MAX_ITEMS:
            break
        out.append(_match_item_from_football(e, "medium"))

    for e in ucl_unique[2:]:
        if len(out) >= AFISHA_MAX_ITEMS:
            break
        out.append(_match_item_from_football(e, "high"))

    return out[:AFISHA_MAX_ITEMS]


# --- Weekly Event Radar: полный API-пул (без лимита 6 и без block-заглушек) ---

WEEKLY_FOOTBALL_MIN_WATCHABILITY = 18

# API-SPORTS league ids: UCL / UEL / Conference; Championship (England tier 2)
_WEEKLY_UEFA_LEAGUE_IDS = frozenset({2, 3, 848})
_CHAMPIONSHIP_ENGLAND_LEAGUE_ID = 40

_WORLD_HOCKEY_NATIONS = (
    "canada",
    "usa",
    "united states",
    "finland",
    "sweden",
    "czech",
    "czechia",
    "italy",
    "switzerland",
    "germany",
    "latvia",
    "slovakia",
    "norway",
    "denmark",
    "france",
    "austria",
    "great britain",
    "united kingdom",
    "uk",
    "poland",
    "hungary",
    "kazakhstan",
)

_HOCKEY_KHL_RE = re.compile(
    r"\b(khl\b|kontinental\s+hockey|ак\s+барс|lokomotiv\s+yaroslavl)\b",
    re.I,
)
_HOCKEY_NHL_RE = re.compile(
    r"\b(nhl\b|national\s+hockey\s+league|stanley\s+cup)\b",
    re.I,
)
_HOCKEY_WORLD_RE = re.compile(
    r"\b(world\s+championship|iihf|world\s+cup.*hockey|championship.*hockey)\b",
    re.I,
)

_TENNIS_TIER_RE = re.compile(
    r"\b(atp\s*500|wta\s*500|atp\s*1000|wta\s*1000|masters\s+1000|"
    r"roland\s+garros|wimbledon|us\s+open|australian\s+open|indian\s+wells|"
    r"miami\s+open|monte\s+carlo|madrid\s+open|rome|hamburg|cincinnati|shanghai)\b",
    re.I,
)
_TENNIS_KNOWN_PLAYERS = (
    "djokovic",
    "nadal",
    "federer",
    "alcaraz",
    "sinner",
    "medvedev",
    "zverev",
    "rublev",
    "tsitsipas",
    "ruud",
    "fritz",
    "paul",
    "altmaier",
    "shelton",
    "tiafoe",
    "pegula",
    "sabalenka",
    "swiatek",
    "gauff",
    "rybakina",
)


def _is_world_championship_hockey(e: dict[str, Any]) -> bool:
    blob = _event_blob(e)
    if str(e.get("sport", "")).lower() != "hockey":
        return False
    if re.search(
        r"world\s+championship|iihf|world\s+cup.*hockey|championship.*hockey",
        blob,
        re.I,
    ):
        return True
    title = str(e.get("title", "")).lower()
    if not re.search(r"\s+vs\.?\s+|\s+—\s+|\s+–\s+", title):
        return False
    nations = sum(1 for n in _WORLD_HOCKEY_NATIONS if n in title or n in blob)
    return nations >= 1


def _has_matchup_title(title: str) -> bool:
    return bool(
        re.search(r"\s+vs\.?\s+|\s+—\s+|\s+–\s+", str(title or ""), flags=re.I)
    )


def classify_hockey_bucket(e: dict[str, Any]) -> str:
    """khl | nhl | world_hockey | hockey (generic)."""
    if str(e.get("sport", "")).lower() != "hockey":
        return ""
    blob = _event_blob(e)
    if _HOCKEY_KHL_RE.search(blob):
        return "khl"
    if _HOCKEY_NHL_RE.search(blob):
        return "nhl"
    if _HOCKEY_WORLD_RE.search(blob) or _is_world_championship_hockey(e):
        return "world_hockey"
    return "hockey"


def _log_found_breakdown(merged: list[dict[str, Any]], *, label: str) -> None:
    fb = hk = nba = f1 = esp = mma = tennis = other = 0
    khl = nhl = world_hk = 0
    for row in merged:
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
                world_hk += 1
        elif sp == "basketball":
            nba += 1
        elif sp in ("formula1", "f1"):
            f1 += 1
        elif sp == "esports":
            esp += 1
        elif sp in ("mma", "boxing"):
            mma += 1
        elif sp == "tennis":
            tennis += 1
        else:
            other += 1
    log.info(
        "%s RADAR_RAW_TOTAL=%s FOOTBALL_FOUND=%s HOCKEY_FOUND=%s KHL_FOUND=%s "
        "NHL_FOUND=%s WORLD_HOCKEY_FOUND=%s ESPORTS_FOUND=%s F1_FOUND=%s "
        "NBA_FOUND=%s TENNIS_FOUND=%s MMA_BOXING_FOUND=%s OTHER=%s",
        label,
        len(merged),
        fb,
        hk,
        khl,
        nhl,
        world_hk,
        esp,
        f1,
        nba,
        tennis,
        mma,
        other,
    )


def _formula_session_dt_iso(block: dict[str, Any]) -> str:
    d = block.get("date")
    t = block.get("time")
    if isinstance(d, str) and ("T" in d or len(d) > 12):
        return d
    if isinstance(d, str) and d and t:
        ts = str(t).strip()
        if re.match(r"^\d{2}:\d{2}", ts):
            return f"{d[:10]}T{ts}"
        return f"{d[:10]}T{ts}"
    if isinstance(d, str):
        return d
    return ""


def _expand_formula_api_item(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Одна строка /races → несколько (Practice / Qualifying / Sprint / Race), если есть в JSON."""
    rows: list[dict[str, Any]] = []

    def gp_label() -> str:
        return str(
            item.get("raceName")
            or item.get("name")
            or (item.get("competition") or {}).get("name")
            or (item.get("circuit") or {}).get("name")
            or item.get("grandPrix")
            or item.get("grand_prix")
            or "Grand Prix",
        ).strip()

    gp = gp_label()

    session_map: tuple[tuple[str, str], ...] = (
        ("firstPractice", "Practice 1"),
        ("secondPractice", "Practice 2"),
        ("thirdPractice", "Practice 3"),
        ("FirstPractice", "Practice 1"),
        ("SecondPractice", "Practice 2"),
        ("ThirdPractice", "Practice 3"),
        ("fp1", "Practice 1"),
        ("fp2", "Practice 2"),
        ("fp3", "Practice 3"),
        ("sprintQualifying", "Sprint Qualifying"),
        ("SprintQualifying", "Sprint Qualifying"),
        ("sprint", "Sprint"),
        ("Sprint", "Sprint"),
        ("qualifying", "Qualifying"),
        ("Qualifying", "Qualifying"),
        ("race", "Race"),
        ("Race", "Race"),
    )

    def append_row(dt_iso: str, sess: str) -> None:
        dt_iso = str(dt_iso or "").strip()
        if not dt_iso:
            return
        d_str, t_str = _parse_dt_to_local(dt_iso)
        if not d_str:
            return
        title = f"F1 {gp} - {sess}"
        row: dict[str, Any] = {
            "sport": "formula1",
            "title": title,
            "league": "Formula 1",
            "date": d_str,
            "time": t_str,
            "importance": "high" if sess == "Race" else "medium",
            "source": "API-SPORTS",
            "fixture_utc_iso": dt_iso,
        }
        rows.append(row)

    for key, label in session_map:
        block = item.get(key)
        if isinstance(block, dict):
            append_row(_formula_session_dt_iso(block), label)

    if not rows:
        sched = item.get("schedule") or item.get("sessions")
        if isinstance(sched, dict):
            for k, v in sched.items():
                if not isinstance(v, dict):
                    continue
                dt_raw = _formula_session_dt_iso(v)
                if dt_raw:
                    kl = re.sub(r"[_-]+", " ", str(k)).strip().title() or "Session"
                    append_row(dt_raw, kl)

    if not rows:
        dt_iso = item.get("date") or item.get("time") or ""
        append_row(str(dt_iso), "Race")

    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for r in rows:
        key = (r["date"], r["time"], r["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return unique


def is_gastrobar_api_sport_worthy(e: dict[str, Any]) -> bool:
    """Единый фильтр API-матчей для weekly, now24 и daily."""
    if _is_excluded_event(e):
        return False
    sport = str(e.get("sport", "")).lower()
    blob = _event_blob(e)
    title = str(e.get("title", ""))
    league = str(e.get("league", "")).lower()

    if sport == "football":
        if not _has_matchup_title(title):
            return False
        from football_watchability import _UEFA_CUPS, is_eligible_football_league_now24

        if is_eligible_football_league_now24(e):
            return True
        try:
            lid = int(e.get("league_id") or 0)
        except (TypeError, ValueError):
            lid = 0
        if lid in _UEFA_CUPS:
            return True
        return False

    if sport == "hockey":
        if not _has_matchup_title(title):
            return False
        blob = _event_blob(e)
        if _HOCKEY_WORLD_RE.search(blob) or _is_world_championship_hockey(e):
            return True
        if _HOCKEY_KHL_RE.search(blob):
            return True
        if _HOCKEY_NHL_RE.search(blob):
            return True
        if re.search(r"\bplayoff|play-off|finals?\b", blob, re.I):
            return True
        tl = title.lower()
        nations = sum(1 for n in _WORLD_HOCKEY_NATIONS if n in tl or n in blob)
        return nations >= 2

    if sport == "tennis":
        if not _has_matchup_title(title):
            return False
        blob = _event_blob(e)
        if _TENNIS_TIER_RE.search(blob):
            return True
        stars = sum(1 for p in _TENNIS_KNOWN_PLAYERS if p in blob)
        return stars >= 1

    if sport == "basketball":
        if "nba" not in blob:
            return False
        if "regular season" in blob and "playoff" not in blob and "finals" not in blob:
            return False
        if "playoff" in blob or "finals" in blob or "final four" in blob:
            return _has_matchup_title(title)
        return _contains_any(blob, _TOP_NBA) and _has_matchup_title(title)

    if sport == "formula1":
        return bool(title.strip())

    if sport == "esports":
        if not (_has_matchup_title(title) or re.search(r"\bvs\.?\b", title, re.I)):
            return False
        if _matches_interest(str(e.get("league", "")), title, ""):
            return True
        b = blob
        return bool(
            re.search(
                r"\b(cs2|counter-strike|dota|lol|league\s+legends|valorant|dreamleague|"
                r"dream\s+league|iem\b|esl\b|blast|major|msi|worlds|pgl|betboom|asian)",
                b,
                re.I,
            )
        )

    if sport == "mma":
        return "ufc" in blob and _has_matchup_title(title)

    if sport == "boxing":
        return _is_priority_event(e) and _has_matchup_title(title)

    return _is_priority_event(e)


def is_weekly_radar_api_worthy(e: dict[str, Any]) -> bool:
    """Алиас: weekly и daily используют один API-фильтр."""
    return is_gastrobar_api_sport_worthy(e)


def raw_event_to_radar_program_item(e: dict[str, Any]) -> dict[str, Any]:
    """Плоский match-item для radar (не editor block)."""
    sport = str(e.get("sport", "misc")).lower()
    league_raw = _clean_league_name(str(e.get("league", "")))
    title = _pretty_match_title(str(e.get("title", "")))
    tier = str(e.get("importance", "medium")).lower()
    if tier not in ("high", "medium", "low"):
        tier = "medium"
    emoji_by_sport = {
        "football": "⚽",
        "hockey": "🏒",
        "basketball": "🏀",
        "formula1": "🏎",
        "esports": "🎮",
        "tennis": "🎾",
        "mma": "🥊",
        "boxing": "🥊",
    }
    item: dict[str, Any] = {
        "kind": "match",
        "sport": sport,
        "title": title,
        "league_label_ru": _league_label_ru(league_raw) if sport == "football" else league_raw,
        "league_raw": league_raw,
        "date": str(e.get("date", "")),
        "time": str(e.get("time", "")),
        "tier": "high" if tier == "high" else "medium",
        "emoji": emoji_by_sport.get(sport, "🏟"),
    }
    iso = str(e.get("fixture_utc_iso") or "").strip()
    if iso:
        item["fixture_utc_iso"] = iso
    if e.get("fixture_timestamp") is not None:
        item["fixture_timestamp"] = e.get("fixture_timestamp")
    if e.get("api_timezone"):
        item["api_timezone"] = e.get("api_timezone")
    if e.get("league_id") is not None:
        item["league_id"] = e.get("league_id")
    if e.get("league_country"):
        item["league_country"] = e.get("league_country")
    return item


def build_weekly_radar_api_pool(merged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Все API-матчи недели, прошедшие gastrobar-фильтр (без top-N)."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    stats = {
        "raw": len(merged),
        "worthy": 0,
        "football": 0,
        "hockey": 0,
        "nba": 0,
        "f1": 0,
        "esports": 0,
    }

    for e in merged:
        if not is_gastrobar_api_sport_worthy(e):
            continue
        stats["worthy"] += 1
        sp = str(e.get("sport", "")).lower()
        if sp == "football":
            stats["football"] += 1
        elif sp == "hockey":
            stats["hockey"] += 1
        elif sp == "basketball":
            stats["nba"] += 1
        elif sp == "formula1":
            stats["f1"] += 1
        elif sp == "esports":
            stats["esports"] += 1
        key = (str(e.get("date", "")), str(e.get("time", "")), str(e.get("title", "")))
        if key in seen:
            continue
        seen.add(key)
        out.append(raw_event_to_radar_program_item(e))

    out.sort(
        key=lambda x: (
            str(x.get("date", "")),
            str(x.get("time", "")),
            str(x.get("sport", "")),
        )
    )
    log.info(
        "weekly radar API pool: raw=%s worthy=%s items=%s stats=%s",
        stats["raw"],
        stats["worthy"],
        len(out),
        stats,
    )
    return out


async def get_week_radar_pool_with_stats() -> tuple[list[dict[str, Any]], int, int]:
    """Полный API-пул для Event Radar week (не редакторская программа на 6 пунктов)."""
    merged = await _merge_raw_week_events()
    pool = build_weekly_radar_api_pool(merged)
    log.info(
        "RADAR_WEEKLY POOL_FINAL=%s (raw_merge=%s): "
        "см. строку RADAR_MERGE_AFTER_FETCH и weekly radar API pool stats",
        len(pool),
        len(merged),
    )
    return pool, len(merged), len(pool)


def editor_program_to_legacy_events(
    program: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Плоский список для scheduler / replace_week_events (совместимость)."""
    legacy: list[dict[str, Any]] = []
    sport_by_key = {
        "nba_playoffs": "basketball",
        "nhl_stanley": "hockey",
        "ufc": "mma",
        "f1_gp": "formula1",
    }
    for item in program:
        if item.get("kind") == "block":
            key = str(item.get("key", ""))
            line = str(item.get("line", ""))
            legacy.append(
                {
                    "sport": sport_by_key.get(key, "misc"),
                    "title": line,
                    "league": line,
                    "date": _today().isoformat(),
                    "time": "",
                    "importance": str(item.get("tier", "high")),
                    "editor_block": True,
                }
            )
        else:
            legacy.append(
                {
                    "sport": "football",
                    "title": item.get("title", ""),
                    "league": item.get("league_raw")
                    or item.get("league_label_ru", ""),
                    "date": item.get("date", ""),
                    "time": item.get("time", ""),
                    "importance": str(item.get("tier", "high")),
                    "editor_block": False,
                }
            )
    return legacy


def build_weekly_program_with_stats(
    merged: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    raw_total = len(merged)
    program = build_gastrobar_weekly_program(merged)
    logger.info("Gastrobar editor program (%s items): %s", len(program), program)
    return program, raw_total, len(program)


def _reraise_api_sports(e: BaseException) -> None:
    from api_sports_status import ApiSportsError

    if isinstance(e, ApiSportsError):
        raise e


async def _collect_days_sequential(
    one_day,
    *,
    days_ahead: int,
    sport_label: str,
) -> list[dict[str, Any]]:
    """По одному дню за раз (без parallel burst). Throttle в _get_json."""
    start = _today()
    n_days = max(1, days_ahead)
    events: list[dict[str, Any]] = []
    for i in range(n_days):
        d = start + timedelta(days=i)
        try:
            day_events = await one_day(d)
            if day_events:
                events.extend(day_events)
        except Exception as e:
            _reraise_api_sports(e)
            log.error("%s API day %s failed: %s", sport_label, d.isoformat(), e)
    return events


async def _get_json(url: str, *, headers: dict[str, str], timeout: float = 15.0) -> dict[str, Any]:
    from api_sports_status import ApiSportsError, classify_errors_payload
    from api_sports_throttle import throttle_before_request

    await throttle_before_request()
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, headers=headers)
    if r.status_code == 429:
        raise ApiSportsError("rateLimit", f"HTTP 429: {r.text[:200]}", sport="")
    if r.status_code != 200:
        raise ApiSportsError("unavailable", f"HTTP {r.status_code}: {r.text[:300]}", sport="")
    data = r.json()
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected JSON payload")
    errs = data.get("errors") or {}
    health, detail = classify_errors_payload(errs)
    if health != "ok":
        log.warning(
            "API-SPORTS errors for %s: health=%s detail=%s results=%s",
            url,
            health,
            detail,
            data.get("results"),
        )
        raise ApiSportsError(health, detail or str(errs), sport="")
    return data


async def get_football_events_next_days_vn(*, days_ahead: int = 2) -> list[dict[str, Any]]:
    """Футбол на сегодня + следующие дни (календарь VN) — для now24."""
    if not SPORTS_API_KEY:
        return []

    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    start = datetime.now(tz).date()
    dates = [start + timedelta(days=i) for i in range(max(1, days_ahead))]

    headers = {"x-apisports-key": SPORTS_API_KEY}
    base = "https://v3.football.api-sports.io"

    async def one_day(d: date) -> list[dict[str, Any]]:
        url = f"{base}/fixtures?date={d.isoformat()}"
        log.info("Football endpoint (now24): %s", url)
        try:
            data = await _get_json(url, headers=headers)
        except Exception as e:
            _reraise_api_sports(e)
            log.error("Football day failed (%s): %s", d.isoformat(), e)
            return []

        resp = data.get("response") or []
        if not isinstance(resp, list):
            return []
        log.info("Football fixtures on %s: %s", d.isoformat(), len(resp))

        day_events: list[dict[str, Any]] = []
        for item in resp:
            fixture = item.get("fixture") or {}
            league = item.get("league") or {}
            teams = item.get("teams") or {}
            home = (teams.get("home") or {}).get("name") or ""
            away = (teams.get("away") or {}).get("name") or ""
            league_name = league.get("name") or ""
            league_country = str(league.get("country") or "").strip()
            league_id = league.get("id")
            round_name = league.get("round") or ""
            league_full = league_name + (f" ({round_name})" if round_name else "")
            title = f"{home} vs {away}".strip(" vs").strip()
            dt_iso = str(fixture.get("date") or "")
            dt_fields = _row_from_api_dt(
                dt_iso=dt_iso,
                timestamp=fixture.get("timestamp"),
                timezone_name=str(fixture.get("timezone") or ""),
            )
            if not dt_fields.get("date"):
                continue
            day_events.append(
                {
                    "sport": "football",
                    "title": title or "Match",
                    "league": league_full or league_name or "Football",
                    "home": home,
                    "away": away,
                    "league_id": league_id,
                    "league_country": league_country,
                    "date": dt_fields["date"],
                    "time": dt_fields["time"],
                    "fixture_utc_iso": dt_fields.get("fixture_utc_iso") or dt_iso,
                    "fixture_timestamp": dt_fields.get("fixture_timestamp"),
                    "api_timezone": dt_fields.get("api_timezone"),
                    "importance": "low",
                    "source": "API-SPORTS",
                }
            )
        return day_events

    events: list[dict[str, Any]] = []
    for d in dates:
        try:
            events.extend(await one_day(d))
        except Exception as e:
            _reraise_api_sports(e)
            log.error("Football now24 day failed (%s): %s", d.isoformat(), e)
    return events


async def get_football_events(*, days_ahead: int = 3) -> list[dict[str, Any]]:
    """
    Football API-SPORTS: события на ближайшие days_ahead дней (SAFE_MODE, sequential).
    """
    if not SPORTS_API_KEY:
        return []

    start = _today()
    headers = {"x-apisports-key": SPORTS_API_KEY}
    base = "https://v3.football.api-sports.io"

    async def one_day(d: date) -> list[dict[str, Any]]:
        url = f"{base}/fixtures?date={d.isoformat()}"
        log.info("Football endpoint: %s", url)
        try:
            data = await _get_json(url, headers=headers)
        except Exception as e:
            _reraise_api_sports(e)
            log.error("Football day failed (%s): %s", d.isoformat(), e)
            return []

        resp = data.get("response") or []
        if not isinstance(resp, list):
            return []
        log.info("Football fixtures on %s: %s", d.isoformat(), len(resp))

        day_events: list[dict[str, Any]] = []
        for item in resp:
            fixture = item.get("fixture") or {}
            league = item.get("league") or {}
            teams = item.get("teams") or {}
            home = (teams.get("home") or {}).get("name") or ""
            away = (teams.get("away") or {}).get("name") or ""
            league_name = league.get("name") or ""
            league_country = str(league.get("country") or "").strip()
            league_id = league.get("id")
            round_name = league.get("round") or ""
            league_full = league_name + (f" ({round_name})" if round_name else "")
            title = f"{home} vs {away}".strip(" vs").strip()
            dt_iso = str(fixture.get("date") or "")
            dt_fields = _row_from_api_dt(
                dt_iso=dt_iso,
                timestamp=fixture.get("timestamp"),
                timezone_name=str(fixture.get("timezone") or ""),
            )
            if not dt_fields.get("date"):
                continue
            day_events.append(
                {
                    "sport": "football",
                    "title": title or "Match",
                    "league": league_full or league_name or "Football",
                    "home": home,
                    "away": away,
                    "league_id": league_id,
                    "league_country": league_country,
                    "date": dt_fields["date"],
                    "time": dt_fields["time"],
                    "fixture_utc_iso": dt_fields.get("fixture_utc_iso") or dt_iso,
                    "fixture_timestamp": dt_fields.get("fixture_timestamp"),
                    "api_timezone": dt_fields.get("api_timezone"),
                    "importance": "low",
                    "source": "API-SPORTS",
                }
            )
        return day_events

    return await _collect_days_sequential(
        one_day, days_ahead=days_ahead, sport_label="football"
    )


async def get_basketball_events(*, days_ahead: int = 3) -> list[dict[str, Any]]:
    """
    Basketball API-SPORTS: события на ближайшие days_ahead дней (VN календарь).
    """
    if not SPORTS_API_KEY:
        return []

    start = _today()
    headers = {"x-apisports-key": SPORTS_API_KEY}
    base = "https://v1.basketball.api-sports.io"

    async def one_day(d: date) -> list[dict[str, Any]]:
        url = f"{base}/games?date={d.isoformat()}"
        log.info("Basketball endpoint: %s", url)
        try:
            data = await _get_json(url, headers=headers)
            resp = data.get("response") or []
        except Exception as e:
            _reraise_api_sports(e)
            log.error("Basketball API day failed (%s): %s", d, e)
            return []

        if not isinstance(resp, list):
            return []
        log.info("Basketball games on %s: %s", d.isoformat(), len(resp))
        day_events: list[dict[str, Any]] = []
        for item in resp:
            league = item.get("league") or {}
            teams = item.get("teams") or {}
            home = (teams.get("home") or {}).get("name") or ""
            away = (teams.get("away") or {}).get("name") or ""
            league_name = league.get("name") or ""
            round_name = league.get("round") or ""
            league_full = league_name + (f" ({round_name})" if round_name else "")
            title = f"{home} vs {away}".strip(" vs").strip()
            dt_iso = str(item.get("date") or item.get("time") or "")
            dt_fields = _row_from_api_dt(
                dt_iso=dt_iso,
                timestamp=item.get("timestamp"),
                timezone_name=str(item.get("timezone") or ""),
            )
            if not dt_fields.get("date"):
                continue
            row: dict[str, Any] = {
                "sport": "basketball",
                "title": title or "Game",
                "league": league_full or "Basketball",
                "home": home,
                "away": away,
                "date": dt_fields["date"],
                "time": dt_fields["time"],
                "fixture_utc_iso": dt_fields.get("fixture_utc_iso") or dt_iso,
                "fixture_timestamp": dt_fields.get("fixture_timestamp"),
                "api_timezone": dt_fields.get("api_timezone"),
                "importance": "low",
                "source": "API-SPORTS",
            }
            day_events.append(row)
        return day_events

    return await _collect_days_sequential(
        one_day, days_ahead=days_ahead, sport_label="basketball"
    )


async def get_hockey_events(*, days_ahead: int = 3) -> list[dict[str, Any]]:
    """
    Hockey API-SPORTS: события на ближайшие days_ahead дней (VN календарь).
    """
    if not SPORTS_API_KEY:
        return []

    start = _today()
    headers = {"x-apisports-key": SPORTS_API_KEY}
    base = "https://v1.hockey.api-sports.io"

    async def one_day(d: date) -> list[dict[str, Any]]:
        url = f"{base}/games?date={d.isoformat()}"
        log.info("Hockey endpoint: %s", url)
        try:
            data = await _get_json(url, headers=headers)
            resp = data.get("response") or []
        except Exception as e:
            _reraise_api_sports(e)
            log.error("Hockey API day failed (%s): %s", d, e)
            return []

        if not isinstance(resp, list):
            return []
        log.info("Hockey games on %s: %s", d.isoformat(), len(resp))
        day_events: list[dict[str, Any]] = []
        for item in resp:
            league = item.get("league") or {}
            teams = item.get("teams") or {}
            home = (teams.get("home") or {}).get("name") or ""
            away = (teams.get("away") or {}).get("name") or ""
            league_name = league.get("name") or ""
            round_name = league.get("round") or ""
            league_full = league_name + (f" ({round_name})" if round_name else "")
            title = f"{home} vs {away}".strip(" vs").strip()
            dt_iso = str(item.get("date") or item.get("time") or "")
            dt_fields = _row_from_api_dt(
                dt_iso=dt_iso,
                timestamp=item.get("timestamp"),
                timezone_name=str(item.get("timezone") or ""),
            )
            if not dt_fields.get("date"):
                continue
            row = {
                "sport": "hockey",
                "title": title or "Game",
                "league": league_full or "Hockey",
                "home": home,
                "away": away,
                "date": dt_fields["date"],
                "time": dt_fields["time"],
                "fixture_utc_iso": dt_fields.get("fixture_utc_iso") or dt_iso,
                "fixture_timestamp": dt_fields.get("fixture_timestamp"),
                "api_timezone": dt_fields.get("api_timezone"),
                "importance": "low",
                "source": "API-SPORTS",
            }
            day_events.append(row)
        return day_events

    return await _collect_days_sequential(
        one_day, days_ahead=days_ahead, sport_label="hockey"
    )


async def get_esports_events(*, days_ahead: int = 3) -> list[dict[str, Any]]:
    """
    Esports API-SPORTS (v1.esports) — если включено в подписку.
    Нет ключа/плана → дни тихо возвращают [].
    """
    if not SPORTS_API_KEY:
        return []

    start = _today()
    headers = {"x-apisports-key": SPORTS_API_KEY}
    base = "https://v1.esports.api-sports.io"
    source_dead = False

    async def one_day(d: date) -> list[dict[str, Any]]:
        nonlocal source_dead
        if source_dead:
            return []
        collected: list[dict[str, Any]] = []
        for path in (
            f"/games?date={d.isoformat()}",
            f"/matches?date={d.isoformat()}",
        ):
            url = f"{base}{path}"
            log.info("Esports endpoint: %s", url)
            try:
                data = await _get_json(url, headers=headers)
            except Exception as e:
                _reraise_api_sports(e)
                err = str(e).lower()
                if "service not known" in err or "getaddrinfo" in err or "name or service" in err:
                    log.warning("ESPORTS_SOURCE_UNAVAILABLE: %s", e)
                    source_dead = True
                    return []
                log.info("Esports path failed (%s): %s", path, e)
                continue
            resp = data.get("response") or []
            if not isinstance(resp, list):
                continue
            for item in resp:
                league_o = item.get("league") or item.get("tournament") or {}
                league_name = (
                    league_o.get("name") if isinstance(league_o, dict) else str(league_o or "")
                )
                teams = item.get("teams") or item.get("opponents") or {}
                home = ""
                away = ""
                if isinstance(teams, dict):
                    th = teams.get("home") or teams.get("player1") or teams.get("team1")
                    ta = teams.get("away") or teams.get("player2") or teams.get("team2")
                    if isinstance(th, dict):
                        home = str(th.get("name") or "")
                    elif th:
                        home = str(th)
                    if isinstance(ta, dict):
                        away = str(ta.get("name") or "")
                    elif ta:
                        away = str(ta)
                title = str(
                    item.get("name") or ""
                ).strip() or (
                    f"{home} — {away}".replace("vs", "").strip(" —").strip()
                )
                title = title or "Esports match"
                if home and away and home not in title and away not in title:
                    title = f"{home} — {away}"
                dt_iso = (
                    item.get("begin_at")
                    or item.get("scheduled_at")
                    or item.get("date")
                    or item.get("time")
                    or ""
                )
                d_str, t_str = _parse_dt_to_local(str(dt_iso))
                if not d_str:
                    continue
                row = {
                    "sport": "esports",
                    "title": title,
                    "league": league_name or "Esports",
                    "date": d_str,
                    "time": t_str,
                    "importance": "medium",
                    "source": "API-SPORTS",
                    "fixture_utc_iso": str(dt_iso) if dt_iso else "",
                }
                collected.append(row)
            if collected:
                break
        log.info("Esports resolved on %s: %s items", d.isoformat(), len(collected))
        return collected

    return await _collect_days_sequential(
        one_day, days_ahead=days_ahead, sport_label="esports"
    )


async def get_tennis_events() -> list[dict[str, Any]]:
    """Tennis API-SPORTS: ATP/WTA 500+ и матчи с известными игроками."""
    if not SPORTS_API_KEY:
        return []

    start = _today()
    headers = {"x-apisports-key": SPORTS_API_KEY}
    base = "https://v1.tennis.api-sports.io"

    async def one_day(d: date) -> list[dict[str, Any]]:
        url = f"{base}/fixtures?date={d.isoformat()}"
        log.info("Tennis endpoint: %s", url)
        try:
            data = await _get_json(url, headers=headers)
        except Exception as e:
            log.info("Tennis API day failed (%s): %s", d, e)
            return []
        resp = data.get("response") or []
        if not isinstance(resp, list):
            return []
        day_events: list[dict[str, Any]] = []
        for item in resp:
            if not isinstance(item, dict):
                continue
            fixture = item.get("fixture") or item
            tournament = item.get("tournament") or item.get("competition") or {}
            t_name = (
                tournament.get("name") if isinstance(tournament, dict) else str(tournament or "")
            )
            players = item.get("players") or item.get("opponents") or []
            p1 = p2 = ""
            if isinstance(players, list) and len(players) >= 2:
                for side in players[:2]:
                    if isinstance(side, dict):
                        pl = side.get("player") or side
                        nm = pl.get("name") if isinstance(pl, dict) else str(pl or "")
                        if not p1:
                            p1 = str(nm or "")
                        else:
                            p2 = str(nm or "")
            elif isinstance(players, dict):
                for key in ("home", "away", "player1", "player2"):
                    pl = players.get(key)
                    if isinstance(pl, dict):
                        nm = str(pl.get("name") or "")
                    else:
                        nm = str(pl or "")
                    if nm and not p1:
                        p1 = nm
                    elif nm:
                        p2 = nm
            title = f"{p1} — {p2}".strip(" —") if p1 and p2 else str(item.get("name") or "").strip()
            if not title or title == "—":
                continue
            league_full = str(t_name or "Tennis")
            dt_iso = (
                (fixture.get("date") if isinstance(fixture, dict) else None)
                or item.get("date")
                or ""
            )
            d_str, t_str = _parse_dt_to_local(str(dt_iso))
            if not d_str:
                continue
            row = {
                "sport": "tennis",
                "title": title,
                "league": league_full,
                "date": d_str,
                "time": t_str,
                "importance": "medium",
                "source": "API-SPORTS",
            }
            if dt_iso:
                row["fixture_utc_iso"] = str(dt_iso)
            if not is_gastrobar_api_sport_worthy(row):
                continue
            day_events.append(row)
        log.info("Tennis fixtures on %s: %s worthy", d.isoformat(), len(day_events))
        return day_events

    chunks = await asyncio.gather(
        *[one_day(start + timedelta(days=i)) for i in range(7)],
        return_exceptions=True,
    )
    events: list[dict[str, Any]] = []
    for ch in chunks:
        if isinstance(ch, Exception):
            log.error("Tennis parallel day failed: %s", ch)
            continue
        events.extend(ch)
    return events


async def get_formula_events(*, days_ahead: int = 3) -> list[dict[str, Any]]:
    """
    Formula 1 API-SPORTS: ближайшие гонки/мероприятия.
    Free plan: используем только races?date=YYYY-MM-DD.
    """
    if not SPORTS_API_KEY:
        return []

    start = _today()
    headers = {"x-apisports-key": SPORTS_API_KEY}
    base = "https://v1.formula-1.api-sports.io"

    async def one_day(d: date) -> list[dict[str, Any]]:
        url = f"{base}/races?date={d.isoformat()}"
        log.info("Formula1 endpoint: %s", url)
        try:
            data = await _get_json(url, headers=headers)
        except Exception as e:
            _reraise_api_sports(e)
            log.error("Formula1 day failed (%s): %s", d.isoformat(), e)
            return []

        resp = data.get("response") or []
        if not isinstance(resp, list):
            return []
        log.info("Formula1 races on %s: %s", d.isoformat(), len(resp))

        day_events: list[dict[str, Any]] = []
        for item in resp:
            if not isinstance(item, dict):
                continue
            expanded = _expand_formula_api_item(item)
            log.debug(
                "Formula1 expand rows=%s sample_keys=%s",
                len(expanded),
                list(item.keys())[:12],
            )
            day_events.extend(expanded)
        return day_events

    return await _collect_days_sequential(
        one_day, days_ahead=days_ahead, sport_label="formula1"
    )


async def _merge_raw_safe_72h_events(*, days_ahead: int = 3) -> "ApiCollectResult":
    """
    SAFE_MODE: sequential sports, throttle 1.5s, stop on suspended.
    days_ahead: NOW24=2, NEXT72=3.
    """
    from api_sports_status import (
        ApiCollectResult,
        ApiSportsError,
        _RunState,
        fetch_note_from_health,
    )

    if not SPORTS_API_KEY:
        from api_sports_status import API_NOTE_NO_KEY

        log.warning("SPORTS_API_KEY не задан — SAFE_MODE stub.")
        return ApiCollectResult(
            [],
            fetch_note=API_NOTE_NO_KEY,
            sport_status={},
        )

    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    today = datetime.now(tz).date()
    end_day = today + timedelta(days=max(0, days_ahead - 1))
    log.info(
        "SAFE_MODE API fetch: %s days (%s .. %s), sequential, throttle 1.5s",
        days_ahead,
        today.isoformat(),
        end_day.isoformat(),
    )

    state = _RunState()
    sport_fetches: tuple[tuple[str, Any], ...] = (
        ("football", get_football_events_next_days_vn(days_ahead=days_ahead)),
        ("hockey", get_hockey_events(days_ahead=days_ahead)),
        ("basketball", get_basketball_events(days_ahead=days_ahead)),
        ("formula1", get_formula_events(days_ahead=days_ahead)),
        ("esports", get_esports_events(days_ahead=days_ahead)),
    )
    merged: list[dict[str, Any]] = []
    for label, coro in sport_fetches:
        if state.abort:
            state.sport_status.setdefault(label, "skipped_suspended")
            continue
        try:
            rows = await coro
            state.record(label, "ok")
            log.info("SAFE_MODE %s: %s events", label, len(rows))
            merged.extend(rows)
        except ApiSportsError as e:
            state.record(label, e.health, e.detail)
            if e.health == "suspended":
                break
            if e.health == "rateLimit":
                break
        except Exception as e:
            err = str(e).lower()
            if label == "esports" and (
                "service not known" in err or "getaddrinfo" in err
            ):
                state.record(label, "unavailable", "ESPORTS_SOURCE_UNAVAILABLE")
            else:
                state.record(label, "unavailable", str(e)[:120])
                log.error("SAFE_MODE %s failed: %s", label, e)

    global _last_api_collect_note
    _last_api_collect_note = state.finalize_note()
    from api_sports_status import set_last_sport_status

    set_last_sport_status(state.sport_status)

    _log_found_breakdown(merged, label="SAFE_MODE")
    log.info(
        "SAFE_MODE raw total: %s fetch_note=%s sports=%s",
        len(merged),
        _last_api_collect_note,
        state.sport_status,
    )
    return ApiCollectResult(
        merged,
        fetch_note=_last_api_collect_note,
        sport_status=state.sport_status,
        abort_reason=state.abort_reason,
    )


_last_api_collect_note: str | None = None


def get_last_api_collect_note() -> str | None:
    return _last_api_collect_note


async def _merge_raw_now24_events() -> "ApiCollectResult":
    """NOW24 API: только today + tomorrow."""
    return await _merge_raw_safe_72h_events(days_ahead=2)


async def _merge_raw_week_events() -> "ApiCollectResult":
    """NEXT72 API: максимум 3 дня."""
    return await _merge_raw_safe_72h_events(days_ahead=3)


async def get_week_events_with_stats() -> tuple[list[dict[str, Any]], int, int]:
    """
    Редакторская программа недели + числа для UI:
    (программа для афиши, всего событий с API, сколько пунктов в программе).
    """
    merged = await _merge_raw_week_events()
    return build_weekly_program_with_stats(merged)


async def get_week_events() -> list[dict[str, Any]]:
    """
    Плоский список для scheduler (совместимость с replace_week_events).
    """
    merged = await _merge_raw_week_events()
    program, _, _ = build_weekly_program_with_stats(merged)
    return editor_program_to_legacy_events(program)


def format_week_poster(program: list[dict[str, Any]]) -> str:
    """Текст превью в формате редактора (результат build_gastrobar_weekly_program)."""
    if not program:
        return "События не найдены"
    wd = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")
    lines = ["🔥 ГЛАВНОЕ НА НЕДЕЛЕ", ""]
    for item in program:
        if item.get("kind") == "block":
            lines.append(f"{item.get('emoji', '')} {item.get('line', '')}")
            lines.append("")
            continue
        title = str(item.get("title", ""))
        league_ru = str(item.get("league_label_ru", ""))
        d = str(item.get("date", ""))
        t = str(item.get("time", ""))
        day_mark = ""
        try:
            dd = date.fromisoformat(d)
            day_mark = wd[dd.weekday()]
        except Exception:
            day_mark = d
        lines.append(f"⚽ {title}")
        if league_ru:
            lines.append(league_ru)
        lines.append(f"{day_mark} {t}")
        lines.append("")
    lines.append("📍Gastrobar")
    lines.append("Океанус, улица с траками")
    return "\n".join(lines).strip()


def events_for_dates(
    events: list[dict[str, Any]], target_dates: set[date]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in events:
        try:
            ed = date.fromisoformat(str(e["date"]))
        except (ValueError, KeyError):
            continue
        if ed in target_dates:
            out.append(e)
    return out


def important_today_tomorrow(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """События на сегодня/завтра с высокой важностью."""
    t0 = _today()
    dates = {t0, t0 + timedelta(days=1)}
    cand = events_for_dates(events, dates)
    return [e for e in cand if (e.get("importance") or "").lower() == "high"]


_WD = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")
_MONTHS_GEN = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

_SPORT_EMOJI: dict[str, str] = {
    "football": "⚽",
    "soccer": "⚽",
    "basketball": "🏀",
    "nba": "🏀",
    "hockey": "🏒",
    "nhl": "🏒",
    "mma": "🥊",
    "ufc": "🥊",
    "boxing": "🥊",
    "f1": "🏎",
    "tennis": "🎾",
    "baseball": "⚾",
    "mlb": "⚾",
    "esports": "🎮",
    "nfl": "🏈",
    "poker": "🃏",
    "olympics": "🏅",
}


def _emoji_for_sport(sport: str) -> str:
    key = (sport or "").lower().strip()
    return _SPORT_EMOJI.get(key, "🏟")


def _importance_ru(imp: str) -> str:
    m = {
        "high": "высокая",
        "medium": "средняя",
        "low": "низкая",
    }
    return m.get((imp or "").lower(), imp or "—")


def _format_day_header(d: date) -> str:
    wd = _WD[d.weekday()]
    return f"{wd}, {d.day} {_MONTHS_GEN[d.month - 1]}"


def format_afisha_message(events: list[dict[str, Any]]) -> str:
    """Текст афиши для Telegram (как в ТЗ)."""
    if not events:
        return "Пока нет отобранных событий на эту неделю."
    by_date: dict[date, list[dict[str, Any]]] = {}
    for e in events:
        try:
            dd = date.fromisoformat(str(e["date"]))
        except (ValueError, KeyError):
            continue
        by_date.setdefault(dd, []).append(e)
    lines: list[str] = []
    for dd in sorted(by_date.keys()):
        lines.append(_format_day_header(dd))
        for e in by_date[dd]:
            emo = _emoji_for_sport(str(e.get("sport", "")))
            title = e.get("title", "")
            league = e.get("league", "")
            tm = e.get("time", "")
            imp = _importance_ru(str(e.get("importance", "")))
            lines.append(f"{emo} {title}")
            lines.append(f"{league}, {tm}")
            lines.append(f"Важность: {imp}")
            lines.append("")
    return "\n".join(lines).strip()


def spotlight_line_for_daily(events: list[dict[str, Any]]) -> str:
    """Короткая строка для push «сильный повод»."""
    parts: list[str] = []
    t0 = _today()
    t1 = t0 + timedelta(days=1)
    for e in events[:4]:
        try:
            dd = date.fromisoformat(str(e["date"]))
        except (ValueError, KeyError):
            continue
        day = "сегодня" if dd == t0 else "завтра" if dd == t1 else dd.isoformat()
        parts.append(f"{day}: {e.get('title', '')} ({e.get('league', '')})")
    return "; ".join(parts)
