"""
Поиск постера/картинки для события: официальный арт (Gemini Search) или AI fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from google import genai
from google.genai import types

from config import GEMINI_API_KEY
from gemini_client import effective_gemini_model, log_gemini_error

log = logging.getLogger(__name__)

POSTERS_DIR = Path(__file__).resolve().parent / "posters"
IMAGEN_MODEL = "imagen-3.0-generate-002"
_IMAGE_URL_RE = re.compile(r"https?://[^\s\"\'\)]+\.(?:jpg|jpeg|png|webp)", re.I)


def _client() -> genai.Client:
    return genai.Client(api_key=GEMINI_API_KEY)


def _image_prompt(event: dict[str, Any], *, ai_fallback: bool = False) -> str:
    title = str(event.get("title", "")).strip()
    sub = str(event.get("subtitle", event.get("league", ""))).strip()
    if ai_fallback:
        return (
            f"Dark premium cinematic sports event poster for a nightlife bar TV screen. "
            f"Black background, subtle neon glow, high contrast, readable title area. "
            f"Event: {title}. {sub}. Gastrobar aesthetic, moody, upscale, "
            f"no watermark, no crowded text, vertical-friendly."
        )
    return (
        f"Find the official event poster or promotional key art image URL for: {title}. "
        f"{sub}. Category: {event.get('category', '')}. "
        f"Return JSON only: {{\"image_url\": \"https://...\"}} direct jpg/png/webp link."
    )


def _extract_image_url(text: str) -> str | None:
    m = _IMAGE_URL_RE.search(text)
    if m:
        return m.group(0).rstrip(".,)")
    try:
        data = json.loads(text.strip().removeprefix("```json").removesuffix("```"))
        if isinstance(data, dict):
            url = str(data.get("image_url") or data.get("url") or "").strip()
            if url.startswith("http"):
                return url
    except Exception:
        pass
    i0, i1 = text.find("{"), text.rfind("}")
    if i0 >= 0 and i1 > i0:
        try:
            data = json.loads(text[i0 : i1 + 1])
            url = str(data.get("image_url") or "").strip()
            if url.startswith("http"):
                return url
        except Exception:
            pass
    return None


async def _download_image(url: str) -> bytes | None:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "GastrobarBot/1.0"})
        if r.status_code != 200:
            return None
        ctype = (r.headers.get("content-type") or "").lower()
        if "image" not in ctype and not url.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            return None
        if len(r.content) < 500 or len(r.content) > 8_000_000:
            return None
        return r.content
    except Exception as e:
        log.warning("image download failed %s: %s", url[:80], e)
        return None


def _search_image_url_sync(event: dict[str, Any]) -> str | None:
    if not GEMINI_API_KEY:
        return None
    client = _client()
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )
    try:
        resp = client.models.generate_content(
            model=effective_gemini_model(),
            contents=_image_prompt(event, ai_fallback=False),
            config=config,
        )
        text = (resp.text or "").strip()
        return _extract_image_url(text) if text else None
    except Exception as e:
        log_gemini_error("image_search", e)
        return None


def _generate_ai_image_sync(event: dict[str, Any]) -> bytes | None:
    if not GEMINI_API_KEY:
        return None
    client = _client()
    try:
        resp = client.models.generate_images(
            model=IMAGEN_MODEL,
            prompt=_image_prompt(event, ai_fallback=True),
            config=types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio="3:4",
            ),
        )
        if not resp or not resp.generated_images:
            return None
        img = resp.generated_images[0].image
        if img and img.image_bytes:
            return img.image_bytes
    except Exception as e:
        log_gemini_error("image_imagen", e)
    return None


def _save_bytes(data: bytes, tag: str, *, suffix: str) -> str:
    POSTERS_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\-]", "_", tag)[:32]
    path = POSTERS_DIR / f"{safe}_{suffix}.png"
    path.write_bytes(data)
    return str(path)


async def find_event_image(
    event: dict[str, Any],
    *,
    draft_id: int | None = None,
    force_ai: bool = False,
) -> tuple[bytes | None, str, str | None]:
    """
    (bytes, source_label, saved_path)
    source: official_search | ai_generated | none
    """
    tag = f"daily_{draft_id or 'tmp'}"
    title = str(event.get("title", ""))[:40]

    if not force_ai:
        url = await asyncio.to_thread(_search_image_url_sync, event)
        if url:
            log.info("image search for %r: %s", title, url[:100])
            data = await _download_image(url)
            if data:
                path = _save_bytes(data, tag, suffix="official")
                return data, "official_search", path

    log.info("image AI fallback for %r", title)
    data = await asyncio.to_thread(_generate_ai_image_sync, event)
    if data:
        path = _save_bytes(data, tag, suffix="ai")
        return data, "ai_generated", path
    return None, "none", None


async def regenerate_event_image(
    event: dict[str, Any],
    *,
    draft_id: int | None = None,
) -> tuple[bytes | None, str, str | None]:
    return await find_event_image(event, draft_id=draft_id, force_ai=True)
