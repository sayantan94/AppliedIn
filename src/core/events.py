"""Real-time activity events — what the agent is doing, streamed to the UI.

Every stage emits() an event (discovered, scoring, tailoring, browser action,
gate, applied…). Local mode publishes to a Redis pub/sub channel + keeps a
short history list; the web server streams both to the dashboard over SSE.
Cloud mode logs them (a WebSocket API would stream them there). Emitting never
raises — a telemetry hiccup must not break the pipeline.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from .config import get_settings
from .logging import get_logger

log = get_logger(__name__)

CHANNEL = "appliedin:events"
HISTORY = "appliedin:events:log"
HISTORY_MAX = 2000  # keep the full day's activity — logs must not disappear


@lru_cache
def _redis() -> Any:
    import redis

    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


def emit(kind: str, *, pk: str | None = None, detail: str = "", **extra: Any) -> None:
    """Publish one activity event. `kind` is a short verb: discovered, scoring,
    tailoring, critique, browsing, gate, applied, error, …"""
    event = {"kind": kind, "pk": pk, "detail": detail,
             "at": datetime.now(UTC).isoformat(), **extra}
    payload = json.dumps(event, default=str)
    # Durable diary of OUTCOMES (applied/needs-you/failed) — best-effort, ignores
    # non-outcome kinds internally, never raises.
    try:
        from .memory import remember
        remember(kind, pk=pk, detail=detail, at=event["at"],
                 company=str(extra.get("company", "")), title=str(extra.get("title", "")))
    except Exception:  # never let the diary break a run
        log.debug("memory hook failed", exc_info=True)
    try:
        if get_settings().mode == "local":
            r = _redis()
            pipe = r.pipeline()
            pipe.publish(CHANNEL, payload)
            pipe.lpush(HISTORY, payload)
            pipe.ltrim(HISTORY, 0, HISTORY_MAX - 1)
            pipe.execute()
        else:  # cloud: log (a WebSocket API streams these to Vercel later)
            log.info("event %s pk=%s %s", kind, pk, detail)
    except Exception:  # never let telemetry break the run
        log.debug("emit failed", exc_info=True)


def recent(limit: int = 50) -> list[dict]:
    """The most recent events, newest first (local mode)."""
    try:
        return [json.loads(x) for x in _redis().lrange(HISTORY, 0, limit - 1)]
    except Exception:
        return []
