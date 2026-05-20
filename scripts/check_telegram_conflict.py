"""Проверка: нет ли второго клиента getUpdates."""



from __future__ import annotations



import asyncio

import sys

from pathlib import Path



_ROOT = Path(__file__).resolve().parents[1]

if str(_ROOT) not in sys.path:

    sys.path.insert(0, str(_ROOT))



try:

    from aiogram import Bot

    from aiogram.exceptions import TelegramConflictError

except ModuleNotFoundError:

    print("Ошибка: aiogram не установлен в этом Python.")

    print("Не запускайте голый «python» из системы.")

    print("Используйте:  check_conflict.bat")

    print("   или:      .venv\\Scripts\\python scripts\\check_telegram_conflict.py")

    raise SystemExit(1) from None



from config import TELEGRAM_BOT_TOKEN



_PROBE_TIMEOUT_SEC = 6

_PROBE_ATTEMPTS = 3





async def _check() -> int:

    if not TELEGRAM_BOT_TOKEN:

        print("TELEGRAM_BOT_TOKEN не задан в .env")

        return 1



    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    try:

        await bot.delete_webhook(drop_pending_updates=False)

        me = await bot.get_me()

        wh = await bot.get_webhook_info()

        if str(wh.url or "").strip():

            print(f"WARNING: webhook: {wh.url!r}")

            await bot.delete_webhook(drop_pending_updates=True)



        print(f"Проверка getUpdates ({_PROBE_ATTEMPTS}x timeout={_PROBE_TIMEOUT_SEC}s)...")

        for attempt in range(1, _PROBE_ATTEMPTS + 1):

            try:

                await bot.get_updates(timeout=_PROBE_TIMEOUT_SEC, limit=1)

            except TelegramConflictError:

                print(f"  попытка {attempt}/{_PROBE_ATTEMPTS}: Conflict")

                _print_conflict_help()

                return 1

            print(f"  попытка {attempt}/{_PROBE_ATTEMPTS}: OK")



        print(f"OK: бот @{me.username} — можно запускать start_bot.bat")

        return 0

    finally:

        await bot.session.close()





def _print_conflict_help() -> None:

    print()

    print("=" * 62)

    print("  КОНФЛИКТ: другой экземпляр уже опрашивает Telegram")

    print("=" * 62)

    print()

    print("Это не обязательно Railway. Возможные причины:")

    print("  - второе окно start_bot.bat на этом ПК")

    print("  - бот на другом компьютере")

    print("  - другой хостинг (Render, VPS, GitHub Actions)")

    print("  - старый токен из git / у кого-то из команды")

    print()

    print("Что сделать:")

    print("  1. Закрыть ВСЕ окна с ботом")

    print("  2. scripts\\list_bot_processes.ps1  (должно быть пусто)")

    print("  3. Если Conflict остался:")

    print("     BotFather -> @gastrobar_nhatrang_bot -> API Token -> Revoke")

    print("     новый токен в .env -> start_bot.bat")

    print()

    print("После Revoke все старые копии бота отключатся.")

    print()





if __name__ == "__main__":

    raise SystemExit(asyncio.run(_check()))


