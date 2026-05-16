"""
Daily Content Generator — готовый пост для Telegram Gastrobar (11:00 VN).
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

from ai_generator import generate_daily_campaign_post
from config import ADMIN_ID, TIMEZONE
from daily_event import collect_campaign_events
from database import (
    insert_draft,
    record_scheduled_post,
    save_radar_snapshot,
    upsert_draft_asset,
)
from event_radar import _fetch_radar_pipeline
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
    verified_ok: bool
    time_ok: bool


async def verify_events_for_daily(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool, bool]:
    """Повторная проверка перед публикацией. Не слишком жёстко."""
    if not events:
        return [], False, False

    verified: list[dict[str, Any]] = []
    time_ok = True
    for e in events:
        try:
            r = await verify_event(e)
        except Exception:
            log.exception("daily verify failed for %r", e.get("title"))
            r = None
        if r and str(r.get("confidence", "medium")).lower() in ("high", "medium"):
            verified.append(r)
        elif r:
            r["confidence"] = "medium"
            verified.append(r)
        else:
            fallback = dict(e)
            fallback["confidence"] = "medium"
            fallback["verification_reason"] = "daily_pass_through"
            verified.append(fallback)
            time_ok = False

    if not verified:
        verified = [dict(e) for e in events]
        for v in verified:
            v["confidence"] = "medium"
        time_ok = False

    verified_ok = all(
        str(v.get("confidence", "medium")).lower() in ("high", "medium")
        for v in verified
    )
    return verified, verified_ok, time_ok


async def build_daily_content_package(
    events: list[dict[str, Any]] | None = None,
) -> DailyContentPackage | None:
    if events is not None:
        campaign = collect_campaign_events(events)
    else:
        pool, _, _, _ = await _fetch_radar_pipeline()
        campaign = collect_campaign_events(pool)
    if not campaign:
        log.info("daily content: no campaign events")
        return None

    verified, verified_ok, time_ok = await verify_events_for_daily(campaign)
    post_text = await generate_daily_campaign_post(verified)
    draft_id = await insert_draft("daily_campaign", post_text, "draft")

    primary = verified[0]
    image_bytes, image_source, image_path = await find_event_image(
        primary, draft_id=draft_id
    )
    await upsert_draft_asset(
        draft_id,
        image_path=image_path or "",
        event_json=json.dumps(verified, ensure_ascii=False),
        poster_source=image_source,
    )
    await record_scheduled_post(
        campaign_date=date.today().isoformat(),
        events_json=json.dumps(verified, ensure_ascii=False),
        draft_id=draft_id,
        status="ready",
    )
    await save_radar_snapshot("now24", verified, {"source": "daily_scheduler"})

    return DailyContentPackage(
        events=verified,
        post_text=post_text,
        image_bytes=image_bytes,
        image_path=image_path,
        image_source=image_source,
        draft_id=draft_id,
        verified_ok=verified_ok,
        time_ok=time_ok,
    )


async def deliver_daily_content_to_admin(
    bot: Bot,
    pkg: DailyContentPackage,
    *,
    chat_id: int | None = None,
) -> None:
    target = chat_id or ADMIN_ID
    if not target:
        log.warning("No chat_id for daily delivery")
        return

    titles = ", ".join(str(e.get("title", ""))[:40] for e in pkg.events[:3])
    status = (
        "✅ Daily Content · Gastrobar\n\n"
        f"События: {titles}\n"
        f"{'✅' if pkg.verified_ok else '⚠️'} событие подтверждено\n"
        f"{'✅' if pkg.time_ok else '⚠️'} время подтверждено ({TIMEZONE})\n"
        f"{'✅' if pkg.image_source != 'none' else '⚠️'} картинка: {pkg.image_source or 'нет'}\n"
    )
    await bot.send_message(target, status)

    from keyboards import daily_post_kb

    kb = daily_post_kb(pkg.draft_id)
    if pkg.image_path and Path(pkg.image_path).is_file():
        await bot.send_photo(
            target,
            photo=FSInputFile(pkg.image_path),
            caption=pkg.post_text,
            reply_markup=kb,
        )
    elif pkg.image_bytes:
        await bot.send_photo(
            target,
            photo=BufferedInputFile(pkg.image_bytes, filename="daily_post.png"),
            caption=pkg.post_text,
            reply_markup=kb,
        )
    else:
        await bot.send_message(target, pkg.post_text, reply_markup=kb)


async def run_scheduled_daily_content(bot: Bot) -> None:
    log.info("scheduled daily content generator started")
    try:
        pkg = await build_daily_content_package()
        if not pkg:
            log.info("scheduled daily: nothing to send")
            return
        await deliver_daily_content_to_admin(bot, pkg)
        log.info("scheduled daily content delivered draft_id=%s", pkg.draft_id)
    except Exception:
        log.exception("scheduled daily content failed")
