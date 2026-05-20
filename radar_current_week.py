"""
Event Radar: только LIVE-события текущей недели (Asia/Ho_Chi_Minh).

Gemini — discovery/текст, не источник истины.
Принятие: обязательные поля + current_week + source_verified (API / fixture lock).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

TARGET_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

_API_VERIFIED_MARKERS = (
    "api-sports",
    "api_sports",
    "apisports",
    "fixture_utc",
    "api_sports_match",
    "api_sports_fallback",
)

# Известные галлюцинации / устаревшие карточки (память Gemini)
_HISTORICAL_TITLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"tyson\s+fury.*usyk|usyk.*tyson\s+fury|fury\s+vs\.?\s*usyk", re.I),
    re.compile(r"\bufc\s*302\b", re.I),
    re.compile(r"\bufc\s*300\b(?!\s*live)", re.I),
    re.compile(r"conor\s+mcgregor.*khabib|khabib.*mcgregor", re.I),
    re.compile(r"jake\s+paul.*tyson|tyson.*jake\s+paul", re.I),
    re.compile(r"canelo.*ggg|ggg.*canelo", re.I),
)

# Подозрительные названия турниров (частые ошибки модели)
_FAKE_ESPORTS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\biem\s+dallas\b", re.I),
    re.compile(r"\biem\s+katowice\s+202[0-4]\b", re.I),
    re.compile(r"\bblast\s+world\s+final\s+202[0-4]\b", re.I),
)

_ESPORTS_ALLOW_RE = re.compile(
    r"\b(iem|esl|blast|major|msi|worlds|champions|the\s+international|ti\s*\d|"
    r"valorant\s+masters|lec|lcs|lck|vct|pgl|dreamhack)\b",
    re.I,
)

_UFC_BOXING_RE = re.compile(r"\bufc\b|mma|boxing|heavyweight|title\s+fight", re.I)
_NBA_NHL_RE = re.compile(r"\bnba\b|\bnhl\b|stanley\s+cup", re.I)
_F1_RE = re.compile(r"formula\s*1|\bf1\b|grand\s+prix", re.I)
_FOOTBALL_RE = re.compile(
    r"football|soccer|premier\s+league|\bepl\b|champions\s+league|\bucl\b|"
    r"europa\s+league|\buel\b|la\s+liga|serie\s+a|bundesliga",
    re.I,
)


def today_local() -> datetime:
    return datetime.now(TARGET_TZ)


def current_week_bounds() -> tuple[date, date]:
    """week_start = сегодня (VN), week_end = сегодня + 7 дней."""
    t = today_local().date()
    return t, t + timedelta(days=7)


def _event_local_date(e: dict[str, Any]) -> date | None:
    ld = e.get("local_datetime")
    if isinstance(ld, datetime):
        if ld.tzinfo is None:
            ld = ld.replace(tzinfo=TARGET_TZ)
        return ld.astimezone(TARGET_TZ).date()
    if isinstance(ld, str) and len(ld) >= 10:
        try:
            return date.fromisoformat(ld[:10])
        except ValueError:
            pass
    ds = str(e.get("date") or e.get("original_date") or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", ds):
        try:
            return date.fromisoformat(ds)
        except ValueError:
            return None
    return None


def is_in_current_week(e: dict[str, Any]) -> bool:
    d = _event_local_date(e)
    if d is None:
        return False
    start, end = current_week_bounds()
    return start <= d <= end


def is_source_verified(e: dict[str, Any]) -> bool:
    via = str(e.get("verified_via") or "").lower()
    reason = str(e.get("verification_reason") or "").lower()
    if any(m in via or m in reason for m in _API_VERIFIED_MARKERS):
        return True
    if e.get("fixture_utc_iso") or e.get("league_id") is not None:
        return True
    if str(e.get("confidence", "")).lower() == "high" and e.get("utc_datetime"):
        if "api" in via or "api" in reason:
            return True
    return False


def _category_blob(e: dict[str, Any]) -> str:
    parts = (e.get("title"), e.get("subtitle"), e.get("league"), e.get("category"))
    return " ".join(str(p or "") for p in parts).lower()


def detect_historical_hallucination(e: dict[str, Any]) -> str | None:
    title = str(e.get("title", ""))
    blob = f"{title} {_category_blob(e)}"
    for pat in _HISTORICAL_TITLE_PATTERNS:
        if pat.search(blob):
            return "rejected_historical_event"

    for pat in _FAKE_ESPORTS_PATTERNS:
        if pat.search(blob):
            return "rejected_fake_tournament"

    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", title)]
    cy = today_local().year
    for y in years:
        if y < cy - 1 or y > cy + 1:
            return "rejected_historical_event"
        if y < cy and not is_in_current_week(e):
            return "rejected_old_event"

    d = _event_local_date(e)
    if d is not None:
        start, _ = current_week_bounds()
        if d < start:
            return "rejected_old_event"

    return None


def requires_strict_verification(e: dict[str, Any]) -> bool:
    """Футбол / UFC / esports / NBA·NHL плей-офф — только API/fixture."""
    b = _category_blob(e)
    cat = str(e.get("category", "")).upper()
    if "FOOT" in cat or "SOCCER" in cat or _FOOTBALL_RE.search(b):
        return True
    if _UFC_BOXING_RE.search(b) or "UFC" in cat or "BOX" in cat:
        return True
    if "ESPORT" in cat or _ESPORTS_ALLOW_RE.search(b):
        return True
    if _NBA_NHL_RE.search(b):
        if re.search(r"playoff|conference\s+final|finals|\bfinal\b", b, re.I):
            return True
    return False


def soft_medium_allowed() -> bool:
    return os.getenv("RADAR_SOFT_MEDIUM", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def allows_gemini_discovery_only(e: dict[str, Any]) -> bool:
    """Разрешить Gemini Search без API: Eurovision, F1 с locked UTC, премии."""
    if is_source_verified(e):
        return True
    b = _category_blob(e)
    if "eurovision" in b:
        return True
    if _F1_RE.search(b) and e.get("utc_datetime"):
        return True
    if re.search(r"\b(oscar|grammy|emmy|golden\s+globe|academy\s+award)\b", b, re.I):
        return True
    return False


def log_radar_validation(reason: str, e: dict[str, Any], *, phase: str = "") -> None:
    """reason — один из rejected_* / accepted_current_week_event."""
    title = str(e.get("title", ""))[:100]
    extra = (
        f"phase={phase} date={e.get('date')} local={e.get('local_datetime')} "
        f"via={e.get('verified_via')} conf={e.get('confidence')}"
    )
    log.info("%s: title=%r %s", reason, title, extra)


def apply_acceptance_flags(e: dict[str, Any], *, source_verified: bool) -> dict[str, Any]:
    e = dict(e)
    e["timezone"] = "Asia/Ho_Chi_Minh"
    e["current_week_validated"] = True
    e["source_verified"] = bool(source_verified)
    return e


def validate_radar_event(
    e: dict[str, Any] | None,
    *,
    phase: str = "verify",
    allow_gemini_discovery: bool = False,
) -> dict[str, Any] | None:
    """
    Финальный gate перед афишей.
    allow_gemini_discovery: True только для Eurovision / крупных live-шоу без API.
    """
    if not e:
        return None

    title = str(e.get("title", "")).strip()
    if not title:
        log_radar_validation("rejected_missing_datetime", e, phase=phase)
        return None

    if not str(e.get("local_datetime") or e.get("utc_datetime") or "").strip():
        if not (e.get("date") and e.get("time")):
            log_radar_validation("rejected_missing_datetime", e, phase=phase)
            return None

    tz = str(e.get("timezone") or TARGET_TZ.key)
    if "ho_chi_minh" not in tz.lower() and not e.get("local_datetime"):
        log_radar_validation("rejected_missing_datetime", e, phase=phase)
        return None

    hist = detect_historical_hallucination(e)
    if hist:
        log_radar_validation(hist, e, phase=phase)
        return None

    if not is_in_current_week(e):
        log_radar_validation("rejected_old_event", e, phase=phase)
        return None

    verified = is_source_verified(e)
    if requires_strict_verification(e) and not verified:
        log_radar_validation("rejected_unverified_event", e, phase=phase)
        return None

    if not verified:
        gemini_ok = allow_gemini_discovery or allows_gemini_discovery_only(e)
        if not gemini_ok:
            log_radar_validation("rejected_unverified_event", e, phase=phase)
            return None
        conf = str(e.get("confidence", "medium")).lower()
        if conf not in ("high", "medium"):
            log_radar_validation("rejected_unverified_event", e, phase=phase)
            return None

    out = apply_acceptance_flags(e, source_verified=verified)
    log_radar_validation("accepted_current_week_event", out, phase=phase)
    return out


def filter_radar_events(
    events: list[dict[str, Any]],
    *,
    phase: str = "pipeline",
    allow_gemini_discovery: bool = False,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in events:
        v = validate_radar_event(
            e,
            phase=phase,
            allow_gemini_discovery=allow_gemini_discovery,
        )
        if v:
            out.append(v)
    return out


def discovery_candidate_ok(raw: dict[str, Any]) -> bool:
    """Ранняя отсечка кандидатов Gemini до verify."""
    title = str(raw.get("title", "")).strip()
    if not title:
        return False
    hist = detect_historical_hallucination(
        {
            "title": title,
            "subtitle": raw.get("subtitle"),
            "category": raw.get("category"),
            "date": raw.get("date"),
        }
    )
    if hist:
        log_radar_validation(hist, raw, phase="discovery")
        return False
    ds = str(raw.get("date", "")).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", ds):
        try:
            d = date.fromisoformat(ds)
            start, end = current_week_bounds()
            if d < start or d > end:
                log_radar_validation("rejected_old_event", raw, phase="discovery")
                return False
        except ValueError:
            return False
    else:
        log_radar_validation("rejected_missing_datetime", raw, phase="discovery")
        return False
    return True
