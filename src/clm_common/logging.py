"""Structured application logging to a local file and the service journal."""

import json
import logging
import logging.handlers
from datetime import UTC, datetime
from pathlib import Path

from .certificate import utc


class EventFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "timestamp": utc(datetime.fromtimestamp(record.created, UTC)),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        })


def configure_logging(directory: Path, name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        formatter = EventFormatter()
        for handler in (
            logging.handlers.WatchedFileHandler(directory / f"{name}.log"),
            logging.StreamHandler(),
        ):
            handler.setFormatter(formatter)
            logger.addHandler(handler)
    return logger
