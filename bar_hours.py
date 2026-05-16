"""
Фильтр событий по рабочим часам Gastrobar (Asia/Ho_Chi_Minh).

Открытие 08:00 — закрытие 06:00 (ночной бар: 08:00–23:59 и 00:00–06:00).
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from config import BAR_CLOSE_TIME, BAR_OPEN_TIME

log = logging.getLogger(__name__)

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

_LONG_KEYWORDS = (
    "main card",
    "main event",
    "ufc",
    "boxing",
    "eurovision",
    "final show",
    "award show",
    "grammy",
    "oscar",
    "golden globe",
    "emmy",
    "livestream",
    "live stream",
    "grand final",
    "grand prix",
    "esports",
    "the international",
    "worlds",
    "champions",
    "wrestlemania",
)

_SHORT_KEYWORDS = (
    "practice",
    "first practice",
    "free practice",
    "fp1",
    "fp2",
    "fp3",
    "press conference",
    "weigh-in",
    "weigh in",
)

_DEFAULT_SHORT_MIN = 90
_DEFAULT_LONG_MIN = 150
_F1_RACE_MIN = 120


def _parse_hhmm(value: str) -> int | None:
    s = str(value or "").strip().removeprefix("≈").strip()
    m = _TIME_RE.match(s)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _fmt_hhmm(minutes: int) -> str:
    minutes = minutes % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _event_blob(e: dict[str, Any]) -> str:
    parts = (
        e.get("title"),
        e.get("subtitle"),
        e.get("category"),
        e.get("league"),
        e.get("why"),
    )
    return " ".join(str(p or "") for p in parts).lower()


def is_f1_allowed_session(e: dict[str, Any]) -> bool:
    """Qualifying / Sprint / Race / Grand Prix — да; practice / FP — нет."""
    b = _event_blob(e)
    if not re.search(r"formula\s*1|\bf1\b", b):
        return True
    if re.search(r"\bqualifying\b|\bqualification\b", b):
        return True
    if re.search(r"\bsprint\b", b):
        return True
    if re.search(r"\brace\b", b) or re.search(r"grand\s+prix", b):
        return True
    return False


def is_f1_excluded_event(e: dict[str, Any]) -> bool:
    """Practice / FP1–FP3 / Practice Session — не в афише."""
    b = _event_blob(e)
    if not re.search(r"formula\s*1|\bf1\b", b):
        return False
    if is_f1_allowed_session(e):
        return False
    if re.search(
        r"\bpractice\b|\bfp[123]\b|free\s+practice|first\s+practice|practice\s+session",
        b,
    ):
        return True
    log.info(
        "f1_excluded: title=%r reason=f1_no_allowed_session",
        e.get("title"),
    )
    return True


def is_f1_practice_event(e: dict[str, Any]) -> bool:
    """Алиас для apply_bar_hours."""
    return is_f1_excluded_event(e)


def infer_duration_minutes(e: dict[str, Any]) -> int:
    raw = e.get("duration_minutes")
    if raw is not None:
        try:
            return max(int(raw), 30)
        except (TypeError, ValueError):
            pass

    b = _event_blob(e)
    if any(k in b for k in _SHORT_KEYWORDS):
        return 60
    if any(k in b for k in _LONG_KEYWORDS):
        return _DEFAULT_LONG_MIN
    if re.search(r"formula\s*1|\bf1\b", b) and re.search(r"\brace\b", b):
        return _F1_RACE_MIN
    if "ufc" in b or "boxing" in b:
        return _DEFAULT_LONG_MIN
    return _DEFAULT_SHORT_MIN


def is_long_event(e: dict[str, Any]) -> bool:
    b = _event_blob(e)
    if any(k in b for k in _LONG_KEYWORDS):
        if any(k in b for k in _SHORT_KEYWORDS) and "main card" not in b and "main event" not in b:
            return False
        return True
    if re.search(r"formula\s*1|\bf1\b", b) and re.search(r"\brace\b", b):
        return infer_duration_minutes(e) > 90
    return infer_duration_minutes(e) >= _DEFAULT_LONG_MIN


def _open_minutes() -> int:
    return _parse_hhmm(BAR_OPEN_TIME) or 8 * 60


def _close_minutes() -> int:
    return _parse_hhmm(BAR_CLOSE_TIME) or 6 * 60


def _in_regular_bar_hours(start_m: int) -> bool:
    """08:00–23:59 или 00:00–06:00."""
    open_m = _open_minutes()
    close_m = _close_minutes()
    if open_m <= start_m <= 23 * 60 + 59:
        return True
    if 0 <= start_m <= close_m:
        return True
    return False


def _in_grey_zone(start_m: int) -> bool:
    """Строго между закрытием и открытием: (06:00, 08:00)."""
    close_m = _close_minutes()
    open_m = _open_minutes()
    return close_m < start_m < open_m


def _ends_before_open(start_m: int, duration_min: int) -> bool:
    """Начало после 06:00 и конец до 08:00 (то же утро)."""
    open_m = _open_minutes()
    close_m = _close_minutes()
    if start_m <= close_m:
        return False
    end_m = start_m + duration_min
    return start_m > close_m and end_m < open_m


def _weekday_ru_for(date_s: str, display_time: str) -> str:
    _WD = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")
    try:
        d = date.fromisoformat(date_s)
        return _WD[d.weekday()]
    except ValueError:
        return ""


def apply_bar_hours(event: dict[str, Any]) -> dict[str, Any] | None:
    """
    Проставляет display_time, note, bar_hours_decision.
    Возвращает None, если событие не показываем в афише.
    """
    if is_f1_excluded_event(event):
        log.info(
            "bar_hours: title=%r original_time=%s decision=exclude reason=f1_practice_or_fp",
            event.get("title"),
            event.get("time"),
        )
        return None

    time_raw = str(event.get("time", "")).strip()
    start_m = _parse_hhmm(time_raw)
    original_time = time_raw or "?"
    date_s = str(event.get("date", "")).strip()

    if start_m is None:
        out = dict(event)
        out["display_time"] = str(event.get("time_display") or event.get("time") or "время уточняется")
        out["original_time"] = original_time
        out["bar_hours_decision"] = "keep_unknown_time"
        out["bar_hours_reason"] = "no_parseable_time"
        log.info(
            "bar_hours: title=%r original_time=%s display_time=%s decision=keep reason=no_parseable_time",
            event.get("title"),
            original_time,
            out["display_time"],
        )
        return out

    duration_min = infer_duration_minutes(event)
    long_ev = is_long_event(event)
    open_m = _open_minutes()
    display_m = start_m
    note = ""
    decision = "keep_as_is"
    reason = "inside_bar_hours"

    if _ends_before_open(start_m, duration_min):
        log.info(
            "bar_hours: title=%r original_time=%s display_time=%s decision=exclude "
            "reason=ends_before_open duration_min=%s",
            event.get("title"),
            original_time,
            _fmt_hhmm(start_m),
            duration_min,
        )
        return None

    if _in_regular_bar_hours(start_m):
        decision = "keep_as_is"
        reason = "inside_bar_hours"
    elif _in_grey_zone(start_m):
        if long_ev:
            display_m = open_m
            note = "показываем с открытия"
            decision = "shift_to_open"
            reason = "grey_zone_long_event"
        else:
            log.info(
                "bar_hours: title=%r original_time=%s display_time=%s decision=exclude "
                "reason=outside_bar_hours_short_event duration_min=%s long=%s",
                event.get("title"),
                original_time,
                _fmt_hhmm(start_m),
                duration_min,
                long_ev,
            )
            return None
    else:
        log.info(
            "bar_hours: title=%r original_time=%s decision=exclude reason=outside_bar_hours",
            event.get("title"),
            original_time,
        )
        return None

    display_time = _fmt_hhmm(display_m)
    out = dict(event)
    out["original_time"] = _fmt_hhmm(start_m) if original_time != "?" else original_time
    out["display_time"] = display_time
    out["time_display"] = (
        f"≈{display_time}" if out.get("time_precision") == "estimated" else display_time
    )
    if note:
        out["note"] = note
    else:
        out.pop("note", None)
    out["bar_hours_decision"] = decision
    out["bar_hours_reason"] = reason

    if date_s and display_m != start_m:
        wd = _weekday_ru_for(date_s, display_time)
        if wd:
            out["weekday"] = wd

    log.info(
        "bar_hours: title=%r original_time=%s display_time=%s decision=%s reason=%s long=%s",
        event.get("title"),
        out["original_time"],
        display_time,
        decision,
        reason,
        long_ev,
    )
    return out


def filter_events_for_bar_hours(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for e in events:
        adjusted = apply_bar_hours(e)
        if adjusted:
            kept.append(adjusted)
    log.info("bar_hours filter: in=%s out=%s", len(events), len(kept))
    return kept
