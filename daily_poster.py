"""
Постер для поста дня: поиск официального изображения или AI fallback (Imagen).
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
from event_verifier import bar_event_blob
from gemini_client import effective_gemini_model, log_gemini_error

log = logging.getLogger(__name__)

POSTERS_DIR = Path(__file__).resolve().parent / "posters"
IMAGEN_MODEL = "imagen-3.0-generate-002"
_IMAGE_URL_RE = re.compile(r"https?://[^\s\"\'\)]+\.(?:jpg|jpeg|png|webp)", re.I)


def _client() -> genai.Client:
    return genai.Client(api_key=GEMINI_API_KEY)


def _poster_prompt(event: dict[str, Any], *, ai_fallback: bool = False) -> str:
    title = str(event.get("title", "")).strip()
    sub = str(event.get("subtitle", event.get("league", ""))).strip()
    em = str(event.get("emoji", "🏟")).strip()
    if ai_fallback:
        return (
            f"Dark premium cinematic sports event poster for a nightlife bar TV screen. "
            f"Black background, subtle neon glow, high contrast, minimal text area. "
            f"Event: {title}. {sub}. Style: Gastrobar aesthetic, moody, upscale, "
            f"no watermark, no logos, no crowded text, vertical-friendly composition."
        )
    return (
        f"Find the official event poster or promotional key art image URL for: {title}. "
        f"{sub}. Category: {event.get('category', '')}. "
        f"Return JSON only: {{\"image_url\": \"https://...\"}} with a direct image link (jpg/png/webp). "
        f"If multiple, pick the most official sports broadcaster or league poster."
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
        log.warning("poster download failed %s: %s", url[:80], e)
        return None


def _search_poster_url_sync(event: dict[str, Any]) -> str | None:
    if not GEMINI_API_KEY:
        return None
    client = _client()
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )
    try:
        resp = client.models.generate_content(
            model=effective_gemini_model(),
            contents=_poster_prompt(event, ai_fallback=False),
            config=config,
        )
        text = (resp.text or "").strip()
        return _extract_image_url(text) if text else None
    except Exception as e:
        log_gemini_error("poster_search", e)
        return None


def _generate_ai_poster_sync(event: dict[str, Any]) -> bytes | None:
    if not GEMINI_API_KEY:
        return None
    client = _client()
    try:
        resp = client.models.generate_images(
            model=IMAGEN_MODEL,
            prompt=_poster_prompt(event, ai_fallback=True),
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
        log_gemini_error("poster_imagen", e)
    return None


async def resolve_event_poster(
    event: dict[str, Any],
    *,
    draft_id: int | None = None,
) -> tuple[bytes | None, str, str | None]:
    """
    (bytes, source_label, saved_path)
    source: official_search | ai_generated | none
    """
    POSTERS_DIR.mkdir(parents=True, exist_ok=True)
    b = bar_event_blob(event)
    title = str(event.get("title", ""))[:40]

    url = await asyncio.to_thread(_search_poster_url_sync, event)
    if url:
        log.info("poster search url for %r: %s", title, url[:100])
        data = await _download_image(url)
        if data:
            path = _save_bytes(data, draft_id, suffix="official")
            return data, "official_search", path

    log.info("poster AI fallback for %r category=%s", title, event.get("category"))
    data = await asyncio.to_thread(_generate_ai_poster_sync, event)
    if data:
        path = _save_bytes(data, draft_id, suffix="ai")
        return data, "ai_generated", path

    return None, "none", None


def _save_bytes(data: bytes, draft_id: int | None, *, suffix: str) -> str:
    POSTERS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"daily_{draft_id or 'tmp'}_{suffix}.png"
    path = POSTERS_DIR / name
    path.write_bytes(data)
    return str(path)


async def regenerate_poster_only(
    event: dict[str, Any],
    *,
    draft_id: int | None = None,
    force_ai: bool = True,
) -> tuple[bytes | None, str, str | None]:
    if force_ai:
        data = await asyncio.to_thread(_generate_ai_poster_sync, event)
        if data:
            path = _save_bytes(data, draft_id, suffix="ai_redo")
            return data, "ai_generated", path
        return None, "none", None
    return await resolve_event_poster(event, draft_id=draft_id)
