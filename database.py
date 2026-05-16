"""SQLite через aiosqlite: события и черновики."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from config import DATABASE_PATH


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db() -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport TEXT NOT NULL,
                title TEXT NOT NULL,
                league TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                importance TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS draft_assets (
                draft_id INTEGER PRIMARY KEY,
                image_path TEXT NOT NULL,
                event_json TEXT NOT NULL,
                poster_source TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (draft_id) REFERENCES drafts(id)
            )
            """
        )
        await db.commit()


async def replace_week_events(rows: list[dict[str, Any]]) -> None:
    """Полная замена списка событий недели (после /week или авто-рассылки)."""
    created = _utc_now()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM events")
        if not rows:
            await db.commit()
            return
        await db.executemany(
            """
            INSERT INTO events (
                sport, title, league, date, time, importance, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r["sport"],
                    r["title"],
                    r["league"],
                    r["date"],
                    r["time"],
                    r["importance"],
                    r.get("reason", ""),
                    created,
                )
                for r in rows
            ],
        )
        await db.commit()


async def get_week_events_stored() -> list[dict[str, Any]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT sport, title, league, date, time, importance, reason
            FROM events ORDER BY date, time
            """
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def insert_draft(draft_type: str, text: str, status: str = "draft") -> int:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO drafts (type, text, status, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (draft_type, text, status, _utc_now()),
        )
        await db.commit()
        return int(cur.lastrowid)


async def get_draft(draft_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, type, text, status, created_at FROM drafts WHERE id = ?",
            (draft_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def update_draft_text(draft_id: int, text: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("UPDATE drafts SET text = ? WHERE id = ?", (text, draft_id))
        await db.commit()


async def update_draft_status(draft_id: int, status: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE drafts SET status = ? WHERE id = ?",
            (status, draft_id),
        )
        await db.commit()


async def serialize_events_for_prompt(events: list[dict[str, Any]]) -> str:
    return json.dumps(events, ensure_ascii=False, indent=2)


async def upsert_draft_asset(
    draft_id: int,
    *,
    image_path: str,
    event_json: str,
    poster_source: str = "",
) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            INSERT INTO draft_assets (draft_id, image_path, event_json, poster_source)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(draft_id) DO UPDATE SET
                image_path=excluded.image_path,
                event_json=excluded.event_json,
                poster_source=excluded.poster_source
            """,
            (draft_id, image_path, event_json, poster_source),
        )
        await db.commit()


async def get_draft_asset(draft_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT draft_id, image_path, event_json, poster_source FROM draft_assets WHERE draft_id = ?",
            (draft_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None
