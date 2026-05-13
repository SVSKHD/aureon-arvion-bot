"""Logger setup. Normal buffered logging — no forced flushing."""

import logging
from pathlib import Path

import config

LOGGER_NAME = "anchor_bot"

# Resolve LOG_FILE relative to project root (where config.py lives),
# not the current working directory — so running the bot from any folder
# always writes logs to the same place.
_PROJECT_ROOT = Path(config.__file__).resolve().parent


def _resolve_path(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else _PROJECT_ROOT / path


def setup_logger(name: str = LOGGER_NAME) -> logging.Logger:
    """Configure logger with file + stream handlers. Returns ready-to-use logger."""
    log_path = _resolve_path(config.LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    # Avoid duplicate handlers on re-init
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    return logger


def get_logger(name: str = LOGGER_NAME) -> logging.Logger:
    """Return the configured logger (set up via setup_logger first)."""
    return logging.getLogger(name)
