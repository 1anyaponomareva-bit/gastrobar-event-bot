"""
Конвертация времени событий: только Python + zoneinfo → Asia/Ho_Chi_Minh.
Gemini отдаёт original date/time/timezone; сдвиг часов вручную запрещён.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time as dtime
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
_WD_RU = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

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
    """
    Локальные date+time в source_timezone → Asia/Ho_Chi_Minh.
    Без ручного offset; weekday только из converted datetime.
    """
    if not _DATE_RE.match(date_s):
        raise ValueError(f"bad date: {date_s!r}")
    time_norm = _normalize_hhmm(time_s)
    if not time_norm:
        raise ValueError(f"bad time: {time_s!r}")

    zi = resolve_zone(source_timezone)
    if zi is None:
        raise ValueError(f"bad timezone: {source_timezone!r}")

    d = date.fromisoformat(date_s)
    hh, mm = map(int, time_norm.split(":"))
    local_dt = datetime.combine(d, dtime(hh, mm), tzinfo=zi)
    vn_dt = local_dt.astimezone(TARGET_TZ)

    converted = {
        "date": vn_dt.strftime("%Y-%m-%d"),
        "time": vn_dt.strftime("%H:%M"),
        "weekday": get_ru_weekday(vn_dt),
    }
    return converted


def convert_event_to_vn(event: dict[str, Any]) -> tuple[dict[str, str] | None, str]:
    """
    (converted dict | None, time_precision)
    time_precision: exact | estimated | unknown
    """
    log.info("TIME RAW: %s", event)

    date_s, time_s, src_tz = extract_source_fields(event)
    if not src_tz:
        inferred = infer_source_timezone(event)
        if inferred:
            log.info("TIME INFERRED TZ: %s for %r", inferred, event.get("title"))
            src_tz = inferred

    log.info("TIME SOURCE: %s %s %s", date_s, time_s, src_tz)

    if not _DATE_RE.match(date_s):
        return None, "unknown"

    time_norm, is_approx = _parse_time_flexible(time_s)
    if not time_norm:
        log.warning("TIME: no parseable time for %r raw=%r", event.get("title"), time_s)
        return None, "unknown"

    if not src_tz or not is_valid_source_timezone(src_tz):
        log.warning(
            "TIME: unknown timezone for %r tz=%r — не конвертируем, время уточняется",
            event.get("title"),
            src_tz,
        )
        return None, "unknown"

    try:
        converted = convert_event_time(date_s, time_norm, src_tz)
    except Exception as e:
        log.error("TIME CONVERT failed: %s", e, exc_info=True)
        return None, "unknown"

    log.info("TIME CONVERTED TO VN: %s", converted)
    precision = "estimated" if is_approx else "exact"
    return converted, precision
