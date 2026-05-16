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
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Message,
)

from ai_generator import generate_weekly_poster
from api_checks import check_gemini, check_sports_api
from bot_instance_lock import release_bot_lock, try_acquire_bot_lock
from fatal_conflict_dispatcher import FatalConflictDispatcher
from config import GASTROBAR_GROUP_ID, GEMINI_API_KEY, TELEGRAM_BOT_TOKEN
from database import init_db, insert_draft, update_draft_status
from event_radar import format_radar_afisha, get_event_radar_week

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
# Последняя сессия /events или /week: подборка до генерации афиши
week_editor_state: dict[int, dict[str, Any]] = {}


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


def week_generate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Сгенерировать афишу", callback_data="week:generate")],
        ]
    )


def draft_actions_kb(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"draft:pub:{draft_id}")],
            [InlineKeyboardButton(text="🔄 Переделать", callback_data=f"draft:redo:{draft_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"draft:cancel:{draft_id}")],
        ]
    )


async def _probe_unique_get_updates_client(bot: Bot) -> None:
    """Быстрая проверка: никто другой не держит long polling по этому токену."""
    try:
        await bot.get_updates(limit=1, offset=-1, timeout=1)
    except TelegramConflictError:
        logger.error(
            "TelegramConflictError before polling: токен уже используется для getUpdates "
            "(вторая копия бота, другой ПК/сервер или конфликт с webhook). "
            "Остановите другой процесс или выпустите новый токен в BotFather."
        )
        print(
            "Another client is already polling this bot token. Stop it or use a new token.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


async def set_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(
            command="events",
            description="Собрать события недели",
        ),
        BotCommand(command="check", description="Проверить API подключения"),
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
        "/start, /events и /check.\n\n"
        "Собрать афишу недели (Event Radar): пункт «Собрать события недели» или команда /events.\n"
        "Команду /week можно ввести вручную — она делает то же, что /events (в меню не показывается).\n\n"
        "Если списка нет: полностью закрой чат с ботом и открой снова, либо обнови Telegram."
    )


async def run_event_radar_flow(message: Message) -> None:
    try:
        logger.info("Event Radar command: %s", message.text)
        await message.answer(
            "Ищу события недели для бара (Gemini Search, разные категории)…"
        )
        try:
            events, raw_total, pre_count, selected, fetch_note = await asyncio.wait_for(
                get_event_radar_week(),
                timeout=WEEK_FETCH_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Event Radar: таймаут (%s c)",
                WEEK_FETCH_TIMEOUT_SEC,
            )
            await message.answer(
                "Таймаут поиска. Проверьте интернет и GEMINI_API_KEY, повторите /events."
            )
            return
        except Exception:
            logger.exception("Event Radar failed")
            await message.answer(
                "Не удалось получить подборку Event Radar. "
                "Проверьте GEMINI_API_KEY и доступность Gemini."
            )
            return
        user_id = message.from_user.id if message.from_user else 0

        if selected == 0:
            if fetch_note == "gemini_quota":
                await message.answer(
                    "Сработал дневной лимит бесплатного Gemini (free tier: мало запросов "
                    "на модель в сутки). Подождите до завтра, подключите биллинг в Google AI Studio "
                    "или используйте другой API-ключ/проект.\n\n"
                    "Справка по лимитам: https://ai.google.dev/gemini-api/docs/rate-limits"
                )
            elif fetch_note == "gemini_error":
                await message.answer(
                    "Gemini ответил с ошибкой при поиске недели (не 429). "
                    "Проверьте GEMINI_MODEL и ключ, повторите /events позже."
                )
            elif raw_total > 0 and pre_count > 0:
                await message.answer(
                    "Кандидаты из поиска есть, но после проверки расписания "
                    "(дата, время, источник) и правил бара в подборку ничего не вошло. "
                    "Так бывает, если модель не подтвердила время или событие не подходит под фильтры бара."
                )
            elif raw_total > 0 and pre_count == 0:
                await message.answer(
                    "Поиск вернул строки в JSON, но ни одна не прошла первичную проверку "
                    "(формат даты/времени, таймзона, фильтры бара). Попробуйте /events ещё раз через минуту."
                )
            else:
                logger.warning(
                    "Event Radar empty: raw_total=%s pre_count=%s fetch_note=%s",
                    raw_total,
                    pre_count,
                    fetch_note,
                )
                await message.answer(
                    "Подборка пуста: поиск не вернул ни одного события "
                    "(пустой список от модели, сеть или лимиты API).\n\n"
                    "Сначала выполните /check — там та же модель Gemini, что и в /events. "
                    "На бесплатном тарифе лимит маленький (порядка 20 запросов в сутки на модель); "
                    "при исчерпании подождите до завтра или подключите биллинг.\n\n"
                    "Лимиты: https://ai.google.dev/gemini-api/docs/rate-limits"
                )
            week_editor_state.pop(user_id, None)
            return

        week_editor_state[user_id] = {
            "events": events,
            "raw_total": raw_total,
            "pre_count": pre_count,
            "selected": selected,
        }
        text = (
            "🔭 Event Radar · Gemini Search\n"
            f"{_ru_found_events_line(raw_total)}\n"
            f"{_ru_selected_main_line(selected)}\n\n"
            f"{format_radar_afisha(events)}"
        )
        await message.answer(text, reply_markup=week_generate_kb())
    except Exception:
        logger.exception("Unhandled error in Event Radar handler")
        await message.answer("Ошибка при сборе событий. Попробуйте /events ещё раз.")


@router.message(Command("events", "week"))
async def cmd_events_or_week(message: Message) -> None:
    await run_event_radar_flow(message)


@router.callback_query(F.data == "week:generate")
async def week_generate_poster(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        user_id = callback.from_user.id
        sess = week_editor_state.get(user_id)
        if not sess:
            await callback.message.answer(
                "Сессия устарела. Нажми /events ещё раз (или введи /week — то же самое)."
            )
            return
        events = sess["events"]
        if not events:
            await callback.message.answer("Нет событий для генерации.")
            return

        await callback.message.answer("Пишу афишу…")
        post_text = await generate_weekly_poster(events)
        draft_id = await insert_draft("week_post", post_text, "draft")
        last_draft_state[user_id] = {"draft_id": draft_id, "events": events}
        logger.info("draft created after generate: id=%s", draft_id)
        await callback.message.answer(post_text, reply_markup=draft_actions_kb(draft_id))
    except Exception:
        logger.exception("week:generate failed")
        await callback.message.answer("Не удалось сгенерировать афишу.")


@router.message(Command("check"))
async def cmd_check(message: Message) -> None:
    telegram_line = "✅ Telegram API connected"
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
        "/events — собрать события недели (Event Radar)\n"
        "/check — проверка подключений API\n\n"
        "В меню Telegram: /start, /events, /check. Команду /week можно ввести вручную — "
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
        new_text = await generate_weekly_poster(events)
        await update_draft_status(old_draft_id, "cancelled")
        new_draft_id = await insert_draft("week_post", new_text, "draft")
        last_draft_state[user_id] = {"draft_id": new_draft_id, "events": events}
        logger.info("draft regenerated: old_id=%s new_id=%s", old_draft_id, new_draft_id)
        await callback.message.answer(new_text, reply_markup=draft_actions_kb(new_draft_id))
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
    log.info("Starting bot...")
    log.info("aiogram version: %s", getattr(aiogram, "__version__", "unknown"))

    log.info("Token loaded: %s", "yes" if TELEGRAM_BOT_TOKEN else "no")
    log.info("GEMINI_API_KEY loaded: %s", "yes" if GEMINI_API_KEY else "no")
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
        log.info("Bot username: @%s", me.username)

        await bot.delete_webhook(drop_pending_updates=True)
        await set_bot_commands(bot)
        await _probe_unique_get_updates_client(bot)

        dp = FatalConflictDispatcher()
        dp.include_router(router)
        logger.info("/events handler registered (алиас /week без меню) and router included")
        log.info("Polling started")
        try:
            await dp.start_polling(bot)
        except TelegramConflictError:
            logger.error(
                "Polling остановлен: другой клиент перехватил getUpdates. "
                "Проверь второй терминал, сервер или webhook по этому токену."
            )
            print(
                "TelegramConflictError during polling. Only one getUpdates client is allowed.",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
    finally:
        await bot.session.close()


if __name__ == "__main__":
    _lock_path = Path(__file__).resolve().parent / "bot.lock"
    if not try_acquire_bot_lock(_lock_path, logger):
        sys.exit(1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt, shutting down")
    finally:
        release_bot_lock(_lock_path, logger)
