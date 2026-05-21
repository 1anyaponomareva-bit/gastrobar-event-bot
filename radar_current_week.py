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
    r"valorant\s+masters|lec|lcs|lck|vct|pgl|dreamhack|cs2|dota|dreamleague|"
    r"dream\s+league|falcons|asia\s+championship)\b",
    re.I,
)

_UFC_BOXING_RE = re.compile(r"\bufc\b|mma|boxing|heavyweight|title\s+fight", re.I)
_NBA_NHL_RE = re.compile(r"\bnba\b|\bnhl\b|stanley\s+cup", re.I)
_F1_RE = re.compile(r"formula\s*1|\bf1\b|grand\s+prix", re.I)
_FOOTBALL_RE = re.compile(
    r"football|soccer|premier\s+league|\bepl\b|champions\s+league|\bucl\b|"
    r"europa\s+league|\buel\b|la\s+liga|serie\s+a|bundesliga|ligue\s+1|"
    r"espanyol|real\s+sociedad|real\s+madrid|barcelona|atletico",
    re.I,
)
_HOCKEY_BLOB_RE = re.compile(
    r"\b(nhl|khl|stanley|iihf|world\s+championship|hockey|playoff)\b",
    re.I,
)


def today_local() -> datetime:
    return datetime.now(TARGET_TZ)


def current_week_bounds() -> tuple[date, date]:
    """Legacy: календарные границы ~72 ч (today .. today+2)."""
    t = today_local().date()
    return t, t + timedelta(days=2)


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
    """NEXT72: now <= event_local <= now + 72h (VN)."""
    from next24 import resolve_event_local_datetime_vn, vn_now

    dt = resolve_event_local_datetime_vn(e)
    if dt is None:
        return False
    now_local = vn_now()
    end_local = now_local + timedelta(hours=72)
    return now_local <= dt <= end_local


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
    """Разрешить Gemini Search без API: Eurovision, F1, NHL/NBA плей-офф с locked VN time."""
    if is_source_verified(e):
        return True
    b = _category_blob(e)
    if "eurovision" in b:
        return True
    if _F1_RE.search(b) and e.get("utc_datetime"):
        return True
    if re.search(r"\b(oscar|grammy|emmy|golden\s+globe|academy\s+award)\b", b, re.I):
        return True
    if e.get("local_datetime") and e.get("utc_datetime"):
        if _NBA_NHL_RE.search(b) and re.search(
            r"playoff|conference\s+final|stanley|finals|\bfinal\b", b, re.I
        ):
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


def has_source_timezone(e: dict[str, Any]) -> bool:
    for key in (
        "source_timezone",
        "original_timezone",
        "timezone",
    ):
        raw = str(e.get(key, "")).strip()
        if raw and raw.lower() not in ("unknown", "none", ""):
            return True
    return False


def is_in_week_window(e: dict[str, Any]) -> bool:
    return is_in_current_week(e)


def event_watchability_score(e: dict[str, Any]) -> int:
    score = int(e.get("watchability_score", 0) or e.get("radar_priority_score", 0))
    if score > 0:
        return score
    from watchability import enrich_watchability

    return int(enrich_watchability(dict(e)).get("watchability_score", 0))


def is_trusted_radar_category(e: dict[str, Any]) -> bool:
    """Категории, для которых medium + Gemini Search допустимы без API match."""
    b = _category_blob(e)
    cat = str(e.get("category", "")).upper()
    if _F1_RE.search(b):
        return True
    if "ESPORT" in cat or _ESPORTS_ALLOW_RE.search(b):
        return True
    if "HOCKEY" in cat or _HOCKEY_BLOB_RE.search(b):
        return True
    if _NBA_NHL_RE.search(b):
        return True
    if "FOOT" in cat or "SOCCER" in cat or _FOOTBALL_RE.search(b):
        return True
    if "eurovision" in b:
        return True
    return False


def passes_medium_weekly_radar(e: dict[str, Any]) -> bool:
    """
    Medium/high из Gemini Search: datetime + неделя + timezone + trusted category.
    Не требует API-SPORTS exact match.
    """
    from event_participants import has_matchup_in_title
    from next24 import resolve_event_local_datetime_vn

    conf = str(e.get("confidence", "medium")).lower()
    if conf not in ("high", "medium"):
        return False

    title = str(e.get("title", "")).strip()
    if len(title) < 4:
        return False

    if resolve_event_local_datetime_vn(e) is None:
        return False

    if not is_in_week_window(e):
        return False

    if not has_source_timezone(e):
        return False

    if not is_trusted_radar_category(e):
        return False

    via = str(e.get("verified_via", "")).lower()
    if "gemini" not in via and "api-sports" not in via:
        return False

    if not has_matchup_in_title(title) and "FOOT" not in str(e.get("category", "")).upper():
        if not _F1_RE.search(_category_blob(e)) and not _ESPORTS_ALLOW_RE.search(_category_blob(e)):
            return False

    from config import RADAR_MIN_WATCHABILITY

    floor = max(12, RADAR_MIN_WATCHABILITY - 18)
    if event_watchability_score(e) < floor:
        b = _category_blob(e)
        if _FOOTBALL_RE.search(b) and has_matchup_in_title(title):
            return True
        if _F1_RE.search(b) or _HOCKEY_BLOB_RE.search(b) or _ESPORTS_ALLOW_RE.search(b):
            return True
        return False

    return True


def log_radar_gate_reject(
    e: dict[str, Any],
    reject_reason_exact: str,
    *,
    phase: str = "",
) -> None:
    from next24 import resolve_event_local_datetime_vn

    dt = resolve_event_local_datetime_vn(e)
    log.info(
        "rejected_unverified_event: title=%r category=%r via=%r confidence=%r "
        "watchability_score=%s local_datetime=%s is_in_week_window=%s "
        "has_source_timezone=%s reject_reason_exact=%s phase=%s",
        e.get("title"),
        e.get("category"),
        e.get("verified_via"),
        e.get("confidence"),
        event_watchability_score(e),
        dt.isoformat() if dt else None,
        is_in_week_window(e),
        has_source_timezone(e),
        reject_reason_exact,
        phase,
    )


def radar_gate_reject_reason(
    e: dict[str, Any] | None,
    *,
    phase: str = "verify",
    allow_gemini_discovery: bool = False,
) -> str | None:
    """Точная причина отказа или None если событие проходит gate."""
    if not e:
        return "missing_event"

    title = str(e.get("title", "")).strip()
    if not title:
        return "missing_title"

    if not str(e.get("local_datetime") or e.get("utc_datetime") or "").strip():
        if not (e.get("date") and e.get("time")):
            return "missing_datetime"

    tz = str(e.get("timezone") or TARGET_TZ.key)
    if "ho_chi_minh" not in tz.lower() and not e.get("local_datetime"):
        return "missing_vn_timezone"

    hist = detect_historical_hallucination(e)
    if hist:
        return hist

    if not is_in_week_window(e):
        return "outside_week_window"

    verified = is_source_verified(e)
    medium_ok = passes_medium_weekly_radar(e)

    if requires_strict_verification(e) and not verified and not medium_ok:
        if str(e.get("verified_via", "")).upper() == "API-SPORTS":
            pass
        else:
            return "strict_sport_requires_api_match"

    if not verified and not medium_ok:
        if allow_gemini_discovery or allows_gemini_discovery_only(e):
            conf = str(e.get("confidence", "medium")).lower()
            if conf not in ("high", "medium"):
                return f"confidence_{conf}_not_allowed"
        else:
            if not has_source_timezone(e):
                return "gemini_no_source_timezone"
            if not is_trusted_radar_category(e):
                return "untrusted_category"
            conf = str(e.get("confidence", "medium")).lower()
            if conf not in ("high", "medium"):
                return f"confidence_{conf}_not_allowed"
            return "not_source_verified_and_not_medium_trusted"

    return None


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
    Финальный gate: datetime + неделя + не историческое + title/category.
    Medium Gemini Search с trusted category — без API exact match.
    """
    reject = radar_gate_reject_reason(
        e, phase=phase, allow_gemini_discovery=allow_gemini_discovery
    )
    if reject:
        log_radar_gate_reject(e, reject, phase=phase)
        return None

    out = apply_acceptance_flags(e, source_verified=is_source_verified(e))
    log.info(
        "accepted_current_week_event: title=%r via=%r confidence=%r "
        "watchability=%s medium_trusted=%s source_verified=%s phase=%s",
        out.get("title"),
        out.get("verified_via"),
        out.get("confidence"),
        event_watchability_score(e),
        passes_medium_weekly_radar(e),
        out.get("source_verified"),
        phase,
    )
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
