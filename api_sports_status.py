"""
API-SPORTS: классификация ошибок (suspended / rateLimit / date range) и /api_status.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# fetch_note для pipeline / UI
API_NOTE_SUSPENDED = "api_suspended"
API_NOTE_RATE_LIMIT = "api_rate_limit"
API_NOTE_UNAVAILABLE = "api_unavailable"
API_NOTE_NO_KEY = "api_no_key"
API_NOTE_DATE_RANGE = "api_date_range"
API_NOTE_OK = "api_ok"

API_FAILURE_NOTES = frozenset(
    {
        API_NOTE_SUSPENDED,
        API_NOTE_RATE_LIMIT,
        API_NOTE_UNAVAILABLE,
        API_NOTE_NO_KEY,
        API_NOTE_DATE_RANGE,
    }
)

_STATUS_RANK = {
    "ok": 0,
    "date_range": 1,
    "rateLimit": 2,
    "suspended": 3,
    "unavailable": 4,
    "no_key": 5,
    "skipped": 6,
}


class ApiSportsError(Exception):
    """Ошибка ответа API-SPORTS (HTTP 200 + errors или HTTP fail)."""

    def __init__(self, health: str, detail: str, *, sport: str = "") -> None:
        self.health = health
        self.detail = detail
        self.sport = sport
        super().__init__(detail)


@dataclass
class ApiCollectResult:
    events: list[dict[str, Any]]
    fetch_note: str | None = None
    sport_status: dict[str, str] = field(default_factory=dict)
    abort_reason: str | None = None


def is_api_failure_note(note: str | None) -> bool:
    return note in API_FAILURE_NOTES


def is_truly_empty_radar_note(note: str | None) -> bool:
    """«Пусто» только если API отработал, а в окне нет матчей."""
    if note is None:
        return True
    if is_api_failure_note(note):
        return False
    return note in (
        "api_unified",
        "api_gemini_unified",
        "api_ok_empty",
        "api_window_empty",
        "api_window_only",
        "api_filter_empty",
        "now24_emergency",
        "now24_soft",
        "weekly_cache_today",
        "weekly_cache_stale",
        "weekly_cache_pipeline",
        "weekly_cache_api_down",
        "betboom_ok",
        "betboom_cache",
        "api_sports_fallback",
    )


def classify_errors_payload(errors: Any) -> tuple[str, str]:
    """
    Вернуть (health, detail).
    health: ok | suspended | rateLimit | date_range | unknown
    """
    if not errors:
        return "ok", ""

    parts: list[str] = []
    if isinstance(errors, dict):
        for k, v in errors.items():
            parts.append(f"{k}: {v}")
    elif isinstance(errors, list):
        for item in errors:
            parts.append(str(item))
    else:
        parts.append(str(errors))

    blob = " ".join(parts).lower()
    detail = "; ".join(parts)[:400]

    if "suspended" in blob or "account is suspended" in blob:
        return "suspended", detail
    if "too many requests" in blob or "ratelimit" in blob or "rate limit" in blob:
        return "rateLimit", detail
    if "date range" in blob or "free plan" in blob and "date" in blob:
        return "date_range", detail
    if "requests" in blob and ("limit" in blob or "exceeded" in blob):
        return "rateLimit", detail
    return "unknown", detail


def fetch_note_from_health(health: str) -> str | None:
    if health == "suspended":
        return API_NOTE_SUSPENDED
    if health == "rateLimit":
        return API_NOTE_RATE_LIMIT
    if health == "date_range":
        return API_NOTE_DATE_RANGE
    if health in ("unavailable", "unknown"):
        return API_NOTE_UNAVAILABLE
    if health == "no_key":
        return API_NOTE_NO_KEY
    return None


def format_api_failure_user_message(note: str | None) -> str:
    if note == API_NOTE_SUSPENDED:
        return (
            "API-SPORTS аккаунт suspended.\n"
            "Проверьте dashboard.api-football.com."
        )
    if note == API_NOTE_RATE_LIMIT:
        return (
            "API-SPORTS лимит запросов исчерпан.\n"
            "Попробуйте позже или используйте кэш."
        )
    if note == API_NOTE_DATE_RANGE:
        return (
            "API-SPORTS: ограничение Free plan по диапазону дат.\n"
            "Запрашиваем только today / tomorrow / day+2."
        )
    if note == API_NOTE_NO_KEY:
        return "API-SPORTS: не задан SPORTS_API_KEY в переменных окружения."
    if note == API_NOTE_UNAVAILABLE:
        return "Нет сохранённой афиши, API сейчас недоступен."
    return "API-SPORTS: ошибка загрузки данных."


def _worst_status(a: str, b: str) -> str:
    return a if _STATUS_RANK.get(a, 0) >= _STATUS_RANK.get(b, 0) else b


@dataclass
class _RunState:
    abort: bool = False
    abort_reason: str | None = None
    worst: str = "ok"
    sport_status: dict[str, str] = field(default_factory=dict)

    def record(self, sport: str, status: str, detail: str = "") -> None:
        self.sport_status[sport] = status if status == "ok" else f"{status}: {detail[:80]}"
        self.worst = _worst_status(self.worst, status)
        if status == "suspended":
            self.abort = True
            self.abort_reason = API_NOTE_SUSPENDED
            log.error("API-SPORTS SUSPENDED — aborting further sport requests")

    def finalize_note(self) -> str | None:
        return fetch_note_from_health(self.worst) if self.worst != "ok" else None


_last_sport_status: dict[str, str] = {}


def get_last_sport_status() -> dict[str, str]:
    return dict(_last_sport_status)


def set_last_sport_status(status: dict[str, str]) -> None:
    global _last_sport_status
    _last_sport_status = dict(status)


async def probe_sport(
    sport: str,
    url: str,
    *,
    headers: dict[str, str],
) -> tuple[str, str]:
    """Один probe-запрос для /api_status."""
    from sports_events import _get_json

    try:
        data = await _get_json(url, headers=headers)
        errs = data.get("errors") or {}
        health, detail = classify_errors_payload(errs)
        if health != "ok":
            return health, detail
        n = len(data.get("response") or [])
        return "ok", f"{n} items"
    except ApiSportsError as e:
        return e.health, e.detail
    except Exception as e:
        log.exception("API_STATUS probe_sport %s failed", sport, exc_info=True)
        return "unavailable", str(e)[:120]


async def format_api_status_report() -> str:
    """Текст для команды /api_status."""
    global _last_sport_status
    from config import SPORTS_API_KEY

    if not SPORTS_API_KEY:
        return "API-SPORTS: SPORTS_API_KEY не задан."

    today = datetime.now(VN_TZ).date().isoformat()
    headers = {"x-apisports-key": SPORTS_API_KEY}
    probes = (
        ("football", f"https://v3.football.api-sports.io/fixtures?date={today}"),
        ("hockey", f"https://v1.hockey.api-sports.io/games?date={today}"),
        ("basketball", f"https://v1.basketball.api-sports.io/games?date={today}"),
        ("formula1", f"https://v1.formula-1.api-sports.io/races?date={today}"),
        ("esports", f"https://v1.esports.api-sports.io/games?date={today}"),
    )
    lines = ["API-SPORTS status", f"date probe: {today}", ""]
    sport_status: dict[str, str] = {}
    worst = "ok"
    for sport, url in probes:
        health, detail = await probe_sport(sport, url, headers=headers)
        sport_status[sport] = health
        worst = _worst_status(worst, health)
        lines.append(f"{sport}: {health}" + (f" — {detail}" if detail else ""))
    _last_sport_status = sport_status
    lines.append("")
    note = fetch_note_from_health(worst)
    if note:
        lines.append(f"overall: {note}")
        lines.append("")
        lines.append(format_api_failure_user_message(note))
    else:
        lines.append("overall: ok")
    return "\n".join(lines)
