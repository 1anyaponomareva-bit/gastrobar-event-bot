"""
Editorial formatters — посты Gastrobar по типу события.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from daily_display import format_event_schedule_line
from watchability import detect_editorial_type

log = logging.getLogger(__name__)

_FOOTER = "📍Океанус, улица с траками"


def _timing_hook(e: dict[str, Any]) -> str:
    timing = str(e.get("daily_timing_phrase", "Сегодня")).strip()
    if "ноч" in timing.lower():
        return "Сегодня ночью"
    if "завтра" in timing.lower():
        return "Завтра"
    return "Сегодня"


def _bar_lines() -> list[str]:
    return [
        "",
        "Смотрим на большом экране в Gastrobar.",
        "",
        "🍺 холодное пиво",
        "🥃 фирменные настойки",
        "",
        _FOOTER,
    ]


def _participants_line(e: dict[str, Any]) -> str | None:
    p = str(e.get("participants", "")).strip()
    if p and p != str(e.get("title", "")).strip():
        return p
    return None


def format_football_post(e: dict[str, Any]) -> str:
    em = str(e.get("emoji", "⚽")).strip() or "⚽"
    sched = format_event_schedule_line(e)
    title = str(e.get("title", "")).strip()
    sub = str(e.get("subtitle", e.get("league", ""))).strip()
    hook = _timing_hook(e)

    lines = [
        f"{hook} на большом экране — топ-футбол {em}",
        "",
        f"🕒 {sched}",
        title,
    ]
    pending = _participants_line(e)
    if pending == "участники уточняются":
        lines.append(pending)
    elif sub and sub.lower() != title.lower():
        lines.append(sub)
    lines.extend(_bar_lines())
    log.info("formatter used: format_football_post title=%r", title)
    return "\n".join(lines)


def format_nba_post(e: dict[str, Any]) -> str:
    from event_verifier import bar_event_blob

    em = str(e.get("emoji", "🏀")).strip() or "🏀"
    sched = format_event_schedule_line(e)
    title = str(e.get("title", "")).strip()
    sub = str(e.get("subtitle", e.get("league", ""))).strip()
    hook = _timing_hook(e)
    b = bar_event_blob(e)

    context = ""
    if re.search(r"western\s+conference\s+final", b):
        context = "Старт финала Западной конференции."
    elif re.search(r"eastern\s+conference\s+final", b):
        context = "Старт финала Восточной конференции."
    elif re.search(r"conference\s+final", b):
        context = "Финал конференции — серия на вылет."
    elif re.search(r"nba\s+finals|\bfinals\b", b):
        context = "Финал NBA — главная серия."
    elif "playoff" in b:
        context = "Плей-офф NBA — важная игра."

    lines = [
        f"{hook} включаем NBA {em}",
        "",
        f"🕒 {sched}",
        title,
    ]
    if sub and sub.lower() != title.lower():
        lines.append(sub)
    if context:
        lines.append("")
        lines.append(context)
        if re.search(r"game\s*1|первая\s+игра", b):
            lines.append("Первая игра серии.")
    lines.extend(_bar_lines())
    log.info("formatter used: format_nba_post title=%r", title)
    return "\n".join(lines)


def format_nhl_post(e: dict[str, Any]) -> str:
    em = str(e.get("emoji", "🏒")).strip() or "🏒"
    sched = format_event_schedule_line(e)
    title = str(e.get("title", "")).strip()
    sub = str(e.get("subtitle", e.get("league", ""))).strip()

    lines = [
        f"{_timing_hook(e)} — хоккей на экране {em}",
        "",
        f"🕒 {sched}",
        title,
    ]
    if sub and sub.lower() != title.lower():
        lines.append(sub)
    lines.extend(_bar_lines())
    log.info("formatter used: format_nhl_post title=%r", title)
    return "\n".join(lines)


def format_ufc_post(e: dict[str, Any]) -> str:
    em = str(e.get("emoji", "🥊")).strip() or "🥊"
    sched = format_event_schedule_line(e)
    title = str(e.get("title", "")).strip()
    note = str(e.get("ufc_main_note", "")).strip()

    lines = [
        f"{_timing_hook(e)} — UFC на большом экране {em}",
        "",
        f"🕒 {sched}",
        title,
    ]
    if note:
        lines.append(note)
    lines.extend(_bar_lines())
    log.info("formatter used: format_ufc_post title=%r", title)
    return "\n".join(lines)


def format_f1_post(e: dict[str, Any]) -> str:
    em = str(e.get("emoji", "🏎")).strip() or "🏎"
    sched = format_event_schedule_line(e)
    title = str(e.get("title", "")).strip()
    sub = str(e.get("subtitle", e.get("league", ""))).strip()

    lines = [
        f"Уик-энд Formula 1 в Gastrobar {em}",
        "",
        f"🕒 {sched}",
        title,
    ]
    if sub:
        lines.append(sub)
    lines.extend(_bar_lines())
    log.info("formatter used: format_f1_post title=%r", title)
    return "\n".join(lines)


def format_eurovision_post(e: dict[str, Any]) -> str:
    em = str(e.get("emoji", "🎤")).strip() or "🎤"
    sched = format_event_schedule_line(e)
    title = str(e.get("title", "")).strip()

    lines = [
        f"{_timing_hook(e)} — Eurovision в Gastrobar {em}",
        "",
        f"🕒 {sched}",
        title,
        "",
        "Включаем трансляцию на большом экране.",
        "Атмосфера как на арене — только ближе к бару.",
        "",
        _FOOTER,
    ]
    log.info("formatter used: format_eurovision_post title=%r", title)
    return "\n".join(lines)


def format_esports_post(e: dict[str, Any]) -> str:
    em = str(e.get("emoji", "🕹")).strip() or "🕹"
    sched = format_event_schedule_line(e)
    title = str(e.get("title", "")).strip()
    sub = str(e.get("subtitle", e.get("league", ""))).strip()

    lines = [
        f"{_timing_hook(e)} — киберспорт на экране {em}",
        "",
        f"🕒 {sched}",
        title,
    ]
    if sub:
        lines.append(sub)
    lines.extend(_bar_lines())
    log.info("formatter used: format_esports_post title=%r", title)
    return "\n".join(lines)


def format_generic_post(e: dict[str, Any]) -> str:
    from daily_display import format_single_daily_post_template

    text = format_single_daily_post_template(e)
    log.info("formatter used: format_generic_post title=%r", e.get("title"))
    return text


_FORMATTERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "football": format_football_post,
    "nba": format_nba_post,
    "nhl": format_nhl_post,
    "ufc": format_ufc_post,
    "f1": format_f1_post,
    "eurovision": format_eurovision_post,
    "esports": format_esports_post,
    "live": format_generic_post,
    "generic": format_generic_post,
}


def format_daily_post_for_event(e: dict[str, Any]) -> str:
    etype = str(e.get("editorial_type") or detect_editorial_type(e))
    fn = _FORMATTERS.get(etype, format_generic_post)
    return fn(e)


def format_daily_campaign_post(events: list[dict[str, Any]]) -> str:
    if len(events) == 1:
        return format_daily_post_for_event(events[0])
    if len(events) == 2:
        from daily_tv import format_dual_screen_daily_post

        return format_dual_screen_daily_post(events)
    parts = [format_daily_post_for_event(e) for e in events[:2]]
    return "\n\n—\n\n".join(parts)
