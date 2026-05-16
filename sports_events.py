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

from config import SPORTS_API_KEY


def _today() -> date:
    return date.today()


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

_TOP_EPL = ("manchester city", "liverpool", "arsenal")
_TOP_LALIGA = ("real madrid", "barcelona", "atletico")
_TOP_BUNDES = ("bayern", "dortmund")
_TOP_SERIEA = ("inter", "milan", "juventus", "napoli")
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
    API обычно возвращает ISO datetime в UTC.
    Возвращаем (YYYY-MM-DD, HH:MM) в Asia/Ho_Chi_Minh.
    """
    if not dt_str:
        return "", "00:00"
    s = str(dt_str).strip()
    # Часто встречается формат с Z
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Если вернули только дату — считаем 00:00
        try:
            d = date.fromisoformat(s[:10])
            dt = datetime.combine(d, dtime(0, 0), tzinfo=TZ)
        except Exception:
            return "", "00:00"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    local = dt.astimezone(TZ)
    return local.date().isoformat(), local.strftime("%H:%M")


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
        if "premier league" in blob and _contains_any(blob, _TOP_EPL):
            return True
        if ("la liga" in blob or "laliga" in blob) and _contains_any(blob, _TOP_LALIGA):
            return True
        if "bundesliga" in blob and _contains_any(blob, _TOP_BUNDES):
            return True
        if "serie a" in blob and _contains_any(blob, _TOP_SERIEA):
            return True
        return False

    if sport == "basketball":
        if "nba" not in blob:
            return False
        if "playoffs" in blob or "finals" in blob:
            return True
        return _contains_any(blob, _TOP_NBA)

    if sport == "hockey":
        return "nhl" in blob and ("playoffs" in blob or "stanley cup" in blob)

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
    return {
        "kind": "match",
        "sport": "football",
        "title": title,
        "league_label_ru": _league_label_ru(league_raw),
        "league_raw": league_raw,
        "date": str(e.get("date", "")),
        "time": str(e.get("time", "")),
        "tier": tier,
    }


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


async def _get_json(url: str, *, headers: dict[str, str], timeout: float = 15.0) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected JSON payload")
    return data


async def get_football_events() -> list[dict[str, Any]]:
    """
    Football API-SPORTS: события на ближайшие 7 дней.
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
            round_name = league.get("round") or ""
            league_full = league_name + (f" ({round_name})" if round_name else "")
            title = f"{home} vs {away}".strip(" vs").strip()
            dt_iso = fixture.get("date") or ""
            d_str, t_str = _parse_dt_to_local(dt_iso)
            if not d_str:
                continue
            day_events.append(
                {
                    "sport": "football",
                    "title": title or "Match",
                    "league": league_full or league_name or "Football",
                    "date": d_str,
                    "time": t_str,
                    "importance": "low",
                    "source": "API-SPORTS",
                }
            )
        return day_events

    # Free plan: from/to недоступен — запрашиваем по date=; дни параллельно, чтобы /week не «висел» минутами.
    chunks = await asyncio.gather(
        *[one_day(start + timedelta(days=i)) for i in range(7)],
        return_exceptions=True,
    )
    events: list[dict[str, Any]] = []
    for ch in chunks:
        if isinstance(ch, Exception):
            log.error("Football parallel day failed: %s", ch)
            continue
        events.extend(ch)
    return events


async def get_basketball_events() -> list[dict[str, Any]]:
    """
    Basketball API-SPORTS: события на ближайшие 7 дней.
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
            dt_iso = item.get("date") or item.get("time") or ""
            d_str, t_str = _parse_dt_to_local(dt_iso)
            if not d_str:
                continue
            day_events.append(
                {
                    "sport": "basketball",
                    "title": title or "Game",
                    "league": league_full or "Basketball",
                    "date": d_str,
                    "time": t_str,
                    "importance": "low",
                    "source": "API-SPORTS",
                }
            )
        return day_events

    chunks = await asyncio.gather(
        *[one_day(start + timedelta(days=i)) for i in range(7)],
        return_exceptions=True,
    )
    events: list[dict[str, Any]] = []
    for ch in chunks:
        if isinstance(ch, Exception):
            log.error("Basketball parallel day failed: %s", ch)
            continue
        events.extend(ch)
    return events


async def get_hockey_events() -> list[dict[str, Any]]:
    """
    Hockey API-SPORTS: события на ближайшие 7 дней.
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
            dt_iso = item.get("date") or item.get("time") or ""
            d_str, t_str = _parse_dt_to_local(dt_iso)
            if not d_str:
                continue
            day_events.append(
                {
                    "sport": "hockey",
                    "title": title or "Game",
                    "league": league_full or "Hockey",
                    "date": d_str,
                    "time": t_str,
                    "importance": "low",
                    "source": "API-SPORTS",
                }
            )
        return day_events

    chunks = await asyncio.gather(
        *[one_day(start + timedelta(days=i)) for i in range(7)],
        return_exceptions=True,
    )
    events: list[dict[str, Any]] = []
    for ch in chunks:
        if isinstance(ch, Exception):
            log.error("Hockey parallel day failed: %s", ch)
            continue
        events.extend(ch)
    return events


async def get_formula_events() -> list[dict[str, Any]]:
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
            log.error("Formula1 day failed (%s): %s", d.isoformat(), e)
            return []

        resp = data.get("response") or []
        if not isinstance(resp, list):
            return []
        log.info("Formula1 races on %s: %s", d.isoformat(), len(resp))

        day_events: list[dict[str, Any]] = []
        for item in resp:
            race_name = (
                item.get("raceName")
                or item.get("name")
                or item.get("race")
                or item.get("eventName")
                or ""
            )
            dt_iso = item.get("date") or item.get("time") or ""
            d_str, t_str = _parse_dt_to_local(str(dt_iso))
            if not d_str:
                continue
            title = race_name or "Formula 1 race"
            if "formula" not in title.lower():
                title = f"Formula 1 {title}".strip()
            day_events.append(
                {
                    "sport": "formula1",
                    "title": title,
                    "league": "Formula 1",
                    "date": d_str,
                    "time": t_str,
                    "importance": "low",
                    "source": "API-SPORTS",
                }
            )
        return day_events

    chunks = await asyncio.gather(
        *[one_day(start + timedelta(days=i)) for i in range(7)],
        return_exceptions=True,
    )
    events: list[dict[str, Any]] = []
    for ch in chunks:
        if isinstance(ch, Exception):
            log.error("Formula1 parallel day failed: %s", ch)
            continue
        events.extend(ch)
    return events


async def _merge_raw_week_events() -> list[dict[str, Any]]:
    """Все события с API за неделю до фильтра важности."""
    if not SPORTS_API_KEY:
        log.warning(
            "SPORTS_API_KEY не задан — для /week используются демо-события (заглушка). "
            "Добавьте ключ в .env для реальных данных API-SPORTS."
        )
        return _stub_raw_events()

    results = await asyncio.gather(
        get_football_events(),
        get_basketball_events(),
        get_hockey_events(),
        get_formula_events(),
        return_exceptions=True,
    )

    merged: list[dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            log.error("get_week_events sport task failed: %s", r)
            continue
        merged.extend(r)
    return merged


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
