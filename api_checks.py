"""Проверки внешних API: Gemini, API-SPORTS, BetBoom."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from config import GEMINI_API_KEY, SPORTS_API_KEY, TIMEZONE
from gemini_client import (
    effective_gemini_model,
    gemini_search_disabled_reason,
    gemini_search_quota_message,
    generate_radar_content_sync,
    is_gemini_search_available,
    is_gemini_quota_error,
    log_gemini_error,
)

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CheckResult:
    name: str
    ok: bool
    details: str | None = None


@dataclass(slots=True)
class ComponentStatus:
    """Статус одного источника для /check."""

    label: str
    status: str
    detail: str = ""


def _safe_err(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


def _status_line(comp: ComponentStatus) -> str:
    icon = "✅" if comp.status == "ok" else "❌" if comp.status == "error" else "⚠️"
    extra = f" — {comp.detail}" if comp.detail else ""
    return f"{icon} {comp.label}: {comp.status}{extra}"


async def check_gemini_text() -> ComponentStatus:
    if not GEMINI_API_KEY:
        return ComponentStatus("Gemini text", "error", "GEMINI_API_KEY missing")
    try:
        text = await asyncio.to_thread(
            generate_radar_content_sync,
            "Ответь одним словом: ok",
            use_search=False,
            purpose="check_gemini_text",
        )
        if text.lower().strip() != "ok":
            return ComponentStatus("Gemini text", "error", f"unexpected: {text!r}")
        return ComponentStatus(
            "Gemini text",
            "ok",
            effective_gemini_model(),
        )
    except Exception as e:
        log_gemini_error("check_gemini_text", e)
        return ComponentStatus("Gemini text", "error", _safe_err(e))


async def check_gemini_search() -> ComponentStatus:
    if not GEMINI_API_KEY:
        return ComponentStatus("Gemini Search grounding", "error", "GEMINI_API_KEY missing")
    if not is_gemini_search_available():
        reason = gemini_search_disabled_reason() or "quota_exhausted"
        return ComponentStatus("Gemini Search grounding", reason, gemini_search_quota_message())
    try:
        from gemini_client import _generate_sync

        await asyncio.to_thread(
            _generate_sync,
            contents='Use Google Search: reply JSON {"ok":true}',
            use_search=True,
            max_retries=1,
        )
        return ComponentStatus("Gemini Search grounding", "ok", "grounding available")
    except Exception as e:
        if is_gemini_quota_error(e) or not is_gemini_search_available():
            return ComponentStatus(
                "Gemini Search grounding",
                "quota_exhausted",
                gemini_search_quota_message(),
            )
        log_gemini_error("check_gemini_search", e)
        return ComponentStatus("Gemini Search grounding", "error", _safe_err(e))


async def check_api_sports() -> ComponentStatus:
    if not SPORTS_API_KEY:
        return ComponentStatus("API-Sports", "error", "SPORTS_API_KEY missing")

    from api_sports_status import classify_errors_payload, get_last_sport_status, probe_sport

    cached = get_last_sport_status()
    if cached:
        worst = "ok"
        worst_detail = ""
        rank = {"ok": 0, "date_range": 1, "rateLimit": 2, "suspended": 3, "unavailable": 4}
        for sport, st in cached.items():
            head = str(st).split(":")[0]
            if rank.get(head, 99) > rank.get(worst, 0):
                worst = head
                worst_detail = f"{sport}: {st}"
        if worst == "ok":
            return ComponentStatus("API-Sports", "ok", "last probe ok")
        if worst == "rateLimit":
            return ComponentStatus("API-Sports", "rateLimit", worst_detail)
        if worst == "suspended":
            return ComponentStatus("API-Sports", "suspended", worst_detail)
        return ComponentStatus("API-Sports", worst, worst_detail or str(cached))

    today = datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()
    headers = {"x-apisports-key": SPORTS_API_KEY}
    url = f"https://v3.football.api-sports.io/fixtures?date={today}"
    try:
        health, detail = await probe_sport("football", url, headers=headers)
        if health == "ok":
            return ComponentStatus("API-Sports", "ok", f"football {today}")
        if health == "rateLimit":
            return ComponentStatus("API-Sports", "rateLimit", detail[:200])
        if health == "suspended":
            return ComponentStatus("API-Sports", "suspended", detail[:200])
        return ComponentStatus("API-Sports", health, detail[:200])
    except Exception as e:
        return ComponentStatus("API-Sports", "error", _safe_err(e))


async def check_betboom_parser() -> ComponentStatus:
    from betboom_cache import load_betboom_cache
    from config import BETBOOM_USE_PLAYWRIGHT

    cached = await load_betboom_cache(allow_stale=True)
    if cached:
        return ComponentStatus(
            "BetBoom parser",
            "cache",
            f"{len(cached)} events in snapshot",
        )
    if not BETBOOM_USE_PLAYWRIGHT:
        return ComponentStatus(
            "BetBoom parser",
            "ok",
            "no cache yet (Playwright off)",
        )
    return ComponentStatus(
        "BetBoom parser",
        "ok",
        "no cache yet — run /events to fetch line",
    )


async def run_connectivity_check() -> str:
    """Полный отчёт для /check."""
    gemini_text, gemini_search, api_sports, betboom = await asyncio.gather(
        check_gemini_text(),
        check_gemini_search(),
        check_api_sports(),
        check_betboom_parser(),
    )
    lines = [
        "🔌 Проверка источников Event Radar",
        "",
        _status_line(gemini_text),
        _status_line(gemini_search),
        _status_line(api_sports),
        _status_line(betboom),
        "",
        "Event Radar discovery: BetBoom → cache → API-SPORTS (если включён fallback).",
        "Gemini Search не используется для поиска матчей (только текст постов).",
    ]
    return "\n".join(lines)


# --- legacy helpers (daily / др.) ---


async def check_gemini() -> CheckResult:
    comp = await check_gemini_text()
    return CheckResult(
        name="Gemini API",
        ok=comp.status == "ok",
        details=comp.detail or comp.status,
    )


async def check_sports_api() -> CheckResult:
    comp = await check_api_sports()
    return CheckResult(
        name="API-SPORTS",
        ok=comp.status == "ok",
        details=comp.detail or comp.status,
    )
