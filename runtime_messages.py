"""Тексты ошибок для Telegram с учётом RUN_MODE (local / railway)."""

from __future__ import annotations

from config import GEMINI_API_KEY, RUN_MODE, is_local_run, is_railway_run

# Меняйте при деплое — по этой метке видно, какой код ответил в Telegram.
BOT_BUILD_ID = "railway-runmode-fix-20260520"

GEMINI_TROUBLESHOOT = (
    "Проверьте GEMINI_API_KEY через /check и посмотрите логи в терминале, "
    "где запущен бот."
)

_RADAR_REASON_RU: dict[str, str] = {
    "gemini_api_key_missing": "Gemini API key missing",
    "gemini_search_failed": "Gemini search failed",
    "gemini_overloaded": "Gemini overloaded (503) — retry in 1–2 min",
    "timeout": "timeout",
    "no_candidates": "no candidates",
    "verification_failed": "verification failed",
    "api_filter_empty": "API events dropped by filters (tier/score)",
    "unexpected_error": "unexpected error",
}


def runtime_logs_hint() -> str:
    if is_local_run():
        return "Проверьте терминал локального запуска."
    if is_railway_run():
        return "Проверьте логи Railway."
    return "Проверьте терминал локального запуска."


def troubleshoot_footer() -> str:
    return f"{GEMINI_TROUBLESHOOT}\n{runtime_logs_hint()}"


def build_tag_line() -> str:
    return f"🛠 build: {BOT_BUILD_ID} · RUN_MODE={RUN_MODE}"


def event_radar_error_message(reason: str) -> str:
    label = _RADAR_REASON_RU.get(reason, _RADAR_REASON_RU["unexpected_error"])
    if reason == "gemini_overloaded":
        body = (
            "⏳ Gemini временно перегружен (503). Ключ в порядке — подождите 1–2 минуты "
            "и нажмите «Обновить неделю».\n\n"
            f"{runtime_logs_hint()}"
        )
    elif reason == "api_filter_empty":
        body = (
            "API-SPORTS вернул события, но все отфильтрованы (tier/score/watchability). "
            "Проверьте RADAR_MIN_WATCHABILITY и логи Railway.\n\n"
            f"{runtime_logs_hint()}"
        )
    else:
        body = troubleshoot_footer()
    return f"❌ Event Radar: {label}\n\n{body}\n\n{build_tag_line()}"


def gemini_test_error_message() -> str:
    return f"Ошибка при /gemini_test.\n\n{troubleshoot_footer()}"


def resolve_radar_error_code(
    *,
    fetch_note: str | None = None,
    raw_total: int = 0,
    prelim_count: int = 0,
    selected: int = 0,
    timed_out: bool = False,
    exception: BaseException | None = None,
) -> str:
    if timed_out:
        return "timeout"
    if fetch_note in _RADAR_REASON_RU:
        return fetch_note
    if exception is not None:
        from gemini_client import is_gemini_transient_error

        if is_gemini_transient_error(exception):
            return "gemini_overloaded"
        msg = str(exception).lower()
        if "gemini_api_key" in msg or "api key" in msg and "gemini" in msg:
            return "gemini_api_key_missing"
        return "unexpected_error"
    if fetch_note == "gemini_api_key_missing" or (
        not GEMINI_API_KEY and fetch_note not in ("sports_fallback",)
    ):
        return "gemini_api_key_missing"
    if fetch_note == "gemini_overloaded":
        return "gemini_overloaded"
    if raw_total == 0 or prelim_count == 0:
        if fetch_note in ("gemini_error", "search_fallback"):
            return "gemini_search_failed"
        return "no_candidates"
    if selected == 0 and prelim_count > 0:
        return "verification_failed"
    if fetch_note in ("gemini_error", "search_fallback"):
        return "gemini_search_failed"
    return "unexpected_error"
