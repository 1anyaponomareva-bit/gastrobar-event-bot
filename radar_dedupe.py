"""Дедупликация событий Event Radar (title + datetime + category)."""

from __future__ import annotations

import re
from typing import Any

_VS_SPLIT = re.compile(r"\s[—–-]\s|\bvs\.?\b", re.I)


def normalize_event_title(title: str) -> str:
    """A vs B и B vs A → одна ключевая строка."""
    t = re.sub(r"\s+", " ", (title or "").lower().strip())
    parts = [p.strip() for p in _VS_SPLIT.split(t) if p.strip()]
    if len(parts) >= 2:
        parts.sort()
        return " vs ".join(parts)
    return t


def radar_dedupe_key(e: dict[str, Any], *, exact: bool = False) -> tuple[str, str, str]:
    raw_title = str(e.get("title", "")).strip().lower()
    title = raw_title if exact else normalize_event_title(raw_title)
    utc = str(e.get("utc_datetime", "")).strip()
    if utc:
        dt_key = utc
    else:
        dt_key = "|".join(
            (
                str(e.get("local_date") or e.get("date", "")).strip(),
                str(
                    e.get("local_time")
                    or e.get("display_time")
                    or e.get("time", "")
                ).strip(),
            )
        )
    cat = str(e.get("category", "")).strip().upper()
    if not cat:
        from watchability import detect_editorial_type

        cat = detect_editorial_type(e).upper()
    return (title, dt_key, cat)


def dedupe_events(
    events: list[dict[str, Any]],
    *,
    log_prefix: str = "",
    exact: bool = False,
) -> list[dict[str, Any]]:
    import logging

    log = logging.getLogger(__name__)
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for e in events:
        k = radar_dedupe_key(e, exact=exact)
        if k in seen:
            if log_prefix:
                log.info(
                    "%s dedupe drop: title=%r dt=%s cat=%s",
                    log_prefix,
                    e.get("title"),
                    k[1],
                    k[2],
                )
            continue
        seen.add(k)
        out.append(e)
    return out
