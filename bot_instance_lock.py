"""Один экземпляр бота: lock-файл с PID и проверка через psutil (Windows и др.)."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import psutil


def _read_lock_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw)
    except (OSError, ValueError):
        return None


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if not psutil.pid_exists(pid):
        return False
    try:
        return psutil.Process(pid).is_running()
    except psutil.NoSuchProcess:
        return False


def try_acquire_bot_lock(lock_path: Path, log: logging.Logger) -> bool:
    """
    Создаёт bot.lock с PID или отказывает, если жива другая копия.
    Возвращает True, если эта копия может работать.
    """
    while True:
        if not lock_path.exists():
            lock_path.write_text(str(os.getpid()), encoding="utf-8")
            log.info("created lock file")
            return True

        old_pid = _read_lock_pid(lock_path)
        if old_pid is None:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
            log.info("stale lock removed")
            continue

        if _process_is_alive(old_pid):
            log.info("existing bot process detected")
            print(
                "Bot is already running. Stop the previous process first.",
                file=sys.stderr,
            )
            return False

        try:
            lock_path.unlink()
        except OSError:
            pass
        log.info("stale lock removed")


def release_bot_lock(lock_path: Path, log: logging.Logger) -> None:
    try:
        if lock_path.is_file():
            lock_path.unlink()
            log.info("removed lock file")
    except OSError as e:
        log.warning("could not remove lock file: %s", e)
