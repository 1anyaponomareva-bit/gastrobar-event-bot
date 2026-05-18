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

DAILY_POST_HOUR: int = int(os.getenv("DAILY_POST_HOUR", "11") or "11")

# Авто-афиша недели: четверг 10:40 (Asia/Ho_Chi_Minh)
WEEKLY_RADAR_DOW: int = int(os.getenv("WEEKLY_RADAR_DOW", "3") or "3")  # 0=Пн … 3=Чт
WEEKLY_RADAR_HOUR: int = int(os.getenv("WEEKLY_RADAR_HOUR", "10") or "10")
WEEKLY_RADAR_MINUTE: int = int(os.getenv("WEEKLY_RADAR_MINUTE", "40") or "40")

# Weekly Event Radar: до N событий при высоком watchability
RADAR_WEEKLY_MAX: int = max(6, int(os.getenv("RADAR_WEEKLY_MAX", "15") or "15"))
RADAR_MIN_WATCHABILITY: int = max(
    0, int(os.getenv("RADAR_MIN_WATCHABILITY", "52") or "52")
)

# Сколько телевизоров в баре — лимит параллельных эфиров в daily / now24
GASTROBAR_TV_COUNT: int = max(1, int(os.getenv("GASTROBAR_TV_COUNT", "2") or "2"))
