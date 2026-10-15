"""Loguru configuration: human-readable stderr plus an optional JSON-lines sink.

Callers must never log comment bodies or evidence quotes; log identifiers and counts only.
"""

from __future__ import annotations

import sys

from loguru import logger

from crop_explorer.config import LoggingConfig


def configure_logging(cfg: LoggingConfig) -> None:
    """Replace loguru's default handler with the configured sinks."""
    logger.remove()
    logger.add(sys.stderr, level=cfg.level)
    if cfg.json_path is not None:
        cfg.json_path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(cfg.json_path, level=cfg.level, serialize=True)
