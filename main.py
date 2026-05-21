"""Минимальный тестовый запуск aiogram 3 (диагностика)."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import aiogram
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramConflictError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    MenuButtonCommands,
    Message,
)

from ai_generator import generate_weekly_poster
from daily_event_posts import (
    CACHE_EMPTY_MSG,
    build_daily_content_package,
    deliver_daily_content,
    user_error_message,
)
from api_checks import check_gemini, check_sports_api
import time

from bot_typing import long_operation_typing, show_typing
from fatal_conflict_dispatcher import FatalConflictDispatcher
from config import (
    ADMIN_ID,
    EXPECTED_BOT_USERNAME,
    GASTROBAR_GROUP_ID,
    GEMINI_API_KEY,
    TELEGRAM_BOT_TOKEN,
    is_local_run,
)
from gemini_client import run_gemini_test
from database import (
    get_draft,
    get_draft_asset,
    init_db,
    insert_draft,
    save_radar_snapshot,
    update_draft_status,
    update_draft_text,
    upsert_draft_asset,
)
from keyboards import (
    post_result_kb,
    radar_menu_kb,
    radar_now24_result_kb,
    radar_week_result_kb,
)
from scheduler import shutdown_scheduler
from event_radar import (
    format_radar_now24_message,
    format_radar_week_message,
    get_event_radar_now24,
    get_event_radar_week,
    radar_fetch_header,
)

from error_handling import configure_logging, format_telegram_exception

configure_logging()
log = logging.getLogger(__name__)
logger = log


def _radar_log_context(mode: str) -> str:
    return "NOW24 unexpected error" if mode == "now24" else "WEEK unexpected error"

router = Router()
# Event Radar (Gemini + Google Search) может занять дольше, чем спортивный API.
WEEK_FETCH_TIMEOUT_SEC = 240.0
last_draft_state: dict[int, dict[str, Any]] = {}
last_week_events: dict[int, list[dict[str, Any]]] = {}
last_now24_events: dict[int, list[dict[str, Any]]] = {}
current_message_context: dict[int, dict[str, Any]] = {}
daily_post_state: dict[int, dict[str, Any]] = {}


def _ru_found_events_line(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        w = "событие"
    elif 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        w = "события"
    else:
        w = "событий"
    return f"Найдено {n} {w}."


def _ru_selected_main_line(n: int, *, mode: str = "next72") -> str:
    if mode == "now24":
        if n % 10 == 1 and n % 100 != 11:
            return f"Выбрано {n} событие на ближайшие 24 часа."
        if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
            return f"Выбрано {n} события на ближайшие 24 часа."
        return f"Выбрано {n} событий на ближайшие 24 часа."
    if n % 10 == 1 and n % 100 != 11:
        return f"Выбрано {n} событие на 3 дня."
    if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return f"Выбрано {n} события на 3 дня."
    return f"Выбрано {n} событий на 3 дня."


def _fail_conflict(context: str) -> None:
    hint = (
        "Остановите другой экземпляр main.py (bot.lock) или второй deployment."
        if is_local_run()
        else "Проверьте TELEGRAM_BOT_TOKEN и единственный deployment."
    )
    logger.error(
        "%s: TelegramConflictError — токен уже занят другим polling/webhook. %s "
        "Ожидается @%s.",
        context,
        hint,
        EXPECTED_BOT_USERNAME or "?",
    )
    print(
        "TelegramConflictError: only one polling client allowed. " + hint,
        file=sys.stderr,
    )
    raise SystemExit(1)


async def _probe_unique_get_updates_client(bot: Bot) -> None:
    """Проверка токена без getUpdates (не съедаем очередь сообщений)."""
    me = await bot.get_me()
    logger.info("Bot API ok before polling: @%s id=%s", me.username, me.id)


async def _clear_webhook_and_log(bot: Bot) -> None:
    try:
        info = await bot.get_webhook_info()
        logger.info(
            "Webhook before delete: url=%r pending_update_count=%s",
            info.url or "",
            info.pending_update_count,
        )
    except Exception:
        logger.exception("getWebhookInfo before delete failed")

    try:
        ok = await bot.delete_webhook(drop_pending_updates=False)
        logger.info("delete_webhook(drop_pending_updates=False): result=%s", ok)
    except Exception:
        logger.exception("delete_webhook failed")
        raise

    try:
        info2 = await bot.get_webhook_info()
        logger.info(
            "Webhook after delete: url=%r pending_update_count=%s",
            info2.url or "",
            info2.pending_update_count,
        )
    except Exception:
        logger.exception("getWebhookInfo after delete failed")


def _assert_expected_bot_username(username: str | None) -> None:
    if not EXPECTED_BOT_USERNAME:
        return
    got = (username or "").lower()
    want = EXPECTED_BOT_USERNAME.lower()
    if got == want:
        logger.info("Bot username matches EXPECTED_BOT_USERNAME=@%s", want)
        return
    fix = (
        "Проверьте TELEGRAM_BOT_TOKEN в .env (токен Gastrobar из BotFather)."
        if is_local_run()
        else "Замените TELEGRAM_BOT_TOKEN в Variables на токен Gastrobar."
    )
    logger.error(
        "Wrong bot token: running @%s but EXPECTED_BOT_USERNAME=@%s. %s",
        username,
        want,
        fix,
    )
    print(
        f"Wrong TELEGRAM_BOT_TOKEN: @{username} != @{want}. {fix}",
        file=sys.stderr,
    )
    raise SystemExit(1)


async def set_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(
            command="events",
            description="Event Radar — меню",
        ),
        BotCommand(command="check", description="Проверить API подключения"),
        BotCommand(command="gemini_test", description="Тест Gemini и Google Search"),
        BotCommand(command="daily", description="Пост дня (одно событие)"),
        BotCommand(command="clear_cache", description="Очистить weekly cache (admin)"),
        BotCommand(command="debug_cache", description="Статус weekly cache (admin)"),
        BotCommand(command="radar_debug", description="Event Radar: счётчики и отбраковка"),
        BotCommand(command="api_status", description="API-SPORTS: статус по видам спорта"),
    ]
    scopes = (
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
    )
    for scope in scopes:
        try:
            await bot.delete_my_commands(scope=scope)
        except Exception:
            logger.warning("delete_my_commands failed for scope %s", type(scope).__name__)
    for scope in scopes:
        await bot.set_my_commands(commands, scope=scope)
    # Кнопка меню слева от поля ввода — список команд (а не «веб» по умолчанию).
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    logger.info(
        "Bot commands registered: %s",
        [c.command for c in commands],
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    log.info("Received /start from %s", user_id)
    bot = message.bot
    try:
        await set_bot_commands(bot)
        if message.chat.type == "private":
            await bot.set_chat_menu_button(
                chat_id=message.chat.id,
                menu_button=MenuButtonCommands(),
            )
    except Exception:
        logger.exception("Не удалось обновить команды меню при /start")
    from runtime_messages import build_tag_line

    build_line = f"\n\n{build_tag_line()}" if is_local_run() else ""
    await message.answer(
        f"Бот живой. Твой ID: {user_id}\n\n"
        "Команды меню обновлены. Открой кнопку меню слева от поля ввода — там "
        "/start, /events, /check и /gemini_test.\n\n"
        "Event Radar: /events — ⚡ 24 часа или 🔥 афиша на 3 дня (BetBoom).\n"
        "Команду /week можно ввести вручную — то же, что афиша на 3 дня.\n\n"
        "Если списка нет: полностью закрой чат с ботом и открой снова, либо обнови Telegram."
        f"{build_line}"
    )


def _events_menu_text() -> str:
    from runtime_messages import build_tag_line

    body = (
        "Что собрать?\n\n"
        "🔥 Афиша на 3 дня\n"
        "— главные события ближайших 3 дней (BetBoom)\n\n"
        "⚡ Ближайшие 24 часа\n"
        "— что начнётся в ближайшие 24 ч + пост на сегодня"
    )
    return f"{body}\n\n{build_tag_line()}"


def _normalize_radar_mode(mode: str) -> str:
    if mode in ("week", "next72"):
        return "next72"
    return mode


async def _deliver_weekly_cache(
    message: Message,
    user_id: int,
    cached: list[dict[str, Any]],
    *,
    header: str,
) -> None:
    last_week_events[user_id] = cached
    current_message_context[user_id] = {"mode": "next72", "events": list(cached)}
    stats = _ru_selected_main_line(len(cached))
    from event_radar import format_radar_next72_message

    body = format_radar_next72_message(cached)
    await message.answer(
        f"{header}\n\n{stats}\n\n{body}",
        reply_markup=radar_week_result_kb(),
    )


async def _answer_radar_empty(
    message: Message,
    *,
    user_id: int,
    raw_total: int,
    pre_count: int,
    fetch_note: str | None,
) -> None:
    from api_sports_status import format_api_failure_user_message, is_api_failure_note
    from runtime_messages import (
        build_tag_line,
        event_radar_error_message,
        resolve_radar_error_code,
    )
    from weekly_events_cache import get_weekly_events_cache_for_display

    if fetch_note in ("betboom_unavailable", "betboom_parse_error"):
        from betboom_parser import format_betboom_unavailable_message

        cached = await get_weekly_events_cache_for_display()
        if cached:
            await _deliver_weekly_cache(
                message,
                user_id,
                cached,
                header=(
                    f"📦 Афиша на 3 дня из кэша ({len(cached)} событий).\n"
                    f"{format_betboom_unavailable_message(fetch_note)}"
                ),
            )
            return
        await message.answer(
            format_betboom_unavailable_message(fetch_note) + f"\n\n{build_tag_line()}"
        )
        return

    if is_api_failure_note(fetch_note):
        cached = await get_weekly_events_cache_for_display()
        if cached:
            await _deliver_weekly_cache(
                message,
                user_id,
                cached,
                header=format_api_failure_user_message(fetch_note),
            )
            return
        await message.answer(
            format_api_failure_user_message(fetch_note) + f"\n\n{build_tag_line()}"
        )
        return

    if fetch_note in (
        "gemini_quota",
        "gemini_error",
        "gemini_overloaded",
        "search_fallback",
        "verification_failed",
        "weekly_cache_quota",
    ):
        cached = await get_weekly_events_cache_for_display()
        if cached:
            quota_hint = ""
            if fetch_note == "gemini_quota":
                quota_hint = (
                    "\n⚠️ Лимит Gemini (≈20 запросов/день на free tier) — "
                    "времена могут быть старыми. Завтра нажмите «Обновить 3 дня»."
                )
            if fetch_note == "weekly_cache_quota":
                hdr = (
                    f"📦 Афиша из кэша ({len(cached)} событий).\n"
                    "⚠️ Gemini лимит исчерпан. Показываю последнюю сохранённую афишу."
                )
            elif fetch_note == "gemini_overloaded":
                hdr = (
                    f"📦 Афиша из кэша ({len(cached)} событий).\n"
                    "Gemini сейчас перегружен (503) — свежий поиск не прошёл."
                )
            else:
                hdr = (
                    f"📦 Афиша из кэша ({len(cached)} событий).\n"
                    "Свежий поиск не удался — показана сохранённая подборка."
                ) + quota_hint
            await _deliver_weekly_cache(message, user_id, cached, header=hdr)
            return

    if fetch_note == "gemini_quota":
        await message.answer(
            "Лимит Gemini исчерпан (429). Старый кэш с неверными временами скрыт.\n"
            "Подождите до завтра или подключите биллинг:\n"
            "https://ai.google.dev/gemini-api/docs/rate-limits"
        )
        return

    code = resolve_radar_error_code(
        fetch_note=fetch_note,
        raw_total=raw_total,
        prelim_count=pre_count,
        selected=0,
    )
    if code == "unexpected_error":
        logger.exception(
            "WEEK empty handler -> unexpected_error fetch_note=%r raw=%s pre=%s",
            fetch_note,
            raw_total,
            pre_count,
            exc_info=True,
        )
    await message.answer(
        event_radar_error_message(code, fetch_note=fetch_note)
    )


async def _run_radar_mode(
    target: Message | CallbackQuery,
    mode: str,
    *,
    force_refresh: bool = False,
) -> None:
    if isinstance(target, CallbackQuery):
        message = target.message
        bot = target.bot
        user_id = target.from_user.id
    else:
        message = target
        bot = message.bot
        user_id = message.from_user.id if message.from_user else 0

    if not message:
        return

    mode = _normalize_radar_mode(mode)
    chat_id = message.chat.id
    label = "афиша на 3 дня" if mode == "next72" else "ближайшие 24 часа"
    search_hint = (
        "📦 Загружаю сохранённую афишу…"
        if mode == "next72" and not force_refresh
        else f"🔍 Ищу: {label}\n✍️ (BetBoom линия · VN время)"
    )

    events: list[dict[str, Any]] = []
    raw_total = pre_count = selected = 0
    fetch_note: str | None = None

    try:
        async with long_operation_typing(
            bot,
            chat_id,
            initial_text=search_hint,
            progress_label=label,
        ) as status_msg:
            t0 = time.monotonic()
            logger.info("radar:%s fetch started user_id=%s", mode, user_id)
            try:
                if mode == "next72":
                    coro = get_event_radar_week(force_refresh=force_refresh)
                else:
                    coro = get_event_radar_now24()
                events, raw_total, pre_count, selected, fetch_note = await asyncio.wait_for(
                    coro,
                    timeout=WEEK_FETCH_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                from runtime_messages import event_radar_error_message

                logger.warning(
                    "radar:%s timeout after %.0fs user_id=%s",
                    mode,
                    time.monotonic() - t0,
                    user_id,
                )
                await status_msg.edit_text(
                    f"⏱ Поиск занял больше {int(WEEK_FETCH_TIMEOUT_SEC // 60)} мин.\n\n"
                    + event_radar_error_message("timeout")
                )
                return
            except Exception as exc:
                from runtime_messages import (
                    event_radar_error_message,
                    resolve_radar_error_code,
                )
                from weekly_events_cache import get_weekly_events_cache_for_display

                logger.exception(
                    "%s (inner fetch, %.1fs)",
                    _radar_log_context(mode),
                    time.monotonic() - t0,
                    exc_info=True,
                )
                if mode == "next72":
                    cached = await get_weekly_events_cache_for_display()
                    if cached:
                        try:
                            await status_msg.delete()
                        except Exception:
                            logger.exception("status_msg.delete failed", exc_info=True)
                        await _deliver_weekly_cache(
                            message,
                            user_id,
                            cached,
                            header=(
                                f"📦 Афиша из сохранённого кэша ({len(cached)} событий).\n"
                                "Свежий поиск не удался — см. терминал.\n"
                                "⚠️ Если времена EPL ~22:00 — кэш устарел, дождитесь сброса лимита Gemini."
                            ),
                        )
                        return
                code = resolve_radar_error_code(exception=exc)
                await status_msg.edit_text(
                    event_radar_error_message(code, exc=exc)
                )
                return
            logger.info(
                "radar:%s fetch done in %.1fs selected=%s raw=%s",
                mode,
                time.monotonic() - t0,
                selected,
                raw_total,
            )
            try:
                await status_msg.delete()
            except Exception:
                logger.exception("status_msg.delete failed", exc_info=True)

        if mode == "next72" and events:
            from weekly_football_times import enrich_weekly_football_times

            events = await enrich_weekly_football_times(events)

        if mode == "next72":
            last_week_events[user_id] = events if selected else []
        else:
            last_now24_events[user_id] = events if selected else []
        current_message_context[user_id] = {
            "mode": mode,
            "events": list(events) if selected else [],
        }

        if selected == 0:
            if mode == "now24":
                from event_radar_pipeline import get_last_now24_debug
                from runtime_messages import format_now24_empty_message

                await message.answer(
                    format_now24_empty_message(
                        pool_count=raw_total,
                        window_count=pre_count,
                        fetch_note=fetch_note,
                        debug=get_last_now24_debug(),
                    ),
                    reply_markup=radar_now24_result_kb(),
                )
            else:
                await _answer_radar_empty(
                    message,
                    user_id=user_id,
                    raw_total=raw_total,
                    pre_count=pre_count,
                    fetch_note=fetch_note,
                )
            return

        await save_radar_snapshot(
            mode, events, {"raw_total": raw_total, "selected": selected}
        )

        extra = radar_fetch_header(fetch_note, events if mode == "now24" else None)
        found_n = pre_count if mode == "next72" and pre_count else raw_total
        stats = (
            f"{_ru_found_events_line(found_n)}\n"
            f"{_ru_selected_main_line(selected, mode=mode)}"
        )
        from event_radar import format_radar_next72_message

        body = (
            format_radar_next72_message(events)
            if mode == "next72"
            else format_radar_now24_message(events)
        )
        text = f"{extra}\n{stats}\n\n{body}" if extra else f"{stats}\n\n{body}"
        kb = radar_week_result_kb() if mode == "next72" else radar_now24_result_kb()
        async with show_typing(bot, chat_id):
            await message.answer(text, reply_markup=kb)
    except Exception as exc:
        logger.exception(
            "%s (outer handler user_id=%s)",
            _radar_log_context(mode),
            user_id,
            exc_info=True,
        )
        await message.answer(format_telegram_exception(exc))


@router.message(Command("events", "week"))
async def cmd_events_or_week(message: Message) -> None:
    logger.info(
        "cmd /events: user=%s chat=%s type=%s",
        message.from_user.id if message.from_user else 0,
        message.chat.id,
        message.chat.type,
    )
    try:
        await message.answer(_events_menu_text(), reply_markup=radar_menu_kb())
    except Exception:
        logger.exception("cmd /events answer failed")
        raise


@router.callback_query(F.data == "radar:next72")
async def radar_next72(callback: CallbackQuery) -> None:
    await callback.answer()
    await _run_radar_mode(callback, "next72", force_refresh=False)


@router.callback_query(F.data == "radar:next72:force")
async def radar_next72_force(callback: CallbackQuery) -> None:
    await callback.answer("Обновление афиши на 3 дня…")
    await _run_radar_mode(callback, "next72", force_refresh=True)


@router.callback_query(F.data == "radar:week")
async def radar_week(callback: CallbackQuery) -> None:
    await callback.answer()
    await _run_radar_mode(callback, "next72", force_refresh=False)


@router.callback_query(F.data == "radar:week:force")
async def radar_week_force(callback: CallbackQuery) -> None:
    await callback.answer("Обновление афиши на 3 дня…")
    await _run_radar_mode(callback, "next72", force_refresh=True)


@router.callback_query(F.data == "radar:now24")
async def radar_now24(callback: CallbackQuery) -> None:
    await callback.answer()
    logger.info("radar:now24 clicked")
    await _run_radar_mode(callback, "now24")


@router.callback_query(F.data == "radar:close")
async def radar_close(callback: CallbackQuery) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    last_week_events.pop(user_id, None)
    last_now24_events.pop(user_id, None)
    current_message_context.pop(user_id, None)
    if callback.message:
        await callback.message.answer("Event Radar закрыт.")


def _events_from_context(user_id: int, mode: str) -> list[dict[str, Any]]:
    mode = _normalize_radar_mode(mode)
    ctx = current_message_context.get(user_id) or {}
    if ctx.get("mode") == mode and ctx.get("events"):
        return list(ctx["events"])
    if mode == "now24":
        return list(last_now24_events.get(user_id) or [])
    return list(last_week_events.get(user_id) or [])


@router.callback_query(
    F.data.in_({"radar:post_next72", "radar:post_week", "radar:week:gen"})
)
async def radar_post_next72(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        user_id = callback.from_user.id
        logger.info("POST GENERATION: mode=next72 no-search")
        events = _events_from_context(user_id, "next72") or _events_from_context(
            user_id, "week"
        )
        logger.info("POST_NEXT72_FROM_STATE: %s", [e.get("title") for e in events])
        if not events:
            await callback.message.answer(
                "Нет сохранённой афиши на 3 дня. Сначала нажмите 🔥 Афиша на 3 дня."
            )
            return
        async with show_typing(callback.bot, callback.message.chat.id):
            await callback.message.answer("Пишу пост по афише на 3 дня…")
            post_text = await generate_weekly_poster(events)
        draft_id = await insert_draft("week_post", post_text, "draft")
        last_draft_state[user_id] = {"draft_id": draft_id, "events": events}
        await callback.message.answer(post_text, reply_markup=post_result_kb(draft_id))
    except Exception as exc:
        logger.exception("Callback failed: radar_post_next72", exc_info=True)
        await callback.message.answer(format_telegram_exception(exc))


@router.callback_query(F.data == "radar:post_now24")
async def radar_post_now24(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        user_id = callback.from_user.id
        chat_id = callback.message.chat.id
        logger.info("POST GENERATION: mode=now24 no-search")
        events = _events_from_context(user_id, "now24")
        logger.info("POST_NOW24_FROM_STATE: %s", [e.get("title") for e in events])
        if not events:
            await callback.message.answer(
                "Нет сохранённых событий на 24 часа. "
                "Сначала нажмите ⚡ Ближайшие 24 часа в /events."
            )
            return
        from daily_event_posts import build_post_from_saved_events

        async with long_operation_typing(
            callback.bot,
            chat_id,
            initial_text="📋 Оформляю пост по показанным событиям…\n✍️ Бот печатает…",
            progress_label="пост now24",
        ):
            result = await asyncio.wait_for(
                build_post_from_saved_events(
                    events,
                    mode="now24",
                    log_prefix="radar_post_now24",
                ),
                timeout=WEEK_FETCH_TIMEOUT_SEC,
            )
        if not result.ok or not result.package:
            await callback.message.answer(
                result.error_detail or "Не удалось оформить пост."
            )
            return
        pkg = result.package
        daily_post_state[user_id] = {
            "draft_id": pkg.draft_id,
            "event": pkg.events[0] if pkg.events else {},
            "events": pkg.events,
            "image_path": pkg.image_path,
            "poster_source": pkg.image_source,
            "mode": "now24",
        }
        await deliver_daily_content(callback.bot, pkg, chat_id=chat_id)
    except asyncio.TimeoutError:
        logger.warning("Callback radar_post_now24 timeout")
        await callback.message.answer("Таймаут. Повторите позже.")
    except Exception as exc:
        logger.exception("Callback failed: radar_post_now24", exc_info=True)
        await callback.message.answer(format_telegram_exception(exc))


@router.message(Command("daily"))
async def cmd_daily(message: Message) -> None:
    logger.info("daily started (command /daily)")
    user_id = message.from_user.id if message.from_user else 0
    try:
        async with long_operation_typing(
            message.bot,
            message.chat.id,
            initial_text="📋 Готовлю пост дня…\n✍️ Бот печатает…",
            progress_label="пост дня",
        ):
            result = await asyncio.wait_for(
                build_daily_content_package(log_prefix="cmd_daily"),
                timeout=WEEK_FETCH_TIMEOUT_SEC,
            )
        if result.error_code == "cache_empty":
            await message.answer(CACHE_EMPTY_MSG)
            async with long_operation_typing(
                message.bot,
                message.chat.id,
                initial_text="🔍 Быстрый поиск ближайших 24 часов…",
                progress_label="поиск 24ч",
            ):
                result = await asyncio.wait_for(
                    build_daily_content_package(
                        log_prefix="cmd_daily",
                        force_fresh_fallback=True,
                    ),
                    timeout=WEEK_FETCH_TIMEOUT_SEC,
                )
        if not result.ok or not result.package:
            logger.warning(
                "cmd /daily failed: code=%s detail=%s",
                result.error_code,
                result.error_detail,
            )
            await message.answer(user_error_message(result))
            return
        pkg = result.package
        daily_post_state[user_id] = {
            "draft_id": pkg.draft_id,
            "events": pkg.events,
            "event": pkg.events[0] if pkg.events else {},
            "image_path": pkg.image_path,
            "poster_source": pkg.image_source,
        }
        await deliver_daily_content(message.bot, pkg, chat_id=message.chat.id)
    except asyncio.TimeoutError:
        logger.warning("cmd /daily timeout")
        await message.answer("Таймаут. Повторите позже.")
    except Exception as exc:
        logger.exception("cmd /daily unexpected error", exc_info=True)
        await message.answer(format_telegram_exception(exc))


@router.callback_query(F.data.startswith("daily:pub:"))
async def daily_publish(callback: CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    try:
        draft_id = int(callback.data.split(":")[2])
        asset = await get_draft_asset(draft_id)
        draft = await get_draft(draft_id)
        if not draft or not GASTROBAR_GROUP_ID:
            await callback.message.answer("Нет черновика или не задан GASTROBAR_GROUP_ID.")
            return
        caption = draft["text"]
        if asset and asset.get("image_path"):
            from pathlib import Path

            p = Path(asset["image_path"])
            if p.is_file():
                await bot.send_photo(GASTROBAR_GROUP_ID, photo=FSInputFile(p), caption=caption)
            else:
                await bot.send_message(GASTROBAR_GROUP_ID, caption)
        else:
            await bot.send_message(GASTROBAR_GROUP_ID, caption)
        await update_draft_status(draft_id, "published")
        await callback.message.answer("Пост дня опубликован в группу.")
    except Exception as exc:
        logger.exception("Callback failed: daily_publish", exc_info=True)
        await callback.message.answer(format_telegram_exception(exc))


@router.callback_query(F.data.startswith("daily:redo:"))
async def daily_redo(callback: CallbackQuery) -> None:
    await callback.answer("Переделываю пост…")
    import json

    user_id = callback.from_user.id
    try:
        draft_id = int(callback.data.split(":")[2])
        state = daily_post_state.get(user_id, {})
        events = state.get("events") or []
        if not events:
            asset = await get_draft_asset(draft_id)
            if asset:
                try:
                    parsed = json.loads(asset["event_json"])
                    events = parsed if isinstance(parsed, list) else [parsed]
                except json.JSONDecodeError:
                    events = []
        if not events:
            events = _events_from_context(
                user_id, str(state.get("mode", "now24"))
            )
        mode = str(state.get("mode", "now24"))
        from daily_event_posts import build_post_from_saved_events

        async with long_operation_typing(
            callback.bot,
            callback.message.chat.id,
            initial_text="🔄 Переделываю пост дня…\n✍️ Бот печатает…",
            progress_label="пост дня",
        ):
            result = await asyncio.wait_for(
                build_post_from_saved_events(
                    events,
                    mode=mode if mode in ("now24", "week") else "now24",
                    log_prefix="daily_redo",
                ),
                timeout=WEEK_FETCH_TIMEOUT_SEC,
            )
        if not result.ok or not result.package:
            await callback.message.answer(user_error_message(result))
            return
        await update_draft_status(draft_id, "cancelled")
        pkg = result.package
        daily_post_state[user_id] = {
            "draft_id": pkg.draft_id,
            "events": pkg.events,
            "event": pkg.events[0] if pkg.events else {},
            "image_path": pkg.image_path,
            "poster_source": pkg.image_source,
        }
        await deliver_daily_content(callback.bot, pkg, chat_id=callback.message.chat.id)
    except Exception as exc:
        logger.exception("Callback failed: daily_redo", exc_info=True)
        await callback.message.answer(format_telegram_exception(exc))


@router.callback_query(F.data.startswith("daily:cancel:"))
async def daily_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        draft_id = int(callback.data.split(":")[2])
        await update_draft_status(draft_id, "cancelled")
        user_id = callback.from_user.id
        daily_post_state.pop(user_id, None)
        await callback.message.answer("Пост дня отменён.")
    except Exception as exc:
        logger.exception("Callback failed: daily_cancel", exc_info=True)
        if callback.message:
            await callback.message.answer(format_telegram_exception(exc))


@router.message(Command("gemini_test"))
async def cmd_gemini_test(message: Message) -> None:
    try:
        async with show_typing(message.bot, message.chat.id):
            await message.answer("Проверяю Gemini (plain + Google Search)…")
            report = await run_gemini_test()
        await message.answer(report.format_telegram())
    except Exception as exc:
        logger.exception("gemini_test unexpected error", exc_info=True)
        from runtime_messages import gemini_test_error_message

        await message.answer(gemini_test_error_message(exc))


@router.message(Command("clear_cache"))
async def cmd_clear_cache(message: Message) -> None:
    if not message.from_user or message.from_user.id != ADMIN_ID:
        await message.answer("Команда только для администратора.")
        return
    from weekly_events_cache import clear_weekly_events_cache

    await clear_weekly_events_cache()
    await message.answer("✅ Weekly cache очищен. Следующая афиша соберётся заново.")


@router.message(Command("debug_cache"))
async def cmd_debug_cache(message: Message) -> None:
    if not message.from_user or message.from_user.id != ADMIN_ID:
        await message.answer("Команда только для администратора.")
        return
    from gemini_usage import get_gemini_calls_today_sync
    from weekly_events_cache import (
        load_weekly_events_cache,
        weekly_cache_updated_today_vn,
    )

    events = await load_weekly_events_cache()
    today = await weekly_cache_updated_today_vn()
    lines = [
        "🧪 Weekly cache debug",
        f"Событий в кэше: {len(events)}",
        f"Обновлён сегодня (VN): {'да' if today else 'нет'}",
        f"Gemini calls сегодня: {get_gemini_calls_today_sync()}",
    ]
    for i, e in enumerate(events[:8], 1):
        lines.append(
            f"{i}. {e.get('local_weekday', e.get('weekday', ''))} "
            f"{e.get('local_time', e.get('time', ''))} — {e.get('title', '')[:60]}"
        )
    if len(events) > 8:
        lines.append(f"… ещё {len(events) - 8}")
    await message.answer("\n".join(lines))


@router.message(Command("radar_debug"))
async def cmd_radar_debug(message: Message) -> None:
    if not message.from_user or message.from_user.id != ADMIN_ID:
        await message.answer("Команда только для администратора.")
        return
    from event_radar_pipeline import get_radar_debug_report

    await message.answer("⏳ Собираю rule-based radar debug (API)…")
    async with show_typing(message.bot, message.chat.id):
        try:
            body = await asyncio.wait_for(get_radar_debug_report(), timeout=120.0)
        except asyncio.TimeoutError:
            await message.answer("❌ radar_debug: таймаут API (>120 с).")
            return
        except Exception as exc:
            logger.exception("RADAR_DEBUG unexpected error", exc_info=True)
            await message.answer(format_telegram_exception(exc))
            return
    if len(body) > 4000:
        body = body[:3990] + "\n…(обрезано)"
    await message.answer(body)


@router.message(Command("api_status"))
async def cmd_api_status(message: Message) -> None:
    from api_sports_status import format_api_status_report

    async with show_typing(message.bot, message.chat.id):
        try:
            body = await asyncio.wait_for(format_api_status_report(), timeout=90.0)
        except asyncio.TimeoutError:
            await message.answer("⏱ /api_status: таймаут (>90 с).")
            return
        except Exception as exc:
            logger.exception("API_STATUS unexpected error", exc_info=True)
            await message.answer(format_telegram_exception(exc))
            return
    if len(body) > 4000:
        body = body[:3990] + "\n…(обрезано)"
    await message.answer(body)


@router.message(Command("check"))
async def cmd_check(message: Message) -> None:
    telegram_line = "✅ Telegram API connected"
    async with show_typing(message.bot, message.chat.id):
        gemini, sports = await asyncio.gather(check_gemini(), check_sports_api())
    lines = [telegram_line]
    lines.append(
        ("✅ " if gemini.ok else "❌ ")
        + "Gemini API "
        + ("connected" if gemini.ok else (gemini.details or "error"))
    )
    lines.append(
        ("✅ " if sports.ok else "❌ ")
        + "API-SPORTS "
        + ("connected" if sports.ok else (sports.details or "error"))
    )
    await message.answer("\n".join(lines))


@router.message(F.text)
async def cmd_help_hint(message: Message) -> None:
    """Подсказка: без команд бот молчит — пользователь мог отправить обычный текст или неизвестную команду."""
    if message.text and message.text.strip().startswith("/"):
        logger.info("unhandled command text=%r chat=%s", message.text, message.chat.id)
    await message.answer(
        "Я отвечаю только на команды:\n"
        "/start — проверка, что бот на связи\n"
        "/events — Event Radar (24 ч / афиша на 3 дня)\n"
        "/daily — готовый пост на сегодня\n"
        "/check — проверка подключений API\n"
        "/api_status — статус API-SPORTS по видам спорта\n"
        "/gemini_test — тест Gemini и Google Search\n\n"
        "В меню Telegram: /start, /events, /daily, /check, /gemini_test. Команду /week можно ввести вручную — "
        "она вызывает тот же сценарий, что и /events.\n\n"
        "Напишите одну из них (со слэшем в начале)."
    )


@router.callback_query(F.data.startswith("draft:pub:"))
async def draft_publish(callback: CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    try:
        draft_id = int(callback.data.split(":")[2])
        text = callback.message.text or ""
        if not GASTROBAR_GROUP_ID:
            await callback.message.answer("Не задан GASTROBAR_GROUP_ID в .env")
            return
        await bot.send_message(GASTROBAR_GROUP_ID, text)
        await update_draft_status(draft_id, "published")
        logger.info("draft published: id=%s", draft_id)
        await callback.message.answer("Опубликовал в группу.")
    except Exception as exc:
        logger.exception("Callback failed: draft_publish", exc_info=True)
        await callback.message.answer(format_telegram_exception(exc))


@router.callback_query(F.data.startswith("draft:redo:"))
async def draft_redo(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        user_id = callback.from_user.id
        state = last_draft_state.get(user_id)
        if not state:
            await callback.message.answer("Нет черновика для переделки.")
            return
        old_draft_id = int(state["draft_id"])
        events = state["events"]
        async with show_typing(callback.bot, callback.message.chat.id):
            new_text = await generate_weekly_poster(events)
        await update_draft_status(old_draft_id, "cancelled")
        new_draft_id = await insert_draft("week_post", new_text, "draft")
        last_draft_state[user_id] = {"draft_id": new_draft_id, "events": events}
        logger.info("draft regenerated: old_id=%s new_id=%s", old_draft_id, new_draft_id)
        await callback.message.answer(new_text, reply_markup=post_result_kb(new_draft_id))
    except Exception as exc:
        logger.exception("Callback failed: draft_redo", exc_info=True)
        await callback.message.answer(format_telegram_exception(exc))


@router.callback_query(F.data.startswith("draft:cancel:"))
async def draft_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        draft_id = int(callback.data.split(":")[2])
        await update_draft_status(draft_id, "cancelled")
        user_id = callback.from_user.id
        if user_id in last_draft_state:
            del last_draft_state[user_id]
        logger.info("draft cancelled: id=%s", draft_id)
        await callback.message.answer("Ок, не публикуем.")
    except Exception as exc:
        logger.exception("Callback failed: draft_cancel", exc_info=True)
        await callback.message.answer(format_telegram_exception(exc))


async def main() -> None:
    from startup import (
        log_startup_banner,
        run_polling,
        setup_scheduler_if_admin,
        validate_required_config,
    )

    log.info("Starting Gastrobar bot...")
    log_startup_banner()
    validate_required_config()
    log.info("aiogram version: %s", getattr(aiogram, "__version__", "unknown"))

    await init_db()
    from weekly_events_cache import load_weekly_events_cache

    await load_weekly_events_cache()
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            logger.error(
                "TELEGRAM_BOT_TOKEN отклонён Telegram (Revoke / неверный токен). "
                "BotFather → новый токен → Railway Variables → Redeploy."
            )
            print(
                "TELEGRAM_BOT_TOKEN invalid. Update token in Railway Variables and redeploy.",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        logger.info("Running bot username: @%s", me.username)
        logger.info("Running bot id: %s name: %r", me.id, me.full_name)
        _assert_expected_bot_username(me.username)

        await _clear_webhook_and_log(bot)
        await set_bot_commands(bot)
        await _probe_unique_get_updates_client(bot)

        from bot_update_log import LogUpdatesMiddleware

        dp = FatalConflictDispatcher()
        dp.update.outer_middleware(LogUpdatesMiddleware())
        dp.include_router(router)
        logger.info(
            "Handlers ready: /events /daily /check (router included)"
        )
        setup_scheduler_if_admin(bot)
        try:
            await run_polling(dp, bot)
        except TelegramConflictError:
            _fail_conflict("polling")
        except TelegramUnauthorizedError:
            logger.error(
                "Polling: Unauthorized — обновите TELEGRAM_BOT_TOKEN в Railway и Redeploy."
            )
            raise SystemExit(1) from None
    finally:
        shutdown_scheduler()
        await bot.session.close()


if __name__ == "__main__":
    from startup import acquire_instance_lock, release_instance_lock

    if not acquire_instance_lock():
        sys.exit(1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — graceful shutdown")
    except SystemExit:
        raise
    except TelegramConflictError:
        _fail_conflict("main")
    except Exception:
        logger.exception("Fatal error in main")
        sys.exit(1)
    finally:
        release_instance_lock()
