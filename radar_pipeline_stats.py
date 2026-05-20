"""Счётчики и логи этапов weekly/now24 pipeline."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class PipelineStats:
    def __init__(self, *, label: str = "weekly") -> None:
        self.label = label
        self.counts: dict[str, int] = {}
        self.removals: list[tuple[str, str, str]] = []

    def set(self, stage: str, count: int) -> None:
        self.counts[stage] = count

    def removed(self, title: str, reason: str) -> None:
        self.removals.append((title, reason, ""))

    def flush_summary(self) -> None:
        parts = " ".join(f"{k}={v}" for k, v in self.counts.items())
        log.info("WEEKLY_PIPELINE [%s] %s", self.label, parts)
        for title, reason, _ in self.removals[:40]:
            log.info(
                "WEEKLY_PIPELINE REMOVED: event=%r reason=%s",
                (title or "")[:80],
                reason,
            )
        if len(self.removals) > 40:
            log.info(
                "WEEKLY_PIPELINE REMOVED: ... and %s more",
                len(self.removals) - 40,
            )
