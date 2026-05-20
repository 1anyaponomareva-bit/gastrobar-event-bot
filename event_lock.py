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
_GOLDEN_KNIGHT_FIX = re.compile(r"\bGolden Knight\b")


def _normalize_now24_match_title(title: str, *, now24: bool) -> str:
    """Исправление известных опечаток API в строке матча (Next24)."""
    if not now24 or not title:
        return title
    return _GOLDEN_KNIGHT_FIX.sub("Golden Knights", title)


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
    from radar_dedupe import dedupe_events, radar_dedupe_key

    events = dedupe_events(events, log_prefix=f"{log_prefix}_lock")
    locked: list[LockedEvent] = []
    seen_lock: set[tuple[str, str, str]] = set()
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
        title = _normalize_now24_match_title(title, now24=(log_prefix == "now24_afisha"))
        if not title:
            continue

        lk = radar_dedupe_key(e)
        if lk in seen_lock:
            log.info("%s skipped lock duplicate: title=%r", log_prefix, title)
            continue
        seen_lock.add(lk)

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
    now24: bool = False,
    chronological: bool = True,
) -> str:
    """
    Чистое Python-форматирование locked списка (без Gemini).
    По умолчанию — строго по времени; day_label = СЕГОДНЯ/ЗАВТРА/ПТ.
    """
    from event_grouping import apply_grouping_for_weekly_display, format_parallel_block_lines
    from event_radar_pipeline import enrich_events_for_display

    if not locked:
        return "Пока нет событий в подборке."

    events = locked_events_to_dicts(locked)
    events = enrich_events_for_display(events)
    if chronological:
        display = events
    else:
        display = apply_grouping_for_weekly_display(events, collapse_blocks=not now24)

    lines = [section_title, ""]

    for e in display:
        if str(e.get("afisha_kind", "")) == "parallel_block":
            lines.extend(format_parallel_block_lines(e))
            lines.append("")
            continue

        em = str(e.get("emoji", "🏟")).strip()
        wd = str(e.get("day_label") or e.get("local_weekday") or e.get("weekday", "")).strip()
        tm = str(e.get("local_time") or e.get("display_time", "")).strip()
        title = _normalize_now24_match_title(
            str(e.get("title", "")).strip(),
            now24=now24,
        )
        sub = str(e.get("subtitle", e.get("league", ""))).strip()

        lines.append(f"{em} {wd} {tm}")
        lines.append(title)
        if sub and sub.lower() not in title.lower():
            lines.append(sub)
        lines.append("")

    lines.append("📍Океанус, улица с траками")
    body = "\n".join(lines).strip()
    log.info("FORMATTER OUTPUT GENERATED (python-only) len=%s", len(body))
    return body
