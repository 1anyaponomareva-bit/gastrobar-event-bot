"""
Daily Content Generator — готовый пост для Telegram Gastrobar.
Weekly cache = source of truth; fresh search только как fallback.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile, FSInputFile

from ai_generator import generate_daily_campaign_post, generate_daily_event_post
from config import TIMEZONE
from daily_display import (
    format_daily_status_message,
    format_single_daily_post_template,
    post_text_includes_schedule,
)
from daily_event import (
    format_upcoming_preview_message,
    select_nearest_upcoming,
    select_now24_events,
)
from database import (
    insert_draft,
    record_scheduled_post,
    save_radar_snapshot,
    upsert_draft_asset,
)
from event_radar import get_event_radar_now24
from event_verifier import verify_event
from image_finder import find_event_image
from weekly_events_cache import (
    get_weekly_events_cache,
    merge_events_into_weekly_cache,
)

log = logging.getLogger(__name__)

CACHE_EMPTY_MSG = (
    "Недельная афиша ещё не собрана. Сначала соберите /events → Афиша на неделю, "
    "или я сделаю быстрый поиск ближайших 24 часов."
)


@dataclass
class DailyContentPackage:
    events: list[dict[str, Any]]
    post_text: str
    image_bytes: bytes | None
    image_path: str | None
    image_source: str
    draft_id: int
    verified_ok: bool = True
    time_ok: bool = True
    image_missing: bool = False


@dataclass
class DailyBuildResult:
    ok: bool
    package: DailyContentPackage | None = None
    error_code: str = ""
    error_detail: str = ""


def normalize_event_for_daily(e: dict[str, Any]) -> dict[str, Any]:
    out = dict(e)
    out["title"] = str(out.get("title", "")).strip() or "Событие"
    out["display_time"] = str(
        out.get("display_time")
        or out.get("local_time")
        or out.get("time_display")
        or out.get("time", "")
    ).strip()
    out["emoji"] = str(out.get("emoji", "🏟")).strip() or "🏟"
    out["subtitle"] = str(out.get("subtitle", out.get("league", ""))).strip()
    out.setdefault("daily_timing_phrase", "скоро")
    out.setdefault("date", str(out.get("local_date") or out.get("date", "")))
    out.setdefault("weekday", str(out.get("local_weekday") or out.get("weekday", "")))
    return out


async def collect_daily_candidates_from_weekly_cache() -> list[dict[str, Any]]:
    cached = await get_weekly_events_cache()
    log.info("daily using weekly cache: %s events", len(cached))
    candidates = select_now24_events(cached)
    log.info("daily next24 candidates from cache: %s", len(candidates))
    return candidates


async def collect_daily_candidates_fresh() -> tuple[list[dict[str, Any]], str | None]:
    log.info("daily fresh search fallback started")
    events, raw_total, _, _, fetch_note = await get_event_radar_now24()
    log.info(
        "daily fresh search result: selected=%s raw_total=%s note=%s",
        len(events),
        raw_total,
        fetch_note,
    )
    if events:
        await merge_events_into_weekly_cache(events, source="daily_fresh_search")
    return events, fetch_note


async def resolve_daily_events(
    events: list[dict[str, Any]] | None = None,
    *,
    force_fresh_fallback: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """
    Источник событий для поста дня.
    Возвращает (events, source_tag).
    source_tag: provided | weekly_cache | fresh_fallback | cache_empty
    """
    if events is not None:
        log.info("daily using provided events: %s", len(events))
        return [normalize_event_for_daily(e) for e in events], "provided"

    cached = await get_weekly_events_cache()
    if not cached and not force_fresh_fallback:
        log.info("daily: weekly cache empty")
        return [], "cache_empty"

    candidates = await collect_daily_candidates_from_weekly_cache()
    if candidates:
        return candidates, "weekly_cache"

    if not force_fresh_fallback:
        if not cached:
            return [], "cache_empty"
        upcoming = select_nearest_upcoming(cached)
        if upcoming:
            log.info("daily: using nearest upcoming (no events in 24h)")
            return upcoming, "upcoming_preview"
        log.info("daily: no events in 24h window from weekly cache")
        return [], "no_events_in_cache"

    fresh, _ = await collect_daily_candidates_fresh()
    if fresh:
        return fresh, "fresh_fallback"
    cached_after = await get_weekly_events_cache()
    upcoming = select_nearest_upcoming(cached_after)
    if upcoming:
        log.info("daily: nearest upcoming after fresh search empty")
        return upcoming, "upcoming_preview"
    return [], "no_events"


async def verify_events_for_daily(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool, bool]:
    from event_time import apply_event_datetime, has_locked_datetime, reconcile_event_datetime

    if not events:
        return [], False, False

    verified: list[dict[str, Any]] = []
    time_ok = True
    for e in events:
        title = e.get("title")
        cached = dict(e)
        if has_locked_datetime(cached):
            locked = apply_event_datetime(cached)
            if locked:
                verified.append(normalize_event_for_daily(locked))
                log.info(
                    "daily verification: title=%r using weekly locked time utc=%s local=%s",
                    title,
                    locked.get("utc_datetime"),
                    locked.get("local_datetime"),
                )
                continue
        try:
            r = await verify_event(e)
        except Exception:
            log.exception("daily verify failed for %r", title)
            r = None
        if r:
            r = reconcile_event_datetime(cached, r)
        if r and str(r.get("confidence", "medium")).lower() in ("high", "medium"):
            verified.append(normalize_event_for_daily(r))
            log.info(
                "daily verification result: title=%r confidence=%s via=%s utc=%s",
                title,
                r.get("confidence"),
                r.get("verified_via"),
                r.get("utc_datetime"),
            )
        elif r:
            r = normalize_event_for_daily(r)
            r["confidence"] = "medium"
            verified.append(r)
            log.info("daily verification result: title=%r confidence=medium (soft)", title)
        else:
            fallback = apply_event_datetime(cached) or cached
            fallback = normalize_event_for_daily(fallback)
            fallback["confidence"] = "medium"
            fallback["verification_reason"] = "daily_pass_through"
            verified.append(fallback)
            time_ok = False
            log.warning("daily verification pass-through: title=%r", title)

    if not verified:
        verified = [normalize_event_for_daily(e) for e in events]
        time_ok = False

    verified_ok = all(
        str(v.get("confidence", "medium")).lower() in ("high", "medium")
        for v in verified
    )
    return verified, verified_ok, time_ok


async def _generate_post_text(post_events: list[dict[str, Any]]) -> str:
    from event_formatter import format_daily_campaign_post, format_daily_post_for_event
    from watchability import enrich_watchability

    enriched = [enrich_watchability(e) for e in post_events]

    if len(enriched) == 1:
        ev = enriched[0]
        try:
            post_text = await generate_daily_event_post(ev)
            if not post_text_includes_schedule(post_text, ev):
                raise ValueError("missing schedule in gemini post")
            log.info(
                "daily post formatter: gemini+editorial type=%s",
                ev.get("editorial_type"),
            )
            return post_text
        except Exception:
            log.warning(
                "daily: using editorial formatter type=%s title=%r",
                ev.get("editorial_type"),
                ev.get("title"),
            )
            return format_daily_post_for_event(ev)

    editorial = format_daily_campaign_post(enriched)
    log.info("daily post formatter: editorial_campaign events=%s", len(enriched))
    return editorial


async def build_daily_content_package(
    events: list[dict[str, Any]] | None = None,
    *,
    log_prefix: str = "daily",
    force_fresh_fallback: bool = False,
) -> DailyBuildResult:
    log.info("%s started", log_prefix)

    try:
        now24, source_tag = await resolve_daily_events(
            events,
            force_fresh_fallback=force_fresh_fallback,
        )

        if source_tag == "cache_empty":
            return DailyBuildResult(
                ok=False,
                error_code="cache_empty",
                error_detail=CACHE_EMPTY_MSG,
            )

        if not now24:
            log.info("%s: no events in 24h window (source=%s)", log_prefix, source_tag)
            return DailyBuildResult(
                ok=False,
                error_code="no_events",
                error_detail=(
                    "Крупных событий для Gastrobar в ближайшие 24 часа не найдено."
                ),
            )

        is_upcoming_preview = source_tag == "upcoming_preview"
        if is_upcoming_preview:
            preview_msg = format_upcoming_preview_message(now24)
            log.info("%s: upcoming preview mode for %r", log_prefix, now24[0].get("title"))

        for e in now24:
            log.info(
                "daily selected event: title=%r source=%s display_time=%s",
                e.get("title"),
                source_tag,
                e.get("display_time"),
            )

        verified, verified_ok, time_ok = await verify_events_for_daily(now24)
        from daily_tv import apply_tv_limit_for_digest

        verified, skipped_tv = apply_tv_limit_for_digest(verified)
        for e in skipped_tv:
            log.info(
                "daily skipped event: title=%r reason=skipped_due_to_tv_limit",
                e.get("title"),
            )
        if skipped_tv:
            log.info(
                "%s tv_limit: kept=%s skipped=%s",
                log_prefix,
                len(verified),
                len(skipped_tv),
            )
        if not verified:
            log.error("%s verification failed: empty after verify", log_prefix)
            return DailyBuildResult(
                ok=False,
                error_code="verification_failed",
                error_detail="Не удалось подтвердить события.",
            )
        log.info(
            "%s verification summary: ok=%s time_ok=%s count=%s source=%s",
            log_prefix,
            verified_ok,
            time_ok,
            len(verified),
            source_tag,
        )

        post_events = verified if len(verified) > 1 else [verified[0]]
        log.info("%s generate text started (events=%s)", log_prefix, len(post_events))
        try:
            if is_upcoming_preview:
                post_text = preview_msg + "\n\n---\n\n" + await _generate_post_text(
                    post_events
                )
            else:
                post_text = await _generate_post_text(post_events)
            if not (post_text or "").strip():
                raise RuntimeError("empty post text")
            log.info("%s generate text result: ok len=%s", log_prefix, len(post_text))
        except Exception as e:
            log.error("daily post generation failed", exc_info=True)
            if len(post_events) == 1:
                post_text = format_single_daily_post_template(post_events[0])
            else:
                return DailyBuildResult(
                    ok=False,
                    error_code="gemini_text_failed",
                    error_detail=str(e)[:200] or "Gemini text generation failed",
                )

        draft_id = await insert_draft("daily_campaign", post_text, "draft")
        primary = verified[0]

        image_bytes: bytes | None = None
        image_path: str | None = None
        image_source = "none"
        image_missing = False

        log.info("%s image search started for %r", log_prefix, primary.get("title"))
        try:
            image_bytes, image_source, image_path = await find_event_image(
                primary, draft_id=draft_id
            )
            log.info(
                "%s image result: source=%s path=%s bytes=%s",
                log_prefix,
                image_source,
                bool(image_path),
                bool(image_bytes),
            )
        except Exception:
            log.error("%s image search failed", log_prefix, exc_info=True)
            image_missing = True
            image_source = "error"

        if image_source in ("none", "error") or (not image_path and not image_bytes):
            image_missing = True
            log.warning("%s: no image, continuing with text only", log_prefix)

        await upsert_draft_asset(
            draft_id,
            image_path=image_path or "",
            event_json=json.dumps(verified, ensure_ascii=False),
            poster_source=image_source,
        )
        try:
            await record_scheduled_post(
                campaign_date=date.today().isoformat(),
                events_json=json.dumps(verified, ensure_ascii=False),
                draft_id=draft_id,
                status="ready",
            )
            await save_radar_snapshot(
                "now24",
                verified,
                {"source": log_prefix, "weekly_source": source_tag},
            )
        except Exception:
            log.exception("%s: db snapshot failed (non-fatal)", log_prefix)

        return DailyBuildResult(
            ok=True,
            package=DailyContentPackage(
                events=verified,
                post_text=post_text,
                image_bytes=image_bytes,
                image_path=image_path,
                image_source=image_source,
                draft_id=draft_id,
                verified_ok=verified_ok,
                time_ok=time_ok,
                image_missing=image_missing,
            ),
        )
    except Exception as e:
        log.error("%s unexpected error", log_prefix, exc_info=True)
        return DailyBuildResult(
            ok=False,
            error_code="unexpected",
            error_detail=str(e)[:200] or "unexpected error",
        )


_USER_ERROR_RU = {
    "no_events": "Крупных событий для Gastrobar в ближайшие 24 часа не найдено.",
    "cache_empty": CACHE_EMPTY_MSG,
    "verification_failed": "Ошибка: verification failed",
    "gemini_text_failed": "Ошибка: Gemini text generation failed",
    "unexpected": "Ошибка: unexpected error",
}


def user_error_message(result: DailyBuildResult) -> str:
    if result.error_code in _USER_ERROR_RU:
        return _USER_ERROR_RU[result.error_code]
    if result.error_code == "gemini_text_failed":
        return f"Ошибка: Gemini text generation failed\n({result.error_detail[:120]})"
    if result.error_code == "unexpected":
        return f"Ошибка: unexpected error\n({result.error_detail[:120]})"
    return result.error_detail or "Не удалось сгенерировать пост дня."


async def deliver_daily_content(
    bot: Bot,
    pkg: DailyContentPackage,
    *,
    chat_id: int | None = None,
) -> None:
    from config import ADMIN_ID
    from keyboards import daily_preview_kb

    target = chat_id or ADMIN_ID
    if not target:
        log.warning("No chat_id for daily delivery")
        return

    status = format_daily_status_message(
        pkg.events,
        verified_ok=pkg.verified_ok,
        time_ok=pkg.time_ok,
        image_missing=pkg.image_missing,
        image_source=pkg.image_source,
    )
    await bot.send_message(target, status)

    kb = daily_preview_kb(pkg.draft_id)
    caption = pkg.post_text
    if pkg.image_path and Path(pkg.image_path).is_file():
        await bot.send_photo(
            target,
            photo=FSInputFile(pkg.image_path),
            caption=caption,
            reply_markup=kb,
        )
    elif pkg.image_bytes:
        await bot.send_photo(
            target,
            photo=BufferedInputFile(pkg.image_bytes, filename="daily_post.png"),
            caption=caption,
            reply_markup=kb,
        )
    else:
        await bot.send_message(target, caption, reply_markup=kb)


async def deliver_daily_content_to_admin(
    bot: Bot,
    pkg: DailyContentPackage,
    *,
    chat_id: int | None = None,
) -> None:
    await deliver_daily_content(bot, pkg, chat_id=chat_id)


async def run_scheduled_daily_content(bot: Bot) -> None:
    log.info("scheduled daily content generator started")
    result = await build_daily_content_package(log_prefix="scheduled_daily")
    if result.error_code == "cache_empty":
        log.info("scheduled daily: empty weekly cache, trying fresh fallback")
        result = await build_daily_content_package(
            log_prefix="scheduled_daily",
            force_fresh_fallback=True,
        )
    if not result.ok or not result.package:
        log.info("scheduled daily: skip reason=%s", result.error_code)
        return
    await deliver_daily_content_to_admin(bot, result.package)
