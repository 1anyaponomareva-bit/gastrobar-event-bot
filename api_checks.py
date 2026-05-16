"""Проверки внешних API: Gemini и API-SPORTS.

Важно: эти функции НЕ должны валить бота при ошибках.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from google import genai

from config import GEMINI_API_KEY, GEMINI_MODEL, SPORTS_API_KEY

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CheckResult:
    name: str
    ok: bool
    details: str | None = None


def _safe_err(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


def _gemini_sync_call() -> str:
    client = genai.Client(api_key=GEMINI_API_KEY)
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents="Ответь одним словом: ok",
    )
    return (resp.text or "").strip()


async def check_gemini() -> CheckResult:
    if not GEMINI_API_KEY:
        msg = "GEMINI_API_KEY is missing"
        log.warning("Gemini API error: %s", msg)
        return CheckResult(name="Gemini API", ok=False, details=msg)

    try:
        text = await asyncio.to_thread(_gemini_sync_call)
        if text.lower() != "ok":
            msg = f"неожиданный ответ: {text!r}"
            log.warning("Gemini API error: %s", msg)
            return CheckResult(name="Gemini API", ok=False, details=msg)
        log.info("Gemini API connected")
        return CheckResult(name="Gemini API", ok=True, details="connected")
    except Exception as e:
        msg = _safe_err(e)
        log.error("Gemini API error: %s", msg)
        return CheckResult(name="Gemini API", ok=False, details=msg)


async def check_sports_api() -> CheckResult:
    if not SPORTS_API_KEY:
        msg = "SPORTS_API_KEY is missing"
        log.warning("API-SPORTS error: %s", msg)
        return CheckResult(name="API-SPORTS", ok=False, details=msg)

    url = "https://v3.football.api-sports.io/fixtures?next=1"
    headers = {"x-apisports-key": SPORTS_API_KEY}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers=headers)
        if r.status_code != 200:
            msg = f"HTTP {r.status_code}: {r.text[:300]}"
            log.error("API-SPORTS error: %s", msg)
            return CheckResult(name="API-SPORTS", ok=False, details=msg)

        data: dict[str, Any] = r.json()
        resp = data.get("response") or []
        if not isinstance(resp, list) or not resp:
            msg = "пустой response"
            log.warning("API-SPORTS error: %s", msg)
            return CheckResult(name="API-SPORTS", ok=False, details=msg)

        item = resp[0] or {}
        league = (item.get("league") or {}).get("name") or "Unknown league"
        teams = item.get("teams") or {}
        home = (teams.get("home") or {}).get("name") or "Home"
        away = (teams.get("away") or {}).get("name") or "Away"
        fixture = item.get("fixture") or {}
        dt = fixture.get("date") or ""

        log.info("API-SPORTS connected")
        log.info("API-SPORTS sample: %s | %s vs %s | %s", league, home, away, dt)

        details = f"⚽ {home} vs {away}\n{league}\n{dt}"
        return CheckResult(name="API-SPORTS", ok=True, details=details)
    except Exception as e:
        msg = _safe_err(e)
        log.error("API-SPORTS error: %s", msg)
        return CheckResult(name="API-SPORTS", ok=False, details=msg)

