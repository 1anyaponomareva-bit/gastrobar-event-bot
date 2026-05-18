"""Форматирование времени и превью для Daily Post."""

from __future__ import annotations

from typing import Any

from config import TIMEZONE


def format_event_schedule_line(e: dict[str, Any]) -> str:
    """ВТ 07:30 — для поста и служебного сообщения (locked local time only)."""
    wd = str(e.get("local_weekday") or e.get("weekday", "")).strip().upper()
    tm = str(e.get("local_time") or e.get("display_time") or e.get("time", "")).strip()
    if wd and tm:
        return f"{wd} {tm}"
    return tm or wd or "время уточняется"


def format_daily_status_message(
    events: list[dict[str, Any]],
    *,
    verified_ok: bool,
    time_ok: bool,
    image_missing: bool,
    image_source: str,
) -> str:
    if not events:
        return "⚠️ Пост дня · нет событий для публикации."

    e = events[0]
    em = str(e.get("emoji", "🏟")).strip() or "🏟"
    sched = format_event_schedule_line(e)
    title = str(e.get("title", "")).strip()
    sub = str(e.get("subtitle", e.get("league", ""))).strip()

    lines = [
        "✅ Пост дня · Gastrobar",
        "",
        "Событие:" if len(events) == 1 else f"События ({len(events)}):",
        f"{em} {sched}",
        title,
    ]
    if sub and sub.lower() != title.lower():
        lines.append(sub)

    if len(events) > 1:
        for extra in events[1:3]:
            lines.append("")
            em2 = str(extra.get("emoji", "🏟")).strip() or "🏟"
            lines.append(f"{em2} {format_event_schedule_line(extra)}")
            lines.append(str(extra.get("title", "")).strip())

    lines.append("")
    lines.append(
        f"{'✅' if verified_ok else '⚠️'} событие подтверждено"
    )
    if time_ok and sched != "время уточняется":
        lines.append(f"✅ время подтверждено: {sched} {TIMEZONE}")
    else:
        lines.append(f"⚠️ время ({TIMEZONE}): уточняется")
    if image_missing:
        lines.append("⚠️ картинку не нашёл, отправляю только текст.")
    else:
        lines.append(f"✅ картинка: {image_source}")
    return "\n".join(lines)


def format_single_daily_post_template(event: dict[str, Any]) -> str:
    """Готовый пост с обязательным временем (без Gemini)."""
    em = str(event.get("emoji", "🏟")).strip() or "🏟"
    sched = format_event_schedule_line(event)
    title = str(event.get("title", "")).strip()
    sub = str(event.get("subtitle", event.get("league", ""))).strip()
    timing = str(event.get("daily_timing_phrase", "Сегодня")).strip()

    hook = timing
    if "ноч" in timing.lower():
        hook = "Сегодня ночью"
    elif timing in ("сегодня", "уже этой ночью"):
        hook = "Сегодня"
    elif "завтра" in timing.lower():
        hook = "Завтра"

    lines = [
        f"{hook} включаем {em}",
        "",
        f"🕒 {sched}",
        title,
    ]
    if sub and sub.lower() != title.lower():
        lines.append(sub)
    lines.extend(
        [
            "",
            "Смотрим на экране в Gastrobar.",
            "Пиво холодное, настойки заряжены.",
            "",
            "📍Океанус, улица с траками",
        ]
    )
    return "\n".join(lines)


def post_text_includes_schedule(post_text: str, event: dict[str, Any]) -> bool:
    sched = format_event_schedule_line(event)
    tm = str(
        event.get("display_time") or event.get("time_display") or event.get("time", "")
    ).strip()
    wd = str(event.get("weekday", "")).strip().upper()
    blob = post_text.lower()
    if sched.lower() in blob:
        return True
    if tm and tm in post_text:
        return True
    if wd and wd.lower() in blob and tm:
        return True
    return False
