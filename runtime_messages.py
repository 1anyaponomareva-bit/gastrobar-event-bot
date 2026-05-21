"""Тексты ошибок для Telegram с учётом RUN_MODE (local / railway)."""



from __future__ import annotations



import logging

from typing import Any



from config import GEMINI_API_KEY, RUN_MODE, is_local_run, is_railway_run

from error_handling import format_telegram_exception, log_unexpected_resolve



log = logging.getLogger(__name__)



# Меняйте при деплое — по этой метке видно, какой код ответил в Telegram.

BOT_BUILD_ID = "event-radar-menu-3d-not-week-20260520b"



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

    "api_ok_empty": "в окне 72 ч нет подходящих событий (API ответил)",

    "api_window_empty": "в окне нет событий после фильтра времени",

    "unexpected_error": "unexpected error",

}



# fetch_note из pipeline → код для event_radar_error_message

_FETCH_NOTE_TO_CODE: dict[str, str] = {

    "api_ok_empty": "api_ok_empty",

    "api_window_empty": "api_window_empty",

    "api_window_only": "api_window_empty",

    "api_filter_empty": "api_filter_empty",

    "api_unified": "api_filter_empty",

    "api_gemini_unified": "api_filter_empty",

    "api_no_events": "no_candidates",

    "weekly_cache_stale": "no_candidates",
    "betboom_ok": "api_ok_empty",
    "betboom_cache": "api_ok_empty",
    "betboom_unavailable": "betboom_unavailable",
    "betboom_parse_error": "betboom_parse_error",
    "api_sports_fallback": "api_filter_empty",

    "now24_emergency": "api_filter_empty",

}





def runtime_logs_hint() -> str:

    if is_railway_run():

        return "Смотрите Railway logs (строки NOW24_EVENT_RAW, NOW24_DROP)."

    if is_local_run():

        return "Смотрите локальный терминал (строки NOW24_EVENT_RAW, NOW24_DROP)."

    return "Смотрите логи запуска (NOW24_EVENT_RAW, NOW24_DROP)."





def troubleshoot_footer() -> str:

    return f"{GEMINI_TROUBLESHOOT}\n{runtime_logs_hint()}"





def build_tag_line() -> str:

    return f"🛠 build: {BOT_BUILD_ID} · RUN_MODE={RUN_MODE}"





def event_radar_error_message(

    reason: str,

    *,

    exc: BaseException | None = None,

    fetch_note: str | None = None,

) -> str:

    label = _RADAR_REASON_RU.get(reason, _RADAR_REASON_RU["unexpected_error"])

    if reason == "gemini_overloaded":

        body = (

            "⏳ Gemini временно перегружен (503). Ключ в порядке — подождите 1–2 минуты "

            "и нажмите «Обновить 3 дня».\n\n"

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

    elif reason == "api_ok_empty":

        body = (

            "BetBoom ответил, но в ближайшие 3 дня нет подходящих событий для афиши.\n\n"

            f"{runtime_logs_hint()}"

        )

    elif reason == "api_window_empty":

        body = (

            "События есть в API, но ни одно не попало в окно времени (72 ч / 24 ч).\n\n"

            f"{runtime_logs_hint()}"

        )

    else:

        body = troubleshoot_footer()



    msg = f"❌ Event Radar: {label}\n\n{body}\n\n{build_tag_line()}"

    if fetch_note and reason == "unexpected_error":

        msg += f"\n\nfetch_note={fetch_note}"

    if exc is not None:

        msg += f"\n\n{format_telegram_exception(exc)}"

    elif reason == "unexpected_error":

        msg += "\n\n(исключение не передано — см. логи resolve_radar_error_code)"

    return msg





def format_now24_empty_message(

    *,

    pool_count: int = 0,

    window_count: int = 0,

    fetch_note: str | None = None,

    debug: Any | None = None,

) -> str:

    """«Пусто» только если BetBoom ok и в окне нет матчей; иначе — источник недоступен."""

    from api_sports_status import (
        format_api_failure_user_message,
        is_api_failure_note,
        is_truly_empty_radar_note,
    )
    from betboom_parser import format_betboom_unavailable_message, is_betboom_failure_note

    if is_betboom_failure_note(fetch_note):
        return format_betboom_unavailable_message(fetch_note) + f"\n\n{build_tag_line()}"

    if is_api_failure_note(fetch_note):

        return format_api_failure_user_message(fetch_note) + f"\n\n{build_tag_line()}"



    if fetch_note and not is_truly_empty_radar_note(fetch_note):

        return format_api_failure_user_message(fetch_note) + f"\n\n{build_tag_line()}"



    lines = [

        "⚡ Ближайшие 24 часа — сейчас пусто.",

        "",

        "Показываются только события, которые **начнутся в следующие 24 часа** "

        "(время Нячанга). Уже **идущие** или **прошедшие** матчи сюда не попадают.",

    ]

    if debug is not None:

        inside = getattr(debug, "inside_window", None)

        if inside is None:

            inside = getattr(debug, "after_window", "?")

        lines.extend(["", "NOW24 DEBUG:"])

        lines.append(f"now_local: {getattr(debug, 'now_local', '?')}")

        lines.append(f"end_local: {getattr(debug, 'window_end', '?')}")

        lines.append(f"raw_events: {getattr(debug, 'all_events', '?')}")

        lines.append(f"parsed_ok: {getattr(debug, 'parsed_ok', '?')}")

        lines.append(f"inside_window: {inside}")

        lines.append(f"outside_window: {getattr(debug, 'outside_window', '?')}")

        lines.append(f"bad_datetime: {getattr(debug, 'bad_datetime', '?')}")

        lines.append(f"already_started: {getattr(debug, 'already_started', 0)}")

        lines.append(f"after_final: {getattr(debug, 'after_final', '?')}")

        drops = getattr(debug, "drops", None) or []

        lines.append("")

        lines.append("Первые 10 dropped:")

        for d in drops[:10]:

            if isinstance(d, dict):

                loc = d.get("local_datetime") or d.get("event_local", "?")

                lines.append(

                    f"{d.get('title', '?')} | {loc} | "

                    f"Δ{d.get('delta_hours', '?')}ч | {d.get('reason', '?')}"

                )

        lines.append("")

        lines.append(runtime_logs_hint())



    if pool_count > 0:

        lines.extend(

            [

                "",

                "⚠️ В API есть матчи, но в окне 24 ч ничего не прошло — "

                "это debug-ситуация (см. NOW24 DEBUG и логи).",

            ]

        )

    elif window_count > 0 and fetch_note in (

        "api_filter_empty",

        "api_window_only",

    ):

        lines.extend(

            [

                "",

                f"В окне **24 ч** найдено **{window_count}** событий (хоккей, киберспорт, "

                "теннис и др.), но они отфильтрованы перед показом. "

                "Смотрите логи: `NOW24 EMPTY DIAG`, `AFTER_NOW24_WINDOW`, `FINAL_NOW24`.",

            ]

        )

    elif pool_count == 0 and fetch_note in ("api_window_empty", "api_ok_empty", None):

        lines.extend(

            [

                "",

                "Сейчас в API нет матчей в окне 24 ч (today+tomorrow). "

                "Проверьте /api_status и /check.",

            ]

        )

    lines.extend(

        [

            "",

            "🔥 **Афиша на 3 дня** — попробуйте из меню /events.",

        ]

    )

    return "\n".join(lines)





def gemini_test_error_message(exc: BaseException | None = None) -> str:

    base = f"Ошибка при /gemini_test.\n\n{troubleshoot_footer()}"

    if exc is not None:

        return f"{base}\n\n{format_telegram_exception(exc)}"

    return base





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

    if fetch_note in _FETCH_NOTE_TO_CODE:

        return _FETCH_NOTE_TO_CODE[fetch_note]

    if exception is not None:

        from gemini_client import is_gemini_transient_error



        if is_gemini_transient_error(exception):

            return "gemini_overloaded"

        msg = str(exception).lower()

        if "gemini_api_key" in msg or "api key" in msg and "gemini" in msg:

            return "gemini_api_key_missing"

        log.exception(

            "resolve_radar_error_code: unmapped exception fetch_note=%r",

            fetch_note,

            exc_info=exception,

        )

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

    log_unexpected_resolve(

        fetch_note=fetch_note,

        raw_total=raw_total,

        prelim_count=prelim_count,

        selected=selected,

    )

    return "unexpected_error"


