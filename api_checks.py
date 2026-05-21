"""Проверки внешних API: Gemini и API-SPORTS.

Важно: эти функции НЕ должны валить бота при ошибках.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from config import GEMINI_API_KEY, SPORTS_API_KEY, TIMEZONE
from gemini_client import effective_gemini_model, generate_radar_content_sync, log_gemini_error

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CheckResult:
    name: str
    ok: bool
    details: str | None = None


def _safe_err(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


async def check_gemini() -> CheckResult:
    if not GEMINI_API_KEY:
        msg = "GEMINI_API_KEY is missing"
        log.warning("Gemini API error: %s", msg)
        return CheckResult(name="Gemini API", ok=False, details=msg)

    try:
        text = await asyncio.to_thread(
            generate_radar_content_sync,
            "Ответь одним словом: ok",
            use_search=False,
        )
        if text.lower() != "ok":
            msg = f"неожиданный ответ: {text!r}"
            log.warning("Gemini API error: %s", msg)
            return CheckResult(name="Gemini API", ok=False, details=msg)
        log.info("Gemini API connected (model=%s)", effective_gemini_model())
        return CheckResult(
            name="Gemini API",
            ok=True,
            details=f"connected · {effective_gemini_model()}",
        )
    except Exception as e:
        msg = log_gemini_error("check_gemini", e)
        return CheckResult(name="Gemini API", ok=False, details=msg)


async def check_sports_api() -> CheckResult:
    if not SPORTS_API_KEY:
        msg = "SPORTS_API_KEY is missing"
        log.warning("API-SPORTS error: %s", msg)
        return CheckResult(name="API-SPORTS", ok=False, details=msg)

    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    date_vn = today.isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()
    # Free plan: только date= (параметр next= недоступен).
    probe_urls = (
        f"https://v3.football.api-sports.io/fixtures?date={date_vn}",
        f"https://v3.football.api-sports.io/fixtures?date={tomorrow}",
        f"https://v1.hockey.api-sports.io/games?date={date_vn}",
    )
    headers = {"x-apisports-key": SPORTS_API_KEY}

    try:
        last_fail: str | None = None
        async with httpx.AsyncClient(timeout=10.0) as client:
            for url in probe_urls:
                r = await client.get(url, headers=headers)
                if r.status_code != 200:
                    last_fail = f"HTTP {r.status_code}: {r.text[:300]}"
                    log.error("API-SPORTS error (%s): %s", url, last_fail)
                    continue

                data: dict[str, Any] = r.json()
                api_errors = data.get("errors")
                if api_errors:
                    err_msg = str(api_errors)[:400]
                    err_low = err_msg.lower()
                    if "next parameter" in err_low:
                        log.info(
                            "API-SPORTS skip unsupported next= on free plan (%s)",
                            url,
                        )
                        continue
                    log.error("API-SPORTS errors payload: %s", err_msg)
                    last_fail = f"errors из ответа API: {err_msg}"
                    continue

                resp = data.get("response") or []
                results_hint = data.get("results")
                paging_info = data.get("paging")

                if not isinstance(resp, list):
                    last_fail = f"response не список: keys={list(data.keys())}"
                    log.warning("API-SPORTS (%s): %s", url, last_fail)
                    continue

                if not resp:
                    # Ключ и эндпоинт рабочие, матчей на день может не быть.
                    log.info(
                        "API-SPORTS connected (empty day) url=%s results=%s",
                        url,
                        results_hint,
                    )
                    return CheckResult(
                        name="API-SPORTS",
                        ok=True,
                        details=(
                            f"connected (Free plan, date=)\n"
                            f"0 fixtures on {date_vn}\n{url}"
                        ),
                    )

                item = resp[0] or {}
                if "hockey" in url:
                    league = (item.get("league") or {}).get("name") or "Hockey"
                    teams = item.get("teams") or {}
                    home = (teams.get("home") or {}).get("name") or "Home"
                    away = (teams.get("away") or {}).get("name") or "Away"
                    dt = item.get("date") or ""
                    emoji = "🏒"
                else:
                    league = (item.get("league") or {}).get("name") or "Unknown league"
                    teams = item.get("teams") or {}
                    home = (teams.get("home") or {}).get("name") or "Home"
                    away = (teams.get("away") or {}).get("name") or "Away"
                    fixture = item.get("fixture") or {}
                    dt = fixture.get("date") or ""
                    emoji = "⚽"

                log.info("API-SPORTS connected via %s", url)
                log.info("API-SPORTS sample: %s | %s vs %s | %s", league, home, away, dt)

                details = f"{emoji} {home} vs {away}\n{league}\n{dt}"
                return CheckResult(name="API-SPORTS", ok=True, details=details)

        msg = last_fail or "не удалось получить fixtures по date= (football/hockey)"
        return CheckResult(name="API-SPORTS", ok=False, details=msg)
    except Exception as e:
        msg = _safe_err(e)
        log.error("API-SPORTS error: %s", msg)
        return CheckResult(name="API-SPORTS", ok=False, details=msg)

