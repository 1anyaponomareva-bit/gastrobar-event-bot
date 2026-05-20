"""Проверка: кто держит getUpdates, и что делать если Railway уже удалён."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aiogram import Bot
from aiogram.exceptions import TelegramConflictError

from config import TELEGRAM_BOT_TOKEN


async def main() -> int:
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN ne zadan v .env")
        return 1

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        me = await bot.get_me()
        wh = await bot.get_webhook_info()
        print(f"Bot: @{me.username} (id={me.id})")
        print(f"Webhook: {wh.url or '(pustoj)'}")
        print()
        print("Proverka: odin getUpdates timeout=8s...")
        try:
            await bot.get_updates(timeout=8, limit=1)
            print("OK - vtoroj poller NE obnaruzhen. Mozhno zapuskat start_bot.bat")
            return 0
        except TelegramConflictError:
            print("CONFLICT - drugoj klient UZHE derzhit getUpdates.")
            print()
            print("Eto NE obyazatelno Railway. Vozmozhnye prichiny:")
            print("  - vtoroe okno start_bot.bat / python main.py na etom PK")
            print("  - staryj bot na drugom kompyutere")
            print("  - drugoj hosting (Render, Fly, VPS, GitHub Actions)")
            print("  - tot zhe token v drugom proekte / u kogo-to iz komandy")
            print()
            print("Samoe bystroye reshenie:")
            print("  1. BotFather -> @gastrobar_nhatrang_bot -> API Token -> Revoke")
            print("  2. Skopirovat NOVYJ token v .env (TELEGRAM_BOT_TOKEN=...)")
            print("  3. start_bot.bat")
            print()
            print("Posle Revoke vse starye kopii bota perestanut poluchat soobshcheniya.")
            return 1
    finally:
        await bot.session.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
