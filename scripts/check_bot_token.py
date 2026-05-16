"""Печатает @username бота для TELEGRAM_BOT_TOKEN из окружения / .env."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aiogram import Bot

from config import EXPECTED_BOT_USERNAME, RUN_MODE, TELEGRAM_BOT_TOKEN


async def _main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is missing", file=sys.stderr)
        raise SystemExit(1)
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        me = await bot.get_me()
        print(f"RUN_MODE={RUN_MODE}")
        print(f"bot_id={me.id}")
        print(f"username=@{me.username}")
        if EXPECTED_BOT_USERNAME:
            ok = (me.username or "").lower() == EXPECTED_BOT_USERNAME.lower()
            print(f"expected=@{EXPECTED_BOT_USERNAME} match={ok}")
            if not ok:
                raise SystemExit(2)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(_main())
