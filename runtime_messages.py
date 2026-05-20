"""Тексты ошибок для Telegram с учётом RUN_MODE (local / railway)."""

from __future__ import annotations

from config import GEMINI_API_KEY, RUN_MODE, is_local_run, is_railway_run

# Меняйте при деплое — по этой метке видно, какой код ответил в Telegram.
BOT_BUILD_ID = "rpl-weekly-pool-20260521"

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
    "verification_failed": "ни одно событие не прошло проверку (время / API / неделя)",
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
    elif reason == "verification_failed":
        body = (
            "Поиск (Gemini) отдал события, но **ни одно не прошло финальную проверку** — это "
            "часто не про ключ.\n\n"
            "Типично для **афиши недели**: футбол / MMA / еврокубки требуют совпадения с "
            "**API-SPORTS** (ключ `SPORTS_API_KEY` и матч по дате/командам). Если матч не "
            "нашёлся, карточка отбрасывается.\n\n"
            "Ещё причины: дата **вне окна недели** (VN), жёсткий фильтр по времени бара.\n\n"
            "**Free tier Gemini:** если в проекте включены несколько шардов поиска (`RADAR_MULTI_SHARD`), "
            "может сработать лимит RPM — подождите 1–2 мин и нажмите «Обновить».\n\n"
            f"{runtime_logs_hint()}\n"
            "Там ищите строки `rejected_*`, `verify_removed`, `Event Radar verify summary`."
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


def format_now24_empty_message(
    *,
    pool_count: int = 0,
    fetch_note: str | None = None,
) -> str:
    """Понятное «пусто» для режима 24 ч (не путать с поломкой Gemini)."""
    lines = [
        "⚡ Ближайшие 24 часа — сейчас пусто.",
        "",
        "Показываются только события, которые **начнутся в следующие 24 часа** "
        "(время Нячанга). Уже **идущие** или **прошедшие** матчи сюда не попадают.",
    ]
    if pool_count > 0 or fetch_note == "api_filter_empty":
        lines.extend(
            [
                "",
                f"Из API было **{pool_count}** матчей, но после фильтров или окна 24 ч "
                "ничего не осталось. В Railway в логах ищите `NOW24_FILTER` и `drop_window`.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Сейчас в API/кэше нет матчей в этом окне. Проверьте **/check** (API-SPORTS) "
                "или попробуйте позже.",
            ]
        )
    lines.extend(
        [
            "",
            "📅 **Афиша на неделю** — полный список на 7 дней (часто там есть то, "
            "что уже прошло по «24 ч»).",
        ]
    )
    return "\n".join(lines)


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
