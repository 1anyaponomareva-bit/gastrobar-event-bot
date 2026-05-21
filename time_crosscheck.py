"""
Gemini Search: сверка времени кандидата с BetBoom, Google Sports, официальными сайтами лиг.
При 429 / исчерпании квоты — пропуск (событие не отбрасывается).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from google import genai
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_MODEL
from event_time import _DATE_RE, _parse_time_flexible
from gemini_client import is_gemini_quota_error, is_gemini_transient_error, log_gemini_error

log = logging.getLogger(__name__)

_TIME_CROSSCHECK_ENABLED = os.getenv("RADAR_TIME_CROSSCHECK", "0").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
_MAX_OFFICIAL_DELTA_MIN = int(os.getenv("RADAR_TIME_CROSSCHECK_MAX_MIN", "20") or "20")
_QUOTA_COOLDOWN_SEC = float(os.getenv("RADAR_TIME_CROSSCHECK_QUOTA_COOLDOWN", "3600") or "3600")

_quota_open_until: float = 0.0

TIME_CROSSCHECK_PROMPT = """You are a sports schedule fact-checker for a bar TV guide in Vietnam.

Cross-check the KICKOFF / START TIME of this event using Google Search.

Candidate (from Event Radar — may be wrong):
{candidate_json}

Search priority (use at least 2 independent types when possible):
1. Official league or tournament website (e.g. premierleague.com, UEFA.com)
2. Google Sports / Google scoreboard listing for this exact match
3. BetBoom or other major sportsbook line for this exact match (kickoff time)

Rules:
* Confirm EXACT participants and match (not a different fixture).
* Return OFFICIAL local date (YYYY-MM-DD) and start time (HH:MM) in source_timezone (IANA).
* Do NOT convert to Vietnam or Asia/Ho_Chi_Minh — Python converts once.
* Premier League → source_timezone Europe/London
* Europa League / UCL → Europe/Paris unless venue clearly UK
* NBA / NHL default → America/New_York unless West Coast teams
* If sources disagree by ≤15 minutes, pick official league site; note others in notes.
* If candidate time differs from official by >15 minutes, set candidate_matches_official:false and return official values.

Return JSON only:
{{
  "time_verified": true,
  "candidate_matches_official": true,
  "date": "2026-05-24",
  "time": "18:30",
  "source_timezone": "Europe/London",
  "time_precision": "exact",
  "reference_source": "Premier League / Google Sports",
  "notes": "optional short note"
}}

If you cannot verify kickoff time from reliable sources:
{{"time_verified": false, "reason": "..."}}
"""


@dataclass
class TimeCrosscheckResult:
    ok: bool
    candidate_matches: bool = True
    skipped: bool = False
    date: str = ""
    time: str = ""
    source_timezone: str = ""
    time_precision: str = "exact"
    reference_source: str = ""
    notes: str = ""
    reason: str = ""


def time_crosscheck_enabled() -> bool:
    return _TIME_CROSSCHECK_ENABLED and bool(GEMINI_API_KEY)


def _quota_circuit_open() -> bool:
    return time.monotonic() < _quota_open_until


def _trip_quota_circuit(exc: BaseException | None = None) -> None:
    global _quota_open_until
    from gemini_client import disable_gemini_search

    _quota_open_until = time.monotonic() + _QUOTA_COOLDOWN_SEC
    disable_gemini_search("quota_exhausted")
    log.warning(
        "time_crosscheck: Gemini quota circuit open %.0fs (%s)",
        _QUOTA_COOLDOWN_SEC,
        type(exc).__name__ if exc else "prefetch",
    )


def should_time_crosscheck(event: dict[str, Any], *, api_reason: str = "") -> bool:
    """Только спорт без API-SPORTS — экономия квоты Gemini."""
    if api_reason == "api_sports_match":
        return False
    if not time_crosscheck_enabled() or _quota_circuit_open():
        return False
    cat = str(event.get("category", "")).upper()
    blob = " ".join(
        str(event.get(k, ""))
        for k in ("title", "subtitle", "league", "category", "why")
    ).upper()
    if any(x in cat for x in ("FOOT", "SOCCER", "NBA", "NHL", "UFC", "MMA", "F1", "FORMULA")):
        return True
    return bool(
        re.search(
            r"PREMIER\s+LEAGUE|EUROPA\s+LEAGUE|CHAMPIONS\s+LEAGUE|"
            r"\bNBA\b|\bNHL\b|FORMULA\s*1|\bUFC\b",
            blob,
        )
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    t = text.strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t)
        if m:
            t = m.group(1).strip()
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        i0, i1 = t.find("{"), t.rfind("}")
        if i0 == -1 or i1 <= i0:
            raise
        data = json.loads(t[i0 : i1 + 1])
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return data


def _utc_from_fields(date_s: str, time_s: str, tz: str) -> datetime | None:
    from timezone_truth import resolve_trusted_source_timezone, source_to_utc_datetime

    time_norm, _ = _parse_time_flexible(time_s)
    if not time_norm or not _DATE_RE.match(date_s):
        return None
    trusted = resolve_trusted_source_timezone(
        {"title": "", "category": "FOOTBALL", "source_timezone": tz}
    ) or tz
    try:
        return source_to_utc_datetime(date_s, time_norm, trusted)
    except Exception:
        return None


def _delta_minutes(a: datetime, b: datetime) -> float:
    return abs((a.astimezone(timezone.utc) - b.astimezone(timezone.utc)).total_seconds()) / 60.0


def _log_crosscheck(
    event: dict[str, Any],
    result: TimeCrosscheckResult,
    *,
    phase: str,
) -> None:
    cand_d = str(event.get("original_date") or event.get("date", ""))
    cand_t = str(event.get("original_time") or event.get("time", ""))
    cand_z = str(event.get("source_timezone") or event.get("original_timezone", ""))
    if result.skipped:
        log.info(
            "TIME CROSSCHECK [%s] SKIPPED: title=%r reason=%s",
            phase,
            event.get("title"),
            result.reason,
        )
        return
    log.info(
        "TIME CROSSCHECK [%s]: title=%r candidate=%s %s %s | official=%s %s %s | "
        "match=%s verified=%s ref=%s notes=%s",
        phase,
        event.get("title"),
        cand_d,
        cand_t,
        cand_z,
        result.date,
        result.time,
        result.source_timezone,
        result.candidate_matches,
        result.ok,
        result.reference_source,
        (result.notes or result.reason)[:120],
    )


def _gemini_time_crosscheck_sync(event: dict[str, Any]) -> TimeCrosscheckResult:
    if not GEMINI_API_KEY:
        return TimeCrosscheckResult(ok=False, reason="no_gemini_key")
    if _quota_circuit_open():
        return TimeCrosscheckResult(ok=True, skipped=True, reason="quota_circuit_open")

    payload = {
        "title": event.get("title"),
        "category": event.get("category"),
        "subtitle": event.get("subtitle", event.get("league")),
        "candidate_date": event.get("original_date") or event.get("date"),
        "candidate_time": event.get("original_time") or event.get("time"),
        "candidate_source_timezone": event.get("source_timezone")
        or event.get("original_timezone"),
        "candidate_utc": event.get("utc_datetime"),
        "candidate_vn": f"{event.get('local_weekday')} {event.get('local_time')}",
    }
    prompt = TIME_CROSSCHECK_PROMPT.replace(
        "{candidate_json}", json.dumps(payload, ensure_ascii=False, indent=2)
    )
    client = genai.Client(api_key=GEMINI_API_KEY)
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=config,
            )
            last_err = None
            break
        except Exception as e:
            last_err = e
            if is_gemini_quota_error(e):
                log_gemini_error("time_crosscheck", e)
                _trip_quota_circuit(e)
                return TimeCrosscheckResult(ok=True, skipped=True, reason="gemini_quota")
            if attempt < 3 and is_gemini_transient_error(e):
                delay = min(30.0, 4.0 * attempt)
                log.warning("time_crosscheck retry %s/3 in %.0fs", attempt, delay)
                time.sleep(delay)
                continue
            log_gemini_error("time_crosscheck", e)
            return TimeCrosscheckResult(ok=False, reason=str(e)[:200])

    if last_err:
        return TimeCrosscheckResult(ok=False, reason=str(last_err)[:200])

    text = (response.text or "").strip()
    if not text:
        return TimeCrosscheckResult(ok=False, reason="empty_response")
    try:
        data = _extract_json_object(text)
    except Exception as e:
        log.info("time_crosscheck JSON parse failed: %s", e)
        return TimeCrosscheckResult(ok=False, reason="json_parse_failed")

    if not data.get("time_verified") in (True, "true", "yes", 1):
        return TimeCrosscheckResult(
            ok=False,
            reason=str(data.get("reason", "not_verified"))[:200],
        )

    date_s = str(data.get("date", "")).strip()
    time_s = str(data.get("time", "")).strip()
    tz = str(data.get("source_timezone", "")).strip()
    if not (_DATE_RE.match(date_s) and time_s and tz):
        return TimeCrosscheckResult(ok=False, reason="missing_official_fields")

    cand_matches = data.get("candidate_matches_official")
    if cand_matches is True or str(cand_matches).lower() in ("true", "yes", "1"):
        candidate_matches = True
    elif cand_matches is False or str(cand_matches).lower() in ("false", "no", "0"):
        candidate_matches = False
    else:
        candidate_matches = True

    cand_d = str(event.get("original_date") or event.get("date", "")).strip()
    cand_t = str(event.get("original_time") or event.get("time", "")).strip()
    cand_z = str(event.get("source_timezone") or event.get("original_timezone", "")).strip()
    if cand_d and cand_t and cand_z:
        cand_utc = _utc_from_fields(cand_d, cand_t, cand_z)
        off_utc = _utc_from_fields(date_s, time_s, tz)
        if cand_utc and off_utc:
            delta = _delta_minutes(cand_utc, off_utc)
            if delta > _MAX_OFFICIAL_DELTA_MIN:
                candidate_matches = False
                log.warning(
                    "TIME CROSSCHECK delta=%.0f min > %s: title=%r cand=%s %s off=%s %s",
                    delta,
                    _MAX_OFFICIAL_DELTA_MIN,
                    event.get("title"),
                    cand_d,
                    cand_t,
                    date_s,
                    time_s,
                )

    tp = str(data.get("time_precision", "exact")).lower().strip()
    if tp not in ("exact", "estimated"):
        tp = "exact"

    return TimeCrosscheckResult(
        ok=True,
        candidate_matches=candidate_matches,
        date=date_s,
        time=time_s,
        source_timezone=tz,
        time_precision=tp,
        reference_source=str(data.get("reference_source", "")).strip(),
        notes=str(data.get("notes", "")).strip(),
    )


def apply_time_crosscheck(
    event: dict[str, Any],
    *,
    phase: str = "verify",
    require_match: bool = True,
    api_reason: str = "",
) -> dict[str, Any] | None:
    """
    Сверка времени через Gemini Search; при расхождении — пересборка schedule из official.
    При квоте 429 — пропуск, событие остаётся с уже залоченным временем.
    """
    if not should_time_crosscheck(event, api_reason=api_reason):
        return event

    title = str(event.get("title", "")).strip()
    if not title:
        return None

    result = _gemini_time_crosscheck_sync(event)
    _log_crosscheck(event, result, phase=phase)

    if result.skipped:
        event = dict(event)
        event.setdefault(
            "verification_reason",
            str(event.get("verification_reason", "")).strip(),
        )
        note = "time_crosscheck_skipped_quota"
        if note not in event["verification_reason"]:
            event["verification_reason"] = (
                f"{event['verification_reason']};{note}".strip(";")
                if event["verification_reason"]
                else note
            )
        return event

    if not result.ok:
        log.info("time_crosscheck rejected: title=%r reason=%s", title, result.reason)
        return None

    if require_match and not result.candidate_matches:
        log.info(
            "time_crosscheck candidate mismatch — applying official time: title=%r",
            title,
        )

    from locked_time import lock_event_schedule

    merged = dict(event)
    merged["original_date"] = result.date
    merged["original_time"] = result.time
    merged["original_timezone"] = result.source_timezone
    merged["source_timezone"] = result.source_timezone
    merged["time_precision"] = result.time_precision
    if result.reference_source:
        ref = str(merged.get("why", "")).strip()
        merged["why"] = (
            f"{ref} · {result.reference_source}".strip(" ·")
            if ref
            else result.reference_source
        )
    merged.pop("utc_datetime", None)
    merged.pop("local_datetime", None)
    merged["time_locked"] = False
    merged["schedule_locked"] = False

    locked = lock_event_schedule(merged, phase=f"time_crosscheck_{phase}")
    if locked is None:
        log.info("time_crosscheck lock failed: title=%r", title)
        return None

    from timezone_truth import log_event_debug

    log_event_debug(locked, phase=f"time_crosscheck_{phase}")
    locked["time_verified_via"] = result.reference_source or "Gemini time crosscheck"
    if not result.candidate_matches:
        locked["verification_reason"] = (
            str(locked.get("verification_reason", "")).strip()
            + ";time_corrected_by_crosscheck"
        ).strip(";")
    return locked


async def apply_time_crosscheck_async(
    event: dict[str, Any],
    *,
    phase: str = "verify",
    require_match: bool = True,
    api_reason: str = "",
) -> dict[str, Any] | None:
    import asyncio

    if not should_time_crosscheck(event, api_reason=api_reason):
        return event
    return await asyncio.to_thread(
        apply_time_crosscheck,
        event,
        phase=phase,
        require_match=require_match,
        api_reason=api_reason,
    )
