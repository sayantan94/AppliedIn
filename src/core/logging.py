"""Logging — readable on your Mac, JSON in the cloud.

Local mode prints clean, colored, human-readable lines (this is a tool you
watch run). Cloud mode emits JSON so CloudWatch can parse it. Errors always
include the traceback.
"""

from __future__ import annotations

import json
import logging
import os
import sys

_COLORS = {"DEBUG": "\033[2;37m", "INFO": "\033[36m", "WARNING": "\033[33m",
           "ERROR": "\033[1;31m", "CRITICAL": "\033[1;41m"}
_RESET = "\033[0m"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"level": record.levelname, "logger": record.name,
                   "message": record.getMessage()}
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


class _ConsoleFormatter(logging.Formatter):
    """`HH:MM:SS LEVEL  short.logger  message` — colored, with tracebacks."""

    def __init__(self) -> None:
        super().__init__(datefmt="%H:%M:%S")
        self._color = sys.stdout.isatty()

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, self.datefmt)
        name = record.name.split(".")[0]  # discovery, agent, tools…
        msg = record.getMessage()
        line = f"{ts} {record.levelname:<7} {name:<10} {msg}"
        if self._color:
            c = _COLORS.get(record.levelname, "")
            line = f"\033[2m{ts}{_RESET} {c}{record.levelname:<7}{_RESET} \033[2m{name:<10}{_RESET} {msg}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        cloud = os.environ.get("APPLIEDIN_MODE", "local") == "cloud"
        handler.setFormatter(_JsonFormatter() if cloud else _ConsoleFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
