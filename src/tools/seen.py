"""Persistent record of JD URLs already handled — so re-running discovery
doesn't process the same posting again.

The tracking store already dedups by job id, but that lives in Redis; this is a
plain ``.local/seen.json`` that survives a Redis flush / machine restart. The
dashboard's Reset clears it for a true fresh start.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger

log = get_logger(__name__)


def _path() -> Path:
    return Path(get_settings().local_dir) / "seen.json"


def load() -> set[str]:
    p = _path()
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text()))
    except Exception:
        return set()


def mark_all(urls: list[str]) -> None:
    """Record these JD URLs as processed (merges with what's already there)."""
    fresh = {u for u in urls if u}
    if not fresh:
        return
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(load() | fresh)))


def clear() -> None:
    _path().unlink(missing_ok=True)
