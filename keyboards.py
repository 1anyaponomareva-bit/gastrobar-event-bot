"""Inline-клавиатуры Event Radar и постов."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def radar_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📅 Афиша на неделю",
                    callback_data="radar:week",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚡ События сейчас / 24 часа",
                    callback_data="radar:now24",
                ),
            ],
        ]
    )


def radar_week_result_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Сгенерировать пост",
                    callback_data="radar:week:gen",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Обновить",
                    callback_data="radar:week",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Закрыть",
                    callback_data="radar:close",
                ),
            ],
        ]
    )


def radar_now24_result_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Сгенерировать пост на сегодня",
                    callback_data="radar:now24:gen",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Обновить",
                    callback_data="radar:now24",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Закрыть",
                    callback_data="radar:close",
                ),
            ],
        ]
    )


def post_result_kb(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Опубликовать",
                    callback_data=f"draft:pub:{draft_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Переделать",
                    callback_data=f"draft:redo:{draft_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=f"draft:cancel:{draft_id}",
                ),
            ],
        ]
    )


def daily_post_kb(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Опубликовать",
                    callback_data=f"daily:pub:{draft_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Переделать текст",
                    callback_data=f"daily:redo_text:{draft_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🖼 Переделать картинку",
                    callback_data=f"daily:redo_img:{draft_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"daily:cancel:{draft_id}",
                ),
            ],
        ]
    )
