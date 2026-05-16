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
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Message,
)

from ai_generator import generate_daily_event_post, generate_weekly_poster
from daily_event import (
    enrich_daily_campaign_meta,
    fetch_week_events_for_daily,
    get_next_featured_event,
)
from daily_poster import regenerate_poster_only, resolve_event_poster
from api_checks import check_gemini, check_sports_api
from bot_instance_lock import release_bot_lock, try_acquire_bot_lock
from bot_typing import show_typing
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
    update_draft_status,
    update_draft_text,
    upsert_draft_asset,
)
from keyboards import daily_post_kb
from scheduler import (
    get_pending_daily,
    set_pending_daily,
    setup_jobs,
    shutdown_scheduler,
    start_scheduler,
)
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


def week_generate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Сгенерировать афишу", callback_data="week:generate")],
            [
                InlineKeyboardButton(
                    text="🔥 Сгенерировать пост дня",
                    callback_data="daily:generate",
                )
            ],
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
            description="Собрать события недели",
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
        "Собрать афишу недели (Event Radar): пункт «Собрать события недели» или команда /events.\n"
        "Команду /week можно ввести вручную — она делает то же, что /events (в меню не показывается).\n\n"
        "Если списка нет: полностью закрой чат с ботом и открой снова, либо обнови Telegram."
    )


async def run_event_radar_flow(message: Message) -> None:
    try:
        logger.info("Event Radar command: %s", message.text)
        bot = message.bot
        chat_id = message.chat.id
        async with show_typing(bot, chat_id):
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
                        "Выполните /gemini_test — детали в Railway logs. "
                        "Модель: проверьте GEMINI_MODEL (сейчас по умолчанию gemini-2.5-flash)."
                    )
                elif raw_total > 0 and pre_count > 0:
                    await message.answer(
                        "Gemini Search нашёл события, но ни одно не подошло под правила бара "
                        "(сериальные финалы, абстрактные названия без матча и т.п.). "
                        "Попробуйте /events ещё раз — состав недели мог обновиться."
                    )
                elif raw_total > 0 and pre_count == 0:
                    await message.answer(
                        "Поиск вернул строки в JSON, но ни одна не прошла первичную проверку "
                        "(формат даты/времени, таймзона, фильтры бара). "
                        "Попробуйте /events ещё раз через минуту."
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
            header = "🔭 Event Radar · Gemini Search"
            if fetch_note == "search_fallback":
                header = (
                    "🔭 Event Radar · Gemini (fallback)\n"
                    "Google Search недоступен, использую fallback."
                )
            elif fetch_note == "sports_fallback":
                header = (
                    "🔭 Event Radar · API-SPORTS (резерв)\n"
                    "Лимит Gemini исчерпан — показана подборка из спортивного API."
                )
            text = (
                f"{header}\n"
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


async def _build_and_send_daily_post(
    target: Message | CallbackQuery,
    *,
    featured_event: dict[str, Any] | None = None,
    redo_text: bool = False,
    redo_image: bool = False,
    draft_id: int | None = None,
) -> None:
    """Собрать пост дня: текст + постер, отправить превью."""
    import json
    from pathlib import Path

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
    state = daily_post_state.get(user_id, {})

    if redo_text or redo_image:
        if not state and draft_id:
            asset = await get_draft_asset(draft_id)
            draft_row = await get_draft(draft_id)
            if asset and draft_row:
                state = {
                    "draft_id": draft_id,
                    "event": json.loads(asset["event_json"]),
                    "image_path": asset["image_path"],
                    "poster_source": asset.get("poster_source", ""),
                }
        if not state:
            await message.answer(
                "Сессия поста дня устарела. Нажми «Сгенерировать пост дня» снова."
            )
            return
        ev = state["event"]
        did = int(state["draft_id"])
        image_path = state.get("image_path")
        poster_source = state.get("poster_source", "")
    else:
        if featured_event:
            ev = enrich_daily_campaign_meta(featured_event)
        else:
            async with show_typing(bot, chat_id):
                await message.answer(
                    "Ищу главное событие на ближайшие 24 часа (пост дня)…"
                )
                pool = await asyncio.wait_for(
                    fetch_week_events_for_daily(),
                    timeout=WEEK_FETCH_TIMEOUT_SEC,
                )
                ev = get_next_featured_event(pool)
        if not ev:
            await message.answer(
                "Нет подходящего события для поста дня в ближайшие 24 часа "
                "(рабочие часы бара, приоритет, окно публикации). Сначала /events."
            )
            return
        did = None
        image_path = None
        poster_source = ""

    image_bytes: bytes | None = None
    async with show_typing(bot, chat_id):
        if redo_image:
            draft_row = await get_draft(did)
            caption = (draft_row or {}).get("text", "") if draft_row else ""
            image_bytes, poster_source, image_path = await regenerate_poster_only(
                ev, draft_id=did, force_ai=True
            )
        elif redo_text:
            caption = await generate_daily_event_post(ev)
        else:
            caption = await generate_daily_event_post(ev)
            if did is None:
                did = await insert_draft("daily_post", caption, "draft")
            else:
                await update_draft_text(did, caption)
            image_bytes, poster_source, image_path = await resolve_event_poster(
                ev, draft_id=did
            )

    if not caption:
        await message.answer("Не удалось сгенерировать текст поста дня.")
        return

    if did is None:
        did = await insert_draft("daily_post", caption, "draft")
    elif redo_text or redo_image:
        await update_draft_status(did, "draft")
        if redo_text:
            await update_draft_text(did, caption)

    if image_path or image_bytes:
        await upsert_draft_asset(
            did,
            image_path=image_path or "",
            event_json=json.dumps(ev, ensure_ascii=False),
            poster_source=poster_source,
        )

    daily_post_state[user_id] = {
        "draft_id": did,
        "event": ev,
        "image_path": image_path,
        "poster_source": poster_source,
    }

    preview = (
        f"📣 Пост дня · {ev.get('daily_timing_phrase', '')}\n"
        f"{ev.get('title', '')}\n"
        f"Постер: {poster_source or 'нет'}"
    )
    if image_path and Path(image_path).is_file():
        await bot.send_photo(
            chat_id,
            photo=FSInputFile(image_path),
            caption=caption,
            reply_markup=daily_post_kb(did),
        )
    elif image_bytes:
        await bot.send_photo(
            chat_id,
            photo=BufferedInputFile(image_bytes, filename="daily_poster.png"),
            caption=caption,
            reply_markup=daily_post_kb(did),
        )
    else:
        await message.answer(caption, reply_markup=daily_post_kb(did))
    await message.answer(preview)


@router.message(Command("daily"))
async def cmd_daily(message: Message) -> None:
    try:
        pending = get_pending_daily()
        featured = pending[0] if pending else None
        await _build_and_send_daily_post(message, featured_event=featured)
    except asyncio.TimeoutError:
        await message.answer("Таймаут. Повторите позже.")
    except Exception:
        logger.exception("cmd /daily failed")
        await message.answer("Не удалось сгенерировать пост дня.")


@router.callback_query(F.data == "daily:post")
async def daily_post_from_alert(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        pending = get_pending_daily()
        featured = pending[0] if pending else None
        set_pending_daily(None)
        await _build_and_send_daily_post(callback, featured_event=featured)
    except Exception:
        logger.exception("daily:post failed")
        await callback.message.answer("Не удалось сгенерировать пост дня.")


@router.callback_query(F.data == "daily:skip")
async def daily_skip(callback: CallbackQuery) -> None:
    await callback.answer()
    set_pending_daily(None)
    await callback.message.answer("Пост дня пропущен.")


@router.callback_query(F.data == "daily:generate")
async def daily_generate(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        await _build_and_send_daily_post(callback)
    except asyncio.TimeoutError:
        await callback.message.answer("Таймаут. Повторите позже.")
    except Exception:
        logger.exception("daily:generate failed")
        await callback.message.answer("Не удалось сгенерировать пост дня.")


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


@router.callback_query(F.data.startswith("daily:redo_text:"))
async def daily_redo_text(callback: CallbackQuery) -> None:
    await callback.answer("Переписываю текст…")
    try:
        draft_id = int(callback.data.split(":")[2])
        await _build_and_send_daily_post(callback, redo_text=True, draft_id=draft_id)
    except Exception:
        logger.exception("daily redo text failed")
        await callback.message.answer("Не удалось переделать текст.")


@router.callback_query(F.data.startswith("daily:redo_img:"))
async def daily_redo_image(callback: CallbackQuery) -> None:
    await callback.answer("Новый постер…")
    try:
        draft_id = int(callback.data.split(":")[2])
        await _build_and_send_daily_post(callback, redo_image=True, draft_id=draft_id)
    except Exception:
        logger.exception("daily redo image failed")
        await callback.message.answer("Не удалось переделать картинку.")


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

        chat_id = callback.message.chat.id
        async with show_typing(callback.bot, chat_id):
            await callback.message.answer("Пишу афишу…")
            post_text = await generate_weekly_poster(events)
        draft_id = await insert_draft("week_post", post_text, "draft")
        last_draft_state[user_id] = {"draft_id": draft_id, "events": events}
        logger.info("draft created after generate: id=%s", draft_id)
        await callback.message.answer(post_text, reply_markup=draft_actions_kb(draft_id))
    except Exception:
        logger.exception("week:generate failed")
        await callback.message.answer("Не удалось сгенерировать афишу.")


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
        "/events — собрать события недели (Event Radar)\n"
        "/daily — пост дня (одно главное событие ~24 ч)\n"
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
