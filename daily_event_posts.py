"""
Daily Content Generator — готовый пост для Telegram Gastrobar.
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
from config import ADMIN_ID, TIMEZONE
from daily_event import select_now24_events
from database import (
    insert_draft,
    record_scheduled_post,
    save_radar_snapshot,
    upsert_draft_asset,
)
from event_radar import get_event_radar_now24
from event_verifier import verify_event
from image_finder import find_event_image

log = logging.getLogger(__name__)


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
    """Минимальный формат для Gemini daily post."""
    out = dict(e)
    out["title"] = str(out.get("title", "")).strip() or "Событие"
    out["display_time"] = str(
        out.get("display_time") or out.get("time_display") or out.get("time", "")
    ).strip() or "время уточняется"
    out["emoji"] = str(out.get("emoji", "🏟")).strip() or "🏟"
    out["subtitle"] = str(out.get("subtitle", out.get("league", ""))).strip()
    out.setdefault("daily_timing_phrase", "скоро")
    out.setdefault("date", str(out.get("date", "")))
    out.setdefault("weekday", str(out.get("weekday", "")))
    return out


async def fetch_now24_events_for_daily() -> tuple[list[dict[str, Any]], int, str | None]:
    """События ближайших 24 ч (тот же путь, что radar:now24)."""
    events, raw_total, _, _, fetch_note = await get_event_radar_now24()
    log.info("daily now24 fetch: selected=%s raw_total=%s note=%s", len(events), raw_total, fetch_note)
    return events, raw_total, fetch_note


async def verify_events_for_daily(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool, bool]:
    if not events:
        return [], False, False

    verified: list[dict[str, Any]] = []
    time_ok = True
    for e in events:
        title = e.get("title")
        try:
            r = await verify_event(e)
        except Exception:
            log.exception("daily verify failed for %r", title)
            r = None
        if r and str(r.get("confidence", "medium")).lower() in ("high", "medium"):
            verified.append(normalize_event_for_daily(r))
            log.info(
                "daily verification result: title=%r confidence=%s via=%s",
                title,
                r.get("confidence"),
                r.get("verified_via"),
            )
        elif r:
            r = normalize_event_for_daily(r)
            r["confidence"] = "medium"
            verified.append(r)
            log.info("daily verification result: title=%r confidence=medium (soft)", title)
        else:
            fallback = normalize_event_for_daily(e)
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


async def build_daily_content_package(
    events: list[dict[str, Any]] | None = None,
    *,
    log_prefix: str = "daily",
) -> DailyBuildResult:
    log.info("%s started", log_prefix)

    try:
        if events is not None:
            now24 = [normalize_event_for_daily(e) for e in events]
            log.info("%s using provided events: %s", log_prefix, len(now24))
        else:
            now24, raw_total, fetch_note = await fetch_now24_events_for_daily()
            log.info(
                "%s now24 events found: %s (raw_total=%s note=%s)",
                log_prefix,
                len(now24),
                raw_total,
                fetch_note,
            )

        if not now24:
            log.info("%s: no events in 24h window", log_prefix)
            return DailyBuildResult(
                ok=False,
                error_code="no_events",
                error_detail="Крупных событий в ближайшие 24 часа нет.",
            )

        primary = now24[0]
        log.info(
            "%s selected event for daily post: %r (priority list head, total=%s)",
            log_prefix,
            primary.get("title"),
            len(now24),
        )

        verified, verified_ok, time_ok = await verify_events_for_daily(now24)
        from daily_tv import apply_tv_limit_for_digest

        verified, skipped_tv = apply_tv_limit_for_digest(verified)
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
            "%s verification summary: ok=%s time_ok=%s count=%s",
            log_prefix,
            verified_ok,
            time_ok,
            len(verified),
        )

        post_events = verified if len(verified) > 1 else [verified[0]]
        log.info("%s generate text started (events=%s)", log_prefix, len(post_events))
        try:
            if len(post_events) == 1:
                post_text = await generate_daily_event_post(post_events[0])
            elif len(post_events) == 2:
                from daily_tv import format_dual_screen_daily_post

                post_text = format_dual_screen_daily_post(post_events)
            else:
                post_text = await generate_daily_campaign_post(post_events)
            if not (post_text or "").strip():
                raise RuntimeError("empty post text")
            log.info("%s generate text result: ok len=%s", log_prefix, len(post_text))
        except Exception as e:
            log.error("daily post generation failed", exc_info=True)
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
        except Exception as e:
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
            await save_radar_snapshot("now24", verified, {"source": log_prefix})
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
    "no_events": "Крупных событий в ближайшие 24 часа нет.",
    "verification_failed": "Ошибка: verification failed",
    "gemini_text_failed": "Ошибка: Gemini text generation failed",
    "unexpected": "Ошибка: unexpected error",
}


def user_error_message(result: DailyBuildResult) -> str:
    if result.error_code == "gemini_text_failed":
        return f"Ошибка: Gemini text generation failed\n({result.error_detail[:120]})"
    if result.error_code == "verification_failed":
        return "Ошибка: verification failed"
    if result.error_code == "no_events":
        return _USER_ERROR_RU["no_events"]
    if result.error_code == "unexpected":
        return f"Ошибка: unexpected error\n({result.error_detail[:120]})"
    return result.error_detail or "Не удалось сгенерировать пост дня."


async def deliver_daily_content(
    bot: Bot,
    pkg: DailyContentPackage,
    *,
    chat_id: int | None = None,
) -> None:
    from keyboards import daily_preview_kb

    target = chat_id or ADMIN_ID
    if not target:
        log.warning("No chat_id for daily delivery")
        return

    titles = ", ".join(str(e.get("title", ""))[:40] for e in pkg.events[:3])
    status = (
        "✅ Пост дня · Gastrobar\n\n"
        f"События: {titles}\n"
        f"{'✅' if pkg.verified_ok else '⚠️'} событие подтверждено\n"
        f"{'✅' if pkg.time_ok else '⚠️'} время ({TIMEZONE})\n"
    )
    if pkg.image_missing:
        status += "⚠️ Картинку не нашёл, отправляю только текст.\n"
    else:
        status += f"✅ картинка: {pkg.image_source}\n"
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


# Совместимость со scheduler
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
    if not result.ok or not result.package:
        log.info("scheduled daily: skip reason=%s", result.error_code)
        return
    await deliver_daily_content_to_admin(bot, result.package)
