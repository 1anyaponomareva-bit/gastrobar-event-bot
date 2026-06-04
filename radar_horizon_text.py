"""Тексты UI для горизонта афиши (RADAR_HORIZON_DAYS)."""

from __future__ import annotations

from config import RADAR_HORIZON_DAYS


def radar_horizon_days_ru() -> str:
    """«3 дня», «1 день», «5 дней»."""
    n = RADAR_HORIZON_DAYS
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return f"{n} дня"
    return f"{n} дней"


def radar_afisha_button_label() -> str:
    return f"📅 Афиша на {radar_horizon_days_ru()}"


def radar_afisha_section_title() -> str:
    return f"🔥 БЛИЖАЙШИЕ {radar_horizon_days_ru().upper()} В GASTROBAR"


def radar_afisha_menu_line() -> str:
    return (
        f"{radar_afisha_button_label()}\n"
        f"— главные события ближайших {radar_horizon_days_ru()}"
    )
