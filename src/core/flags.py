"""Runtime pipeline controls the dashboard can flip while the daemon runs.

Env settings give the defaults; the UI overrides live (stored in Redis so the
worker, discovery thread, and web server all see the change instantly).

  apply_mode: "gated" — every apply waits for your approval (default)
              "auto"  — jobs scoring ≥ auto_min_score apply THEMSELVES, up to
                        the daily cap; only real blockers (CAPTCHA, missing
                        fact, login) wait for you. Find-and-apply while asleep.
  paused:     freezes the worker + discovery without stopping the daemon.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from .config import get_settings
from .logging import get_logger

log = get_logger(__name__)

_KEY = "appliedin:flags"


@lru_cache
def _redis() -> Any:
    import redis

    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


def get_flag(name: str, default: str = "") -> str:
    try:
        v = _redis().hget(_KEY, name)
        return v if v is not None else default
    except Exception:  # flags are best-effort — never break the pipeline
        return default


def set_flag(name: str, value: str) -> None:
    try:
        _redis().hset(_KEY, name, value)
    except Exception:
        log.debug("set_flag failed", exc_info=True)


def apply_mode() -> str:
    """'gated' (approve each apply) or 'auto' (apply overnight up to the cap)."""
    mode = get_flag("apply_mode", get_settings().apply_mode).lower()
    return mode if mode in ("gated", "auto") else "gated"


def paused() -> bool:
    return get_flag("paused", "no") == "yes"
