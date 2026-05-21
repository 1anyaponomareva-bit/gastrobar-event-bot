"""Inline-клавиатуры Event Radar и постов."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def radar_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔥 Афиша на 3 дня",
                    callback_data="radar:next72",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚡ Ближайшие 24 часа",
                    callback_data="radar:now24",
                ),
            ],
        ]
    )


def radar_next72_result_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Сгенерировать пост",
                    callback_data="radar:post_next72",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Обновить 3 дня",
                    callback_data="radar:next72:force",
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


def radar_week_result_kb() -> InlineKeyboardMarkup:
    """Legacy alias."""
    return radar_next72_result_kb()


def radar_now24_result_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Сгенерировать пост на сегодня",
                    callback_data="radar:post_now24",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Обновить 24 ч",
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


def daily_preview_kb(draft_id: int) -> InlineKeyboardMarkup:
    """Превью поста дня: опубликовать / переделать / отмена."""
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
                    text="🔄 Переделать",
                    callback_data=f"daily:redo:{draft_id}",
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


def daily_post_kb(draft_id: int) -> InlineKeyboardMarkup:
    return daily_preview_kb(draft_id)
