"""Logging utilities for MentoroAI."""

from __future__ import annotations

import json
import logging
from typing import Any


class JsonFormatter(logging.Formatter):
    """Render log records as JSON strings.

    Django's default logging uses plaintext formatters which are hard to parse
    in structured log collectors. The production settings switch the console
    handler to this formatter to ensure logs contain timestamp, level and
    message fields. Optional exception information is added when present.
    """

    def format(self, record: logging.LogRecord) -> str:
        """
        Builds a dict from the LogRecord (including formatted time),
        conditionally attaches exc_info/stack, then json.dumps it;
        safe for production consoles and JSON log pipelines.
        """
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False)
