"""Slow down when the API says to, across every caller at once.

A tokens-per-minute limit is shared by the whole process, so retrying inside one
call does not help: four evaluate lanes each retrying independently hit the same
ceiling four times over, and a relevance screen reading three hundred postings
walks into it again the moment the retries finish. What is needed is not a longer
retry, it is everybody waiting.

So the brake is global. A rate limit anywhere pauses every caller for a moment,
and that moment doubles while limits keep arriving and decays once they stop. It
hangs off litellm's own callbacks, which means it covers our completion() calls
and the ADK's alike without any call site knowing it is there.
"""

from __future__ import annotations

import threading
import time

from .logging import get_logger

log = get_logger(__name__)

# How long everyone waits after the first rate limit, and the ceiling that
# doubling stops at. Four seconds is long enough for a per-minute budget to free
# up meaningfully; a minute is long enough that something is wrong if we reach it.
_BASE_COOLDOWN_S = 4.0
_MAX_COOLDOWN_S = 60.0

# Stop doubling once limits stop arriving: a burst should not leave the pipeline
# crawling for the rest of the run.
_DECAY_AFTER_S = 120.0

_lock = threading.Lock()
_resume_at = 0.0        # wall-clock time when calls may proceed
_cooldown = 0.0         # current penalty, doubles per rate limit
_last_limit = 0.0       # when we last saw one


def wait() -> None:
    """Block until the API is likely to accept a call. Usually returns at once."""
    while True:
        with _lock:
            remaining = _resume_at - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 1.0))


def note_rate_limit() -> float:
    """Record a rate limit and put everyone on hold. Returns the new cooldown."""
    global _resume_at, _cooldown, _last_limit

    with _lock:
        now = time.monotonic()
        if _last_limit and now - _last_limit > _DECAY_AFTER_S:
            _cooldown = 0.0     # the burst passed; start over rather than punish
        _cooldown = min(_cooldown * 2 if _cooldown else _BASE_COOLDOWN_S,
                        _MAX_COOLDOWN_S)
        _last_limit = now
        _resume_at = max(_resume_at, now + _cooldown)
        current = _cooldown
    log.warning("rate limited — holding every model call for %.0fs", current)
    return current


def state() -> dict:
    """For the dashboard: whether we are backing off, and by how much."""
    with _lock:
        return {"cooling_down": _resume_at > time.monotonic(),
                "seconds_left": max(0.0, round(_resume_at - time.monotonic(), 1)),
                "current_penalty": round(_cooldown, 1)}


def install() -> None:
    """Attach the brake to litellm, so every caller inherits it."""
    try:
        import litellm
        from litellm.integrations.custom_logger import CustomLogger
    except Exception:  # noqa: BLE001 — never block startup on the LLM library
        return

    class _Brake(CustomLogger):
        def log_pre_api_call(self, model, messages, kwargs):  # noqa: ANN001, ANN201, ARG002
            wait()

        async def async_log_pre_api_call(self, model, messages, kwargs):  # noqa: ANN001, ANN201, ARG002
            wait()

        def log_failure_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001, ANN201, ARG002
            if _is_rate_limit(kwargs):
                note_rate_limit()

        async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001, ANN201, ARG002
            if _is_rate_limit(kwargs):
                note_rate_limit()

    if not any(type(cb).__name__ == "_Brake" for cb in (litellm.callbacks or [])):
        litellm.callbacks = [*(litellm.callbacks or []), _Brake()]


def _is_rate_limit(kwargs: dict) -> bool:
    exc = (kwargs or {}).get("exception")
    name = type(exc).__name__ if exc else ""
    return "ratelimit" in name.lower() or "rate limit" in str(exc).lower()
