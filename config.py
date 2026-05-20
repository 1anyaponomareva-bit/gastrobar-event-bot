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

def _on_railway_host() -> bool:
    """Railway всегда выставляет эти переменные в контейнере."""
    return bool(
        os.getenv("RAILWAY_ENVIRONMENT")
        or os.getenv("RAILWAY_PROJECT_ID")
        or os.getenv("RAILWAY_SERVICE_ID")
    )


def _resolve_run_mode() -> str:
    """На Railway — всегда railway (даже если в Variables ошибочно RUN_MODE=local)."""
    if _on_railway_host():
        return "railway"
    raw = os.getenv("RUN_MODE", "").strip().lower()
    if raw == "railway":
        return "railway"
    if raw == "local":
        return "local"
    return "local"


RUN_MODE: str = _resolve_run_mode()

# Fail-fast, если на Railway подставили чужой токен (например SpiceSpace)
EXPECTED_BOT_USERNAME: str = (
    os.getenv("EXPECTED_BOT_USERNAME", "gastrobar_nhatrang_bot").strip().lstrip("@")
)


def is_railway_run() -> bool:
    return RUN_MODE == "railway"


def is_local_run() -> bool:
    return RUN_MODE == "local"


BAR_OPEN_TIME: str = (os.getenv("BAR_OPEN_TIME") or "08:00").strip()
BAR_CLOSE_TIME: str = (os.getenv("BAR_CLOSE_TIME") or "06:00").strip()
TIMEZONE: str = (os.getenv("TIMEZONE") or "Asia/Ho_Chi_Minh").strip()

DAILY_POST_HOUR: int = int(os.getenv("DAILY_POST_HOUR", "11") or "11")

# Авто-афиша недели: четверг 10:40 (Asia/Ho_Chi_Minh)
WEEKLY_RADAR_DOW: int = int(os.getenv("WEEKLY_RADAR_DOW", "3") or "3")  # 0=Пн … 3=Чт
WEEKLY_RADAR_HOUR: int = int(os.getenv("WEEKLY_RADAR_HOUR", "10") or "10")
WEEKLY_RADAR_MINUTE: int = int(os.getenv("WEEKLY_RADAR_MINUTE", "40") or "40")

# Weekly Event Radar: мягкий потолок (реальный отбор — watchability, не count)
RADAR_WEEKLY_MAX: int = max(
    50, int(os.getenv("RADAR_WEEKLY_MAX", "999") or "999")
)
RADAR_MIN_WATCHABILITY: int = max(
    0, int(os.getenv("RADAR_MIN_WATCHABILITY", "18") or "18")
)
# API-first: Gemini только если API дал мало событий
RADAR_API_FIRST: bool = os.getenv("RADAR_API_FIRST", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
RADAR_API_MIN_SEED: int = max(
    1, int(os.getenv("RADAR_API_MIN_SEED", "3") or "3")
)
# Now24: без жёсткого top-N (только watchability + дедуп)
NOW24_MAX_ITEMS: int = max(
    8, int(os.getenv("NOW24_MAX_ITEMS", "15") or "15")
)

# Минимум строк в выдаче «24 часа» (если после фильтров есть кандидаты).
NOW24_MIN_ITEMS: int = max(
    1, int(os.getenv("NOW24_MIN_ITEMS", "8") or "8")
)
# Минимум событий в weekly после отбора (backfill major events)
RADAR_WEEKLY_TARGET_MIN: int = max(
    8, int(os.getenv("RADAR_WEEKLY_TARGET_MIN", "10") or "10")
)

# Сколько телевизоров в баре — лимит параллельных эфиров в daily / now24
GASTROBAR_TV_COUNT: int = max(1, int(os.getenv("GASTROBAR_TV_COUNT", "2") or "2"))

# Football now24: минимальный football_watchability_score (API-SPORTS)
NOW24_FOOTBALL_MIN_WATCHABILITY: int = max(
    28, int(os.getenv("NOW24_FOOTBALL_MIN_WATCHABILITY", "38") or "38")
)
