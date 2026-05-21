"""Единое логирование ошибок и тексты для Telegram."""

from __future__ import annotations

import logging
from typing import Any

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

log = logging.getLogger(__name__)


def configure_logging(level: int = logging.INFO) -> None:
    """Идемпотентная настройка root logger (local + Railway)."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level, format=LOG_FORMAT)
    else:
        root.setLevel(level)
        for handler in root.handlers:
            handler.setFormatter(logging.Formatter(LOG_FORMAT))


def format_telegram_exception(exc: BaseException) -> str:
    return f"Ошибка:\n{type(exc).__name__}: {str(exc)[:500]}"


def log_exception(context: str, *, exc: BaseException | None = None) -> None:
    """Полный traceback в лог (logger.exception)."""
    if exc is not None:
        log.exception("%s: %s", context, exc, exc_info=exc)
    else:
        log.exception(context, exc_info=True)


def log_unexpected_resolve(
    *,
    fetch_note: str | None,
    raw_total: int,
    prelim_count: int,
    selected: int,
    extra: dict[str, Any] | None = None,
) -> None:
    log.error(
        "resolve_radar_error_code -> unexpected_error "
        "fetch_note=%r raw_total=%s prelim_count=%s selected=%s extra=%s",
        fetch_note,
        raw_total,
        prelim_count,
        selected,
        extra or {},
        stack_info=True,
    )
