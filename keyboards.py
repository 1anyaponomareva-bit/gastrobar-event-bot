"""Inline-клавиатуры."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def week_actions_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Сгенерировать пост",
                    callback_data="week:gen_one",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Сгенерировать 3 поста",
                    callback_data="week:gen_three",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Обновить события",
                    callback_data="week:refresh",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data="week:cancel",
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


def daily_alert_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔥 Сделать пост дня",
                    callback_data="daily:post",
                ),
                InlineKeyboardButton(
                    text="Пропустить",
                    callback_data="daily:skip",
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
