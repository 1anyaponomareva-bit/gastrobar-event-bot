"""Gemini API: диагностика, логирование ошибок, plain vs Google Search."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_MODEL

log = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
_TEST_PROMPT = "Ответь одним словом: ok"
_SEARCH_TEST_PROMPT = (
    "Use Google Search: what is today's date in UTC? "
    "Reply with JSON only: {\"ok\": true, \"date\": \"YYYY-MM-DD\"}"
)


@dataclass(slots=True)
class GeminiCallResult:
    ok: bool
    label: str
    preview: str | None = None
    error_class: str | None = None
    error_message: str | None = None
    response_body: str | None = None


@dataclass(slots=True)
class GeminiTestReport:
    key_loaded: bool
    model: str
    plain: GeminiCallResult
    search: GeminiCallResult

    def format_telegram(self) -> str:
        key_line = "yes" if self.key_loaded else "no"
        lines = [
            "🧪 Gemini test",
            f"GEMINI_API_KEY loaded: {key_line}",
            f"GEMINI_MODEL: {self.model}",
            "",
            self._line(self.plain, "обычный Gemini"),
            self._line(self.search, "Google Search grounding"),
        ]
        if not self.plain.ok and self.plain.error_message:
            lines.append("")
            lines.append(f"plain: {self.plain.error_class}: {self._short(self.plain.error_message)}")
        if not self.search.ok and self.search.error_message:
            lines.append(
                f"search: {self.search.error_class}: {self._short(self.search.error_message)}"
            )
        return "\n".join(lines)

    @staticmethod
    def _line(r: GeminiCallResult, title: str) -> str:
        if r.ok:
            extra = f" ({r.preview})" if r.preview else ""
            return f"✅ {title} работает{extra}"
        return f"❌ {title} — ошибка"

    @staticmethod
    def _short(msg: str, limit: int = 220) -> str:
        m = " ".join(msg.split())
        return m if len(m) <= limit else m[: limit - 3] + "..."


def effective_gemini_model() -> str:
    m = (GEMINI_MODEL or "").strip()
    return m or DEFAULT_GEMINI_MODEL


def extract_response_body(exc: BaseException) -> str | None:
    """Тело ответа API из google.genai.errors.APIError или похожих исключений."""
    parts: list[str] = []
    for attr in ("details", "response", "message", "status", "code"):
        val = getattr(exc, attr, None)
        if val is None:
            continue
        try:
            if hasattr(val, "json"):
                parts.append(json.dumps(val.json(), ensure_ascii=False)[:4000])
            elif isinstance(val, (dict, list)):
                parts.append(json.dumps(val, ensure_ascii=False)[:4000])
            else:
                s = str(val).strip()
                if s:
                    parts.append(s[:4000])
        except Exception:
            parts.append(str(val)[:4000])
    if not parts:
        return None
    return "\n---\n".join(parts)


def log_gemini_error(context: str, exc: BaseException) -> str:
    """
    Полный лог для Railway: класс, сообщение, traceback, response body.
    Возвращает краткую строку для UI.
    """
    body = extract_response_body(exc)
    summary = f"{type(exc).__name__}: {exc}"
    log.error(
        "Gemini error [%s]: class=%s message=%s",
        context,
        type(exc).__name__,
        exc,
    )
    if body:
        log.error("Gemini error [%s] response_body:\n%s", context, body)
    log.error("Gemini error [%s] traceback:\n%s", context, traceback.format_exc())
    return summary


def _generate_sync(
    *,
    contents: str,
    use_search: bool,
) -> str:
    client = genai.Client(api_key=GEMINI_API_KEY)
    model = effective_gemini_model()
    config = None
    if use_search:
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
        )
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=config,
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Пустой ответ Gemini")
    return text


def _run_call(label: str, *, use_search: bool) -> GeminiCallResult:
    prompt = _SEARCH_TEST_PROMPT if use_search else _TEST_PROMPT
    try:
        text = _generate_sync(contents=prompt, use_search=use_search)
        preview = text[:80].replace("\n", " ")
        log.info("Gemini %s ok: %s", label, preview)
        return GeminiCallResult(ok=True, label=label, preview=preview)
    except Exception as e:
        summary = log_gemini_error(label, e)
        if use_search:
            log.error("Gemini Search error", exc_info=True)
        return GeminiCallResult(
            ok=False,
            label=label,
            error_class=type(e).__name__,
            error_message=str(e),
            response_body=extract_response_body(e),
        )


def run_gemini_test_sync() -> GeminiTestReport:
    if not GEMINI_API_KEY:
        missing = GeminiCallResult(
            ok=False,
            label="plain",
            error_class="ConfigError",
            error_message="GEMINI_API_KEY is missing",
        )
        return GeminiTestReport(
            key_loaded=False,
            model=effective_gemini_model(),
            plain=missing,
            search=GeminiCallResult(
                ok=False,
                label="search",
                error_class="ConfigError",
                error_message="GEMINI_API_KEY is missing",
            ),
        )
    plain = _run_call("plain_generate_content", use_search=False)
    search = _run_call("search_grounding", use_search=True)
    return GeminiTestReport(
        key_loaded=True,
        model=effective_gemini_model(),
        plain=plain,
        search=search,
    )


async def run_gemini_test() -> GeminiTestReport:
    return await asyncio.to_thread(run_gemini_test_sync)


def generate_radar_content_sync(
    prompt: str,
    *,
    use_search: bool,
) -> str:
    """Один вызов generateContent для Event Radar."""
    return _generate_sync(contents=prompt, use_search=use_search)
