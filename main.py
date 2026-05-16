"""Минимальный тестовый запуск aiogram 3 (диагностика)."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import aiogram
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramConflictError
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
    build_daily_content_package,
    deliver_daily_content,
    user_error_message,
)
from api_checks import check_gemini, check_sports_api
from bot_instance_lock import release_bot_lock, try_acquire_bot_lock
import time

from bot_typing import long_operation_typing, show_typing
from fatal_conflict_dispatcher import FatalConflictDispatcher
from config import (
    ADMIN_ID,
    EXPECTED_BOT_USERNAME,
    GASTROBAR_GROUP_ID,
    GEMINI_API_KEY,
    RUN_MODE,
    TELEGRAM_BOT_TOKEN,
    is_railway_run,
)
from gemini_client import effective_gemini_model, run_gemini_test
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
from scheduler import setup_jobs, shutdown_scheduler, start_scheduler
from event_radar import (
    format_radar_now24_message,
    format_radar_week_message,
    get_event_radar_now24,
    get_event_radar_week,
    radar_fetch_header,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)
logger = log

router = Router()
# Event Radar (Gemini + Google Search) может занять дольше, чем спортивный API.
WEEK_FETCH_TIMEOUT_SEC = 240.0
last_draft_state: dict[int, dict[str, Any]] = {}
last_week_events: dict[int, list[dict[str, Any]]] = {}
last_now24_events: dict[int, list[dict[str, Any]]] = {}
daily_post_state: dict[int, dict[str, Any]] = {}


def _ru_found_events_line(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        w = "событие"
    elif 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        w = "события"
    else:
        w = "событий"
    return f"Найдено {n} {w}."


def _ru_selected_main_line(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"Выбрано {n} главное событие недели."
    if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return f"Выбрано {n} главных события недели."
    return f"Выбрано {n} главных событий недели."


def _fail_conflict(context: str) -> None:
    logger.error(
        "%s: TelegramConflictError — токен уже занят другим polling/webhook. "
        "Проверьте TELEGRAM_BOT_TOKEN на Railway (должен быть @%s, не SpiceSpace), "
        "остановите локальный main.py и второй deployment.",
        context,
        EXPECTED_BOT_USERNAME or "?",
    )
    print(
        "TelegramConflictError: stop duplicate polling or fix TELEGRAM_BOT_TOKEN on Railway.",
        file=sys.stderr,
    )
    raise SystemExit(1)


async def _probe_unique_get_updates_client(bot: Bot) -> None:
    """Быстрая проверка: никто другой не держит long polling по этому токену."""
    try:
        await bot.get_updates(limit=1, offset=-1, timeout=1)
        logger.info("getUpdates probe: ok (no conflict before polling)")
    except TelegramConflictError:
        _fail_conflict("getUpdates_probe")


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
        ok = await bot.delete_webhook(drop_pending_updates=True)
        logger.info("delete_webhook(drop_pending_updates=True): result=%s", ok)
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
    logger.error(
        "Wrong bot token: running @%s but EXPECTED_BOT_USERNAME=@%s. "
        "На Railway в Variables замените TELEGRAM_BOT_TOKEN на токен Gastrobar из BotFather.",
        username,
        want,
    )
    print(
        f"Wrong TELEGRAM_BOT_TOKEN: @{username} != @{want}. Fix Railway Variables.",
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
    await message.answer(
        f"Бот живой. Твой ID: {user_id}\n\n"
        "Команды меню обновлены. Открой кнопку меню слева от поля ввода — там "
        "/start, /events, /check и /gemini_test.\n\n"
        "Event Radar: /events — меню (афиша недели или события 24 ч).\n"
        "Команду /week можно ввести вручную — она делает то же, что /events (в меню не показывается).\n\n"
        "Если списка нет: полностью закрой чат с ботом и открой снова, либо обнови Telegram."
    )


def _events_menu_text() -> str:
    return (
        "Что собрать?\n\n"
        "📅 Афиша на неделю\n"
        "— главные события ближайших 7 дней\n\n"
        "⚡ События ближайших 24 часов\n"
        "— события, которые начнутся в течение суток, плюс готовый пост для Telegram"
    )


async def _answer_radar_empty(
    message: Message,
    *,
    raw_total: int,
    pre_count: int,
    fetch_note: str | None,
) -> None:
    if fetch_note == "gemini_quota":
        await message.answer(
            "Лимит Gemini исчерпан. Подождите до завтра или подключите биллинг.\n"
            "https://ai.google.dev/gemini-api/docs/rate-limits"
        )
    elif fetch_note == "gemini_error":
        await message.answer("Ошибка Gemini. Выполните /gemini_test и проверьте логи.")
    elif raw_total > 0:
        await message.answer(
            "Поиск нашёл кандидатов, но ничего не прошло фильтры бара/verify. "
            "Попробуйте «Обновить» через минуту."
        )
    else:
        await message.answer(
            "Подборка пуста. Проверьте GEMINI_API_KEY (/check) и повторите."
        )


async def _run_radar_mode(
    target: Message | CallbackQuery,
    mode: str,
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

    chat_id = message.chat.id
    fetch_fn = get_event_radar_week if mode == "week" else get_event_radar_now24
    label = "афиша на неделю" if mode == "week" else "события 24 часа"

    events: list[dict[str, Any]] = []
    raw_total = pre_count = selected = 0
    fetch_note: str | None = None

    try:
        async with long_operation_typing(
            bot,
            chat_id,
            initial_text=(
                f"🔍 Ищу: {label}\n"
                "✍️ Бот печатает… (Gemini Search + проверка)"
            ),
            progress_label=label,
        ) as status_msg:
            t0 = time.monotonic()
            logger.info("radar:%s fetch started user_id=%s", mode, user_id)
            try:
                events, raw_total, pre_count, selected, fetch_note = await asyncio.wait_for(
                    fetch_fn(),
                    timeout=WEEK_FETCH_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "radar:%s timeout after %.0fs user_id=%s",
                    mode,
                    time.monotonic() - t0,
                    user_id,
                )
                await status_msg.edit_text(
                    f"⏱ Поиск занял больше {int(WEEK_FETCH_TIMEOUT_SEC // 60)} мин.\n"
                    "Попробуйте «Обновить» через минуту или /check (Gemini API)."
                )
                return
            except Exception:
                logger.exception(
                    "radar:%s failed after %.1fs",
                    mode,
                    time.monotonic() - t0,
                )
                await status_msg.edit_text(
                    "❌ Не удалось собрать Event Radar.\n"
                    "Проверьте GEMINI_API_KEY (/check) и логи Railway."
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
                pass

        if mode == "week":
            last_week_events[user_id] = events if selected else []
        else:
            last_now24_events[user_id] = events if selected else []

        if selected == 0:
            if mode == "now24":
                await message.answer(
                    "Нет событий в ближайшие 24 часа.",
                    reply_markup=radar_now24_result_kb(),
                )
            else:
                await _answer_radar_empty(
                    message,
                    raw_total=raw_total,
                    pre_count=pre_count,
                    fetch_note=fetch_note,
                )
            return

        await save_radar_snapshot(
            mode, events, {"raw_total": raw_total, "selected": selected}
        )

        extra = radar_fetch_header(fetch_note)
        stats = f"{_ru_found_events_line(raw_total)}\n{_ru_selected_main_line(selected)}"
        body = (
            format_radar_week_message(events)
            if mode == "week"
            else format_radar_now24_message(events)
        )
        text = f"{extra}\n{stats}\n\n{body}" if extra else f"{stats}\n\n{body}"
        kb = radar_week_result_kb() if mode == "week" else radar_now24_result_kb()
        async with show_typing(bot, chat_id):
            await message.answer(text, reply_markup=kb)
    except Exception:
        logger.exception("radar:%s unhandled error user_id=%s", mode, user_id)
        await message.answer(
            "Произошла ошибка при поиске. Попробуйте /events ещё раз."
        )


@router.message(Command("events", "week"))
async def cmd_events_or_week(message: Message) -> None:
    logger.info("Events menu rendered with week and now24 buttons")
    await message.answer(_events_menu_text(), reply_markup=radar_menu_kb())


@router.callback_query(F.data == "radar:week")
async def radar_week(callback: CallbackQuery) -> None:
    await callback.answer()
    await _run_radar_mode(callback, "week")


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
    if callback.message:
        await callback.message.answer("Event Radar закрыт.")


@router.callback_query(F.data == "radar:week:gen")
async def radar_week_generate(callback: CallbackQuery) -> None:
    await callback.answer()
    events = last_week_events.get(callback.from_user.id) or []
    if not events:
        await callback.message.answer(
            "Сессия устарела. Нажмите /events → Афиша на неделю."
        )
        return
    async with show_typing(callback.bot, callback.message.chat.id):
        await callback.message.answer("Пишу пост по афише недели…")
        post_text = await generate_weekly_poster(events)
    draft_id = await insert_draft("week_post", post_text, "draft")
    last_draft_state[callback.from_user.id] = {"draft_id": draft_id, "events": events}
    await callback.message.answer(post_text, reply_markup=post_result_kb(draft_id))


@router.callback_query(F.data == "radar:post_now24")
async def radar_post_now24(callback: CallbackQuery) -> None:
    await callback.answer()
    logger.info("radar:post_now24 clicked")
    events = last_now24_events.get(callback.from_user.id) or []
    if not events:
        await callback.message.answer("Нет событий для поста на ближайшие 24 часа.")
        return
    chat_id = callback.message.chat.id
    try:
        async with long_operation_typing(
            callback.bot,
            chat_id,
            initial_text="📋 Готовлю пост на сегодня…\n✍️ Бот печатает…",
            progress_label="пост на сегодня",
        ):
            result = await asyncio.wait_for(
                build_daily_content_package(events, log_prefix="radar_post_now24"),
                timeout=WEEK_FETCH_TIMEOUT_SEC,
            )
    except asyncio.TimeoutError:
        await callback.message.answer("Таймаут. Повторите позже.")
        return
    if not result.ok or not result.package:
        await callback.message.answer(user_error_message(result))
        return
    pkg = result.package
    daily_post_state[callback.from_user.id] = {
        "draft_id": pkg.draft_id,
        "event": pkg.events[0] if pkg.events else {},
        "events": pkg.events,
        "image_path": pkg.image_path,
        "poster_source": pkg.image_source,
    }
    await deliver_daily_content(callback.bot, pkg, chat_id=chat_id)


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
    except Exception:
        logger.exception("cmd /daily unexpected error")
        await message.answer("Ошибка: unexpected error")


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
    except Exception:
        logger.exception("daily publish failed")
        await callback.message.answer("Не удалось опубликовать пост дня.")


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
            events = last_now24_events.get(user_id) or []
        async with long_operation_typing(
            callback.bot,
            callback.message.chat.id,
            initial_text="🔄 Переделываю пост дня…\n✍️ Бот печатает…",
            progress_label="пост дня",
        ):
            result = await asyncio.wait_for(
                build_daily_content_package(events, log_prefix="daily_redo"),
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
    except Exception:
        logger.exception("daily redo failed")
        await callback.message.answer("Не удалось переделать пост.")


@router.callback_query(F.data.startswith("daily:cancel:"))
async def daily_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        draft_id = int(callback.data.split(":")[2])
        await update_draft_status(draft_id, "cancelled")
        user_id = callback.from_user.id
        daily_post_state.pop(user_id, None)
        await callback.message.answer("Пост дня отменён.")
    except Exception:
        logger.exception("daily cancel failed")


@router.message(Command("gemini_test"))
async def cmd_gemini_test(message: Message) -> None:
    try:
        async with show_typing(message.bot, message.chat.id):
            await message.answer("Проверяю Gemini (plain + Google Search)…")
            report = await run_gemini_test()
        await message.answer(report.format_telegram())
    except Exception:
        logger.exception("gemini_test failed")
        await message.answer("Ошибка при /gemini_test. Смотрите Railway logs.")


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
    await message.answer(
        "Я отвечаю только на команды:\n"
        "/start — проверка, что бот на связи\n"
        "/events — Event Radar (меню: неделя / 24 ч)\n"
        "/daily — готовый пост на сегодня\n"
        "/check — проверка подключений API\n"
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
    except Exception:
        logger.exception("draft publish failed")
        await callback.message.answer("Не удалось опубликовать.")


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
    except Exception:
        logger.exception("draft regenerate failed")
        await callback.message.answer("Не удалось переделать пост.")


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
    except Exception:
        logger.exception("draft cancel failed")
        await callback.message.answer("Не удалось отменить черновик.")


async def main() -> None:
    import os

    log.info("Starting bot...")
    log.info("RUN_MODE=%s", RUN_MODE)
    if is_railway_run():
        log.info(
            "Railway env: RAILWAY_ENVIRONMENT=%r RAILWAY_SERVICE_NAME=%r",
            os.getenv("RAILWAY_ENVIRONMENT"),
            os.getenv("RAILWAY_SERVICE_NAME"),
        )
    log.info("aiogram version: %s", getattr(aiogram, "__version__", "unknown"))

    log.info("Token loaded: %s", "yes" if TELEGRAM_BOT_TOKEN else "no")
    log.info("GEMINI_API_KEY loaded: %s", "yes" if GEMINI_API_KEY else "no")
    log.info("GEMINI_MODEL: %s", effective_gemini_model())
    if not GEMINI_API_KEY:
        log.warning(
            "GEMINI_API_KEY пуст в .env — команда /events (Event Radar) не заработает"
        )
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing")

    await init_db()
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        me = await bot.get_me()
        logger.info("Running bot username: @%s", me.username)
        logger.info("Running bot id: %s name: %r", me.id, me.full_name)
        _assert_expected_bot_username(me.username)

        await _clear_webhook_and_log(bot)
        await set_bot_commands(bot)
        await _probe_unique_get_updates_client(bot)

        dp = FatalConflictDispatcher()
        dp.include_router(router)
        logger.info("/events handler registered (алиас /week без меню) and router included")
        if ADMIN_ID:
            setup_jobs(bot)
            start_scheduler()
            log.info("Scheduler started for ADMIN_ID=%s", ADMIN_ID)
        else:
            log.warning("ADMIN_ID not set — scheduler disabled")
        log.info("Polling started (single instance, fail-fast on conflict)")
        try:
            await dp.start_polling(bot)
        except TelegramConflictError:
            _fail_conflict("polling")
        finally:
            shutdown_scheduler()
    finally:
        await bot.session.close()


if __name__ == "__main__":
    _lock_path = Path(__file__).resolve().parent / "bot.lock"
    if is_railway_run():
        logger.info("RUN_MODE=railway — bot.lock disabled (Railway must run one replica)")
    elif not try_acquire_bot_lock(_lock_path, logger):
        sys.exit(1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt, shutting down")
    except SystemExit:
        raise
    except TelegramConflictError:
        _fail_conflict("main")
    finally:
        if not is_railway_run():
            release_bot_lock(_lock_path, logger)
