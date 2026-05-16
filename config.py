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

GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

DATABASE_PATH: str = os.getenv("DATABASE_PATH", "gastrobar_bot.sqlite3").strip()
