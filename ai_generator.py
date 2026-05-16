"""Генерация текстов через Google Gemini (google-genai)."""

from __future__ import annotations

import asyncio
import json

from google import genai

from config import GEMINI_API_KEY, GEMINI_MODEL

from event_radar import format_radar_afisha


STYLE_SYSTEM = """Ты копирайтер бара Gastrobar. Пиши по-русски.
Стиль: коротко, живо, дерзко, но не пошло и не «колхозно». Без канцелярита.
Не нашпиговывай эмодзи — максимум одна-две уместные, или без них.
Акценты: спорт на экране, холодное пиво, настойки, вечер в баре.
Геолокация в конце поста: «📍Океанус, улица с траками» (как отдельная строка).
Не задавай вопросов читателю в стиле «о чём написать» — ты сам формулируешь готовый пост.
"""


def _client() -> genai.Client:
    return genai.Client(api_key=GEMINI_API_KEY)


def _generate_weekly_single_post_sync(events_json: str) -> str:
    prompt = f"""{STYLE_SYSTEM}

Ниже JSON со спортивными событиями недели для гостей бара. Напиши ОДИН общий пост-анонс на неделю.
Структура: 2–5 коротких абзацев или строк, динамично. Можно перечислить 2–3 самых жирных повода без сухого списка расписания.
События:
{events_json}
"""
    client = _client()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Пустой ответ Gemini")
    return text


def _generate_three_posts_sync(events_json: str) -> str:
    prompt = f"""{STYLE_SYSTEM}

Ниже JSON со спортивными событиями недели. Сгенерируй ТРИ варианта в ОДНОМ сообщении, чётко разделённые заголовками:

1) Заголовок: «Пост для Telegram»
   Очень короткий пост для канала/чата.

2) Заголовок: «Афишный пост»
   Пост со списком ключевых событий (компактно, читаемо).

3) Заголовок: «Продающий пост»
   Более дерзкий, цепляющий, с призывом заглянуть в бар.

События:
{events_json}
"""
    client = _client()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Пустой ответ Gemini")
    return text


def _generate_daily_spotlight_sync(events_json: str, context_line: str) -> str:
    prompt = f"""{STYLE_SYSTEM}

Контекст: {context_line}

События (JSON):
{events_json}

Напиши один короткий пост-напоминание на сегодня/завтра: почему стоит зайти в Gastrobar посмотреть это на экране.
"""
    client = _client()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Пустой ответ Gemini")
    return text


async def generate_weekly_single_post(events_json: str) -> str:
    return await asyncio.to_thread(_generate_weekly_single_post_sync, events_json)


async def generate_three_posts(events_json: str) -> str:
    return await asyncio.to_thread(_generate_three_posts_sync, events_json)


async def generate_daily_spotlight(events_json: str, context_line: str) -> str:
    return await asyncio.to_thread(
        _generate_daily_spotlight_sync, events_json, context_line
    )


def _generate_sports_program_poster_sync(program: list[dict]) -> str:
    """program — результат build_gastrobar_weekly_program (kind match | block). Резерв."""
    events_json = json.dumps(program[:6], ensure_ascii=False, indent=2)
    prompt = f"""Ты редактор контента спортивного гастробара Gastrobar. Пиши ТОЛЬКО готовый текст поста для Telegram, по-русски.

Вход — не сырой API, а УЖЕ СОБРАННАЯ программа редактора (JSON). Типы элементов:
- kind \"match\" — конкретный футбольный матч: title, league_label_ru, date, time. Раскрой коротко, почему это стоит смотреть в баре; сохрани пару команд и турнир.
- kind \"block\" — общий блок недели для NBA / NHL / UFC / Formula 1: emoji + line. НЕ выдумывай конкретные пары команд для этих видов спорта; это именно «NBA Playoffs», «Stanley Cup Playoffs», «UFC Fight Night», «Formula 1 Grand Prix» как повод включить экран.

Обязательная структура близка к:
- заголовок «🔥 ГЛАВНОЕ НА НЕДЕЛЕ» (можно слегка варьировать формулировку);
- дальше строки по программе: для match — эмодзи ⚽, название матча, строка с турниром по-русски, строка «День недели ЧЧ:ММ» (из date/time);
- для block — строка с emoji и названием блока как в JSON (одна-две строки на блок, без выдуманных матчей);
- коротко: главное на экранах в Gastrobar;
- заверши отдельными строками:
  🍺 холодное пиво
  🥃 фирменные настойки
  📍Океанус, улица с траками

ЗАПРЕЩЕНО:
- копировать поля JSON как есть и сырые английские названия API;
- добавлять матчи или виды спорта, которых нет в программе;
- для NBA/NHL/UFC/F1 перечислять вымышленные «Team A — Team B».

Максимум ~900 символов. Без хэштегов. Без вопросов читателю.

Программа (JSON):
{events_json}
"""
    client = _client()
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Пустой ответ Gemini")
    if len(text) > 900:
        text = text[:897].rstrip() + "..."
    return text


async def generate_weekly_poster(events: list[dict]) -> str:
    """Event Radar: одна готовая афиша из данных без второго длинного Gemini-текста."""
    if not events:
        return "Нет данных для афиши."
    first = events[0]
    if not isinstance(first, dict):
        return await asyncio.to_thread(_generate_sports_program_poster_sync, events)
    if first.get("kind") in ("match", "block"):
        return await asyncio.to_thread(_generate_sports_program_poster_sync, events)
    # Event Radar: те же словари, что в /events — не проверяем только events[0] и поле time:
    # иначе ложно срабатывало «устарела / нет данных», хотя афиша только что собралась.
    return format_radar_afisha(events)


async def generate_week_post(events: list[dict]) -> str:
    """Совместимость со старым именем."""
    return await generate_weekly_poster(events)


def _generate_daily_event_post_sync(event: dict) -> str:
    event_json = json.dumps(event, ensure_ascii=False, indent=2)
    timing = str(event.get("daily_timing_phrase", "скоро")).strip()
    display_time = str(
        event.get("display_time") or event.get("time_display") or event.get("time", "")
    ).strip()
    note = str(event.get("note", "")).strip()
    ufc_note = str(event.get("ufc_main_note", "")).strip()
    em = str(event.get("emoji", "🏟")).strip()

    prompt = f"""{STYLE_SYSTEM}

Напиши ОДИН короткий пост для Telegram — «пост дня» Gastrobar про ОДНО конкретное событие.
Это НЕ недельная афиша, а кампания на ближайший эфир.

Тон: атмосферный, приглашающий, без канцелярита, без вопросов читателю.
Длина: 300–700 символов, не больше.

Структура (гибко):
- цепляющий заход с эмодзи {em} и событием ({timing});
- 1–2 предложения — что смотрим на большом экране, название боя/матча из JSON;
- блок из 2–4 коротких строк про бар (пиво, настойки, атмосфера — своими словами);
- если в JSON есть note «показываем с открытия» — упомяни начало с {display_time} / открытия;
- если есть ufc_main_note — не указывай точное время главного боя, только main card;
- заверши строкой «📍Океанус, улица с траками».

Не перечисляй другие события недели. Без хэштегов.

Событие (JSON):
{event_json}
"""
    client = _client()
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Пустой ответ Gemini (daily post)")
    if len(text) > 700:
        text = text[:697].rstrip() + "..."
    if "Океанус" not in text:
        text = text.rstrip() + "\n\n📍Океанус, улица с траками"
    return text


async def generate_daily_event_post(event: dict) -> str:
    return await asyncio.to_thread(_generate_daily_event_post_sync, event)
