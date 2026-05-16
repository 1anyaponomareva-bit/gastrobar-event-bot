"""Загрузка конфигурации из переменных окружения."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Явный путь к .env рядом с этим файлом — чтобы ключи подхватывались при любом cwd
# и при запуске не из .venv (например `python main.py` из системного Python).
_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")
load_dotenv()  # поверх: переменные из окружения процесса / текущей папки

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0") or 0)
GASTROBAR_GROUP_ID: int = int(os.getenv("GASTROBAR_GROUP_ID", "0") or 0)
SPORTS_API_KEY: str = os.getenv("SPORTS_API_KEY", "").strip()

# Пустая переменная на Railway → default gemini-2.5-flash
GEMINI_MODEL: str = (os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip()

DATABASE_PATH: str = os.getenv("DATABASE_PATH", "gastrobar_bot.sqlite3").strip()

# local — bot.lock на диске; railway — один инстанс на платформе, без lock-файла
_RUN_MODE_RAW = os.getenv("RUN_MODE", "").strip().lower()
if _RUN_MODE_RAW in ("railway", "local"):
    RUN_MODE: str = _RUN_MODE_RAW
elif os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"):
    RUN_MODE = "railway"
else:
    RUN_MODE = "local"

# Fail-fast, если на Railway подставили чужой токен (например SpiceSpace)
EXPECTED_BOT_USERNAME: str = (
    os.getenv("EXPECTED_BOT_USERNAME", "gastrobar_nhatrang_bot").strip().lstrip("@")
)


def is_railway_run() -> bool:
    return RUN_MODE == "railway"


BAR_OPEN_TIME: str = (os.getenv("BAR_OPEN_TIME") or "08:00").strip()
BAR_CLOSE_TIME: str = (os.getenv("BAR_CLOSE_TIME") or "06:00").strip()
TIMEZONE: str = (os.getenv("TIMEZONE") or "Asia/Ho_Chi_Minh").strip()
