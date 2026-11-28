"""Structured application logging to a local file and the service journal."""

import json
import logging
import logging.handlers
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .certificate import utc


class EventFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "timestamp": utc(datetime.fromtimestamp(record.created, UTC)),
                "level": record.levelname.lower(),
                "message": record.getMessage(),
            }
        )


def progress(logger: logging.Logger, message: str, *arguments: Any) -> None:
    logger.info(message, *arguments, extra={"progress": True})


def configure_logging(directory: Path, name: str, *, terminal: bool = False) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        events = logging.handlers.WatchedFileHandler(directory / f"{name}.log")
        events.setFormatter(EventFormatter())
        logger.addHandler(events)

        console = logging.StreamHandler()
        if terminal:
            console.setFormatter(logging.Formatter("%(message)s"))
            console.addFilter(lambda record: getattr(record, "progress", False))
        else:
            console.setFormatter(EventFormatter())
        logger.addHandler(console)
    return logger
