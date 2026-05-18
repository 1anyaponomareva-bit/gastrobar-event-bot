"""
Конвертация времени событий: только Python + zoneinfo → Asia/Ho_Chi_Minh.
Gemini отдаёт original date/time/timezone; сдвиг часов вручную запрещён.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time as dtime, timezone
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?$")
_APPROX_TIME_PREFIXES = re.compile(
    r"^(≈|~|around|about|примерно|ориентировочно|circa)\s*",
    re.I,
)


def _parse_time_flexible(raw: str) -> tuple[str | None, bool]:
    t = str(raw or "").strip()
    if not t:
        return None, False
    approx = bool(_APPROX_TIME_PREFIXES.match(t))
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
        return None, approx
    return f"{int(m.group(1)):02d}:{m.group(2)}", approx


def _event_blob(e: dict[str, Any]) -> str:
    parts = (e.get("title"), e.get("category"), e.get("subtitle"), e.get("league"), e.get("why"))
    s = " ".join(str(p or "") for p in parts).lower()
    for a, b in (("\u2019", "'"), ("\u2013", "-"), ("\u2014", "-")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def _normalize_hhmm(t: str) -> str | None:
    norm, _ = _parse_time_flexible(t)
    return norm

TARGET_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
TARGET_TZ_NAME = "Asia/Ho_Chi_Minh"
_WD_RU = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Immutable после первой конвертации — weekly/daily читают одни и те же поля.
_DATETIME_CANONICAL_KEYS = (
    "utc_datetime",
    "local_datetime",
    "local_date",
    "local_time",
    "local_weekday",
    "timezone",
    "date",
    "time",
    "weekday",
    "display_time",
    "time_display",
)

# IANA и распространённые аббревиатуры (не UTC fallback для «неизвестно»).
_ZONE_ALIASES: dict[str, str] = {
    "ICT": "Asia/Ho_Chi_Minh",
    "GMT+7": "Asia/Bangkok",
    "UTC+7": "Asia/Bangkok",
    "VIETNAM": "Asia/Ho_Chi_Minh",
    "HO_CHI_MINH": "Asia/Ho_Chi_Minh",
    "NHATRANG": "Asia/Ho_Chi_Minh",
    "CEST": "Europe/Zurich",
    "CET": "Europe/Paris",
    "CENTRAL EUROPEAN TIME": "Europe/Paris",
    "CENTRAL EUROPEAN": "Europe/Paris",
    "BST": "Europe/London",
    "GMT": "UTC",
    "UTC": "UTC",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "ET": "America/New_York",
    "EASTERN": "America/New_York",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "PT": "America/Los_Angeles",
    "PACIFIC": "America/Los_Angeles",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "MST": "America/Denver",
    "MDT": "America/Denver",
}


def get_ru_weekday(dt: datetime) -> str:
    return _WD_RU[dt.weekday()]


def resolve_zone(name: str) -> ZoneInfo | None:
    n = str(name or "").strip()
    if not n:
        return None
    if n.upper().replace(" ", "_") in _ZONE_ALIASES:
        n = _ZONE_ALIASES[n.upper().replace(" ", "_")]
    elif n.upper() in _ZONE_ALIASES:
        n = _ZONE_ALIASES[n.upper()]
    else:
        for key, iana in _ZONE_ALIASES.items():
            if key in n.upper():
                n = iana
                break
    try:
        return ZoneInfo(n)
    except Exception:
        return None


def is_valid_source_timezone(name: str) -> bool:
    return resolve_zone(name) is not None


def extract_source_fields(event: dict[str, Any]) -> tuple[str, str, str]:
    """original_date / original_time / original_timezone (или date/time/source_timezone)."""
    date_s = str(
        event.get("original_date") or event.get("date", "")
    ).strip()
    time_s = str(
        event.get("original_time") or event.get("time", "")
    ).strip()
    tz = str(
        event.get("original_timezone")
        or event.get("source_timezone")
        or ""
    ).strip()
    return date_s, time_s, tz


def extract_source_fields_for_conversion(event: dict[str, Any]) -> tuple[str, str, str]:
    """
    Поля для конвертации: только официальный source, не VN date/time после lock.
    """
    odate = str(event.get("original_date", "")).strip()
    otime = str(event.get("original_time", "")).strip()
    otz = str(
        event.get("original_timezone") or event.get("source_timezone") or ""
    ).strip()
    if _DATE_RE.match(odate) and otime and otz and is_valid_source_timezone(otz):
        return odate, otime, otz
    if event.get("time_locked") or event.get("utc_datetime"):
        return "", "", ""
    return extract_source_fields(event)


def parse_datetime_iso(value: str) -> datetime | None:
    s = str(value or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def source_to_utc_datetime(date_s: str, time_s: str, source_timezone: str) -> datetime:
    """Один шаг: wall-clock в source TZ → aware UTC."""
    time_norm = _normalize_hhmm(time_s)
    if not time_norm:
        raise ValueError(f"bad time: {time_s!r}")
    zi = resolve_zone(source_timezone)
    if zi is None:
        raise ValueError(f"bad timezone: {source_timezone!r}")
    d = date.fromisoformat(date_s)
    hh, mm = map(int, time_norm.split(":"))
    local_dt = datetime.combine(d, dtime(hh, mm), tzinfo=zi)
    return local_dt.astimezone(timezone.utc)


def utc_datetime_to_local_fields(utc_dt: datetime) -> dict[str, str]:
    """Единственная конвертация UTC → Asia/Ho_Chi_Minh."""
    utc_aware = utc_dt.astimezone(timezone.utc)
    local_dt = utc_aware.astimezone(TARGET_TZ)
    tm = local_dt.strftime("%H:%M")
    return {
        "utc_datetime": utc_aware.isoformat(),
        "local_datetime": local_dt.isoformat(),
        "local_date": local_dt.date().isoformat(),
        "local_time": tm,
        "local_weekday": get_ru_weekday(local_dt),
        "timezone": TARGET_TZ_NAME,
        "date": local_dt.date().isoformat(),
        "time": tm,
        "weekday": get_ru_weekday(local_dt),
    }


def has_locked_datetime(event: dict[str, Any]) -> bool:
    return bool(
        str(event.get("utc_datetime", "")).strip()
        and str(event.get("local_datetime", "")).strip()
    )


def log_datetime_pipeline(event: dict[str, Any]) -> None:
    log.info("UTC DATETIME: %s", event.get("utc_datetime"))
    log.info("LOCAL DATETIME: %s", event.get("local_datetime"))
    log.info("TIMEZONE USED: %s", TARGET_TZ_NAME)


def _is_asian_football_competition(blob: str) -> bool:
    return bool(
        re.search(
            r"afc|asian\s+cup|j[\-\s]?league|k[\-\s]?league|csl|saudi\s+pro|"
            r"a[\-\s]?league|thai\s+league|vietnam|v[\-\s]?league",
            blob,
            re.I,
        )
    )


def sanity_check_football_vn_time(event: dict[str, Any]) -> None:
    """EPL/топ-лиги редко стартуют днём по VN; предупреждение о возможной ошибке TZ."""
    blob = _event_blob(event)
    cat = str(event.get("category", "")).upper()
    if "FOOT" not in cat and "SOCCER" not in cat and "EPL" not in cat:
        if not re.search(
            r"premier\s+league|champions\s+league|europa\s+league|"
            r"la\s+liga|serie\s+a|bundesliga|ligue\s+1",
            blob,
            re.I,
        ):
            return
    if _is_asian_football_competition(blob):
        return
    tm = str(event.get("local_time") or event.get("time", "")).strip().removeprefix("≈")
    m = _TIME_RE.match(tm)
    if not m:
        return
    hour = int(m.group(1))
    if 9 <= hour <= 14:
        log.warning(
            "TIME SANITY: football %r at %s %s looks like midday VN — "
            "check source TZ (EPL usually evening/night/early morning). "
            "utc=%s original=%s %s %s",
            event.get("title"),
            event.get("local_weekday"),
            tm,
            event.get("utc_datetime"),
            event.get("original_date"),
            event.get("original_time"),
            event.get("original_timezone") or event.get("source_timezone"),
        )


def reconcile_event_datetime(
    cached: dict[str, Any],
    fresh: dict[str, Any] | None,
) -> dict[str, Any]:
    from locked_time import assert_no_time_drift

    return assert_no_time_drift(cached, fresh or cached, context="reconcile")


def get_event_start_vn(event: dict[str, Any]) -> datetime | None:
    """Aware datetime в Asia/Ho_Chi_Minh из canonical local_datetime."""
    loc = parse_datetime_iso(str(event.get("local_datetime", "")))
    if loc is not None:
        return loc.astimezone(TARGET_TZ)
    date_s = str(event.get("local_date") or event.get("date", "")).strip()
    time_s = str(event.get("local_time") or event.get("time", "")).strip()
    time_s = time_s.removeprefix("≈").strip()
    if not _DATE_RE.match(date_s):
        return None
    norm = _normalize_hhmm(time_s)
    if not norm:
        return None
    try:
        d = date.fromisoformat(date_s)
        hh, mm = map(int, norm.split(":"))
        return datetime.combine(d, dtime(hh, mm), tzinfo=TARGET_TZ)
    except ValueError:
        return None


def apply_event_datetime(event: dict[str, Any]) -> dict[str, Any] | None:
    """Delegate to locked_time — no inference, no double conversion."""
    from locked_time import lock_event_schedule

    return lock_event_schedule(event, phase="apply_event_datetime")


def infer_source_timezone(event: dict[str, Any]) -> str | None:
    """Официальная зона по типу события, если Gemini не указал IANA."""
    b = _event_blob(event)
    title = str(event.get("title", "")).lower()

    if "eurovision" in b:
        return "Europe/Zurich"
    if re.search(r"\bufc\b|ufc fight night", b):
        if re.search(r"vegas|las vegas|nevada", b):
            return "America/Los_Angeles"
        if re.search(r"abu dhabi|uae|dubai", b):
            return "Asia/Dubai"
        return "America/New_York"
    if re.search(r"formula\s*1|\bf1\b|grand\s+prix", b):
        if re.search(r"montreal|canada|canadian", b):
            return "America/Toronto"
        if re.search(r"monaco", b):
            return "Europe/Monaco"
        if re.search(r"silverstone|british|uk\b|united kingdom", b):
            return "Europe/London"
        if re.search(r"monza|italy|italian", b):
            return "Europe/Rome"
        if re.search(r"spain|spanish|barcelona", b):
            return "Europe/Madrid"
        if re.search(r"bahrain|saudi|jeddah|qatar", b):
            return "Asia/Bahrain"
        return "Europe/London"
    if re.search(
        r"premier\s+league|\bepl\b|la\s+liga|serie\s+a|bundesliga|ligue\s+1",
        b,
    ):
        if re.search(
            r"london|manchester|liverpool|arsenal|chelsea|tottenham|spurs|"
            r"everton|west ham|newcastle|brighton|wolves|aston villa",
            b,
        ):
            return "Europe/London"
        if re.search(r"barcelona|real madrid|atletico|sevilla", b):
            return "Europe/Madrid"
        if re.search(r"bayern|dortmund|leipzig|leverkusen", b):
            return "Europe/Berlin"
        if re.search(r"inter|milan|juventus|napoli|roma|lazio", b):
            return "Europe/Rome"
        if re.search(r"psg|marseille|lyon|monaco", b):
            return "Europe/Paris"
        return "Europe/London"
    if re.search(r"champions\s+league|uefa", b):
        if re.search(r"london|manchester|liverpool", b):
            return "Europe/London"
        return "Europe/Paris"
    if re.search(r"\bnba\b", b):
        if re.search(r"lakers|clippers|warriors|kings|suns", b):
            return "America/Los_Angeles"
        return "America/New_York"
    if re.search(r"\bnhl\b|stanley", b):
        return "America/New_York"
    return None


def convert_event_time(
    date_s: str,
    time_s: str,
    source_timezone: str,
) -> dict[str, str]:
    """source wall-clock → UTC → VN (legacy dict: date, time, weekday + canonical fields)."""
    if not _DATE_RE.match(date_s):
        raise ValueError(f"bad date: {date_s!r}")
    utc_dt = source_to_utc_datetime(date_s, time_s, source_timezone)
    return utc_datetime_to_local_fields(utc_dt)


def convert_event_to_vn(event: dict[str, Any]) -> tuple[dict[str, str] | None, str]:
    """
    (converted dict | None, time_precision)
    time_precision: exact | estimated | unknown
    """
    log.info("TIME RAW: %s", event)
    applied = apply_event_datetime(dict(event))
    if applied is None:
        return None, "unknown"
    converted = {
        "date": applied["date"],
        "time": applied["time"],
        "weekday": applied["weekday"],
        "utc_datetime": applied["utc_datetime"],
        "local_datetime": applied["local_datetime"],
        "local_date": applied["local_date"],
        "local_time": applied["local_time"],
        "local_weekday": applied["local_weekday"],
        "timezone": applied["timezone"],
    }
    prec = str(applied.get("time_precision", "exact")).lower()
    if prec == "unknown":
        return None, "unknown"
    log.info("TIME CONVERTED TO VN: %s", converted)
    return converted, prec
