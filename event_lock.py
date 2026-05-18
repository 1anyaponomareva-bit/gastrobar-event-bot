"""
Immutable lock для финального списка событий перед форматированием.
Gemini (если используется) получает только locked payload и не может менять события.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

_TIME_OK_RE = re.compile(r"^(≈)?\d{1,2}:\d{2}$")


def has_confirmed_vn_time(e: dict[str, Any]) -> bool:
    """Только подтверждённые date + weekday + HH:MM (VN). Без «время уточняется»."""
    from locked_time import has_locked_schedule

    if not has_locked_schedule(e):
        return False
    date_s = str(e.get("local_date") or e.get("date", "")).strip()
    wd = str(e.get("local_weekday") or e.get("weekday", "")).strip()
    raw = str(e.get("local_time") or e.get("display_time") or e.get("time", "")).strip()
    if not date_s or not wd:
        return False
    if not raw or raw == "время уточняется":
        return False
    if str(e.get("time_precision", "")).lower() == "unknown":
        return False
    clean = raw.removeprefix("≈").strip()
    return bool(_TIME_OK_RE.match(raw) or _TIME_OK_RE.match(clean))


@dataclass(frozen=True)
class LockedEvent:
    title: str
    weekday: str
    display_time: str
    date: str
    category: str
    subtitle: str
    emoji: str
    participants: str
    lock_id: str
    utc_datetime: str = ""
    local_datetime: str = ""
    timezone: str = "Asia/Ho_Chi_Minh"

    def to_formatter_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "weekday": self.weekday,
            "display_time": self.display_time,
            "local_time": self.display_time,
            "date": self.date,
            "local_date": self.date,
            "category": self.category,
            "subtitle": self.subtitle,
            "league": self.subtitle,
            "emoji": self.emoji,
            "participants": self.participants,
            "lock_id": self.lock_id,
            "utc_datetime": self.utc_datetime,
            "local_datetime": self.local_datetime,
            "timezone": self.timezone,
            "time_locked": True,
            "schedule_locked": True,
            "_locked": True,
        }


def _lock_id_from_event(e: dict[str, Any]) -> str:
    return "|".join(
        (
            str(e.get("utc_datetime") or e.get("local_date") or e.get("date", "")).strip(),
            str(e.get("local_time") or e.get("display_time") or e.get("time", "")).strip(),
            str(e.get("title", "")).strip().lower()[:100],
        )
    )


def lock_events_for_formatter(
    events: list[dict[str, Any]],
    *,
    log_prefix: str = "weekly",
) -> list[LockedEvent]:
    locked: list[LockedEvent] = []
    for e in events:
        if str(e.get("afisha_kind", "")) == "parallel_block":
            for m in e.get("block_matches") or []:
                title = str(m).strip()
                if title:
                    le = LockedEvent(
                        title=title,
                        weekday=str(e.get("weekday", "")).strip(),
                        display_time=str(e.get("display_time", "")).strip(),
                        date=str(e.get("date", "")).strip(),
                        category="FOOTBALL",
                        subtitle=str(e.get("subtitle", e.get("league", ""))).strip(),
                        emoji="⚽",
                        participants=title,
                        lock_id=_lock_id_from_event(
                            {"title": title, "date": e.get("date"), "display_time": e.get("display_time")}
                        ),
                    )
                    locked.append(le)
            log.info(
                "%s LOCKED block: headline=%r matches=%s",
                log_prefix,
                e.get("block_headline"),
                len(e.get("block_matches") or []),
            )
            continue

        if not has_confirmed_vn_time(e):
            log.info(
                "%s skipped lock: title=%r reason=no_confirmed_time display=%r",
                log_prefix,
                e.get("title"),
                e.get("display_time"),
            )
            continue

        title = str(e.get("title", "")).strip()
        if not title:
            continue

        sched_tm = str(e.get("local_time") or e.get("time", "")).strip()
        sched_wd = str(e.get("local_weekday") or e.get("weekday", "")).strip()
        sched_date = str(e.get("local_date") or e.get("date", "")).strip()
        locked.append(
            LockedEvent(
                title=title,
                weekday=sched_wd,
                display_time=sched_tm,
                date=sched_date,
                category=str(e.get("category", "")).strip(),
                subtitle=str(e.get("subtitle", e.get("league", ""))).strip(),
                emoji=str(e.get("emoji", "🏟")).strip() or "🏟",
                participants=str(e.get("participants", "")).strip() or title,
                lock_id=_lock_id_from_event(e),
                utc_datetime=str(e.get("utc_datetime", "")).strip(),
                local_datetime=str(e.get("local_datetime", "")).strip(),
                timezone=str(e.get("timezone", "Asia/Ho_Chi_Minh")).strip(),
            )
        )

    log.info("%s LOCKED WEEKLY EVENTS: count=%s", log_prefix, len(locked))
    for le in locked:
        log.info(
            "  LOCKED: %s %s %s | %s",
            le.weekday,
            le.display_time,
            le.title,
            le.subtitle or le.category,
        )
    return locked


def locked_events_to_dicts(locked: list[LockedEvent]) -> list[dict[str, Any]]:
    return [le.to_formatter_dict() for le in locked]


def _normalize_match_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().strip())


def validate_formatter_output(
    output: str,
    locked: list[LockedEvent],
) -> tuple[bool, list[str]]:
    """Проверка: каждый locked title присутствует в тексте (не заменён summary)."""
    if not locked:
        return True, []
    blob = _normalize_match_text(output)
    missing: list[str] = []
    for le in locked:
        title_n = _normalize_match_text(le.title)
        if len(title_n) < 4:
            continue
        if title_n not in blob:
            # допускаем частичное совпадение для длинных title (команды)
            parts = [p.strip() for p in re.split(r"\s[—–-]\s|\bvs\.?\b", le.title, flags=re.I) if len(p.strip()) > 3]
            if parts and any(_normalize_match_text(p) in blob for p in parts):
                continue
            missing.append(le.title)
    ok = len(missing) == 0
    if not ok:
        log.error(
            "FORMATTER OUTPUT REJECTED: missing locked events: %s",
            missing,
        )
    else:
        log.info("FORMATTER OUTPUT VALIDATED: all %s locked events present", len(locked))
    return ok, missing


def format_locked_weekly_afisha(
    locked: list[LockedEvent],
    *,
    section_title: str = "🔥 НА ЭТОЙ НЕДЕЛЕ В GASTROBAR",
) -> str:
    """
    Чистое Python-форматирование locked списка (без Gemini).
    Группировка EPL matchday — только на этапе отображения, lock_id сохраняются.
    """
    from event_grouping import apply_grouping_for_weekly_display, format_parallel_block_lines
    from watchability import detect_editorial_type

    if not locked:
        return "Пока нет событий в подборке."

    events = locked_events_to_dicts(locked)
    display = apply_grouping_for_weekly_display(events)

    lines = [
        section_title,
        "",
        "Что реально стоит смотреть на экранах Gastrobar на этой неделе:",
        "",
    ]

    for e in display:
        if str(e.get("afisha_kind", "")) == "parallel_block":
            lines.extend(format_parallel_block_lines(e))
            lines.append("")
            continue

        em = str(e.get("emoji", "🏟")).strip()
        wd = str(e.get("local_weekday") or e.get("weekday", "")).strip()
        tm = str(e.get("local_time") or e.get("display_time", "")).strip()
        title = str(e.get("title", "")).strip()
        sub = str(e.get("subtitle", "")).strip()

        et = detect_editorial_type(e)
        if et == "nba":
            lines.append("🔥 NBA — главный эфир")
        elif et == "ufc":
            lines.append("🥊 UFC Main Card")
        elif et == "f1":
            lines.append("🏎 Formula 1")

        lines.append(f"{em} {wd} {tm}")
        lines.append(title)
        if sub and sub.lower() != title.lower():
            lines.append(sub)
        lines.append("")

    lines.append("📍Океанус, улица с траками")
    body = "\n".join(lines).strip()
    log.info("FORMATTER OUTPUT GENERATED (python-only) len=%s", len(body))
    return body
