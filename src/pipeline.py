"""The top-level AppliedIn pipeline — the whole flow, one call.

    find + crawl  ->  enqueue  ->  ADK apply pipeline  ->  applied / gated

`run_once` does a full pass; the daemon (local) and the SQS/EventBridge triggers
(cloud) both compose these same two halves. Mode-agnostic: everything comes
from the store factory, so this runs identically on a Mac or on AWS.
"""

from __future__ import annotations

from typing import Any

from core.logging import get_logger
from core.stores import make_stores

log = get_logger(__name__)


def find(stores: Any = None) -> dict:
    """Discover + crawl new jobs across the watchlist and enqueue them."""
    from discovery.handler import run_discovery

    return run_discovery()


def apply_queued(stores: Any = None) -> list[dict]:
    """Drain the queue and run the ADK apply pipeline for each job (local mode)."""
    from agent.run import run_job

    stores = stores or make_stores()
    if not hasattr(stores.queue, "drain"):
        raise RuntimeError("apply_queued is for local mode; cloud uses the SQS event source.")
    return [run_job(item["pk"], stores) for item in stores.queue.drain(stores.tailor_queue)]


def run_once(stores: Any = None) -> dict:
    """One full pass: find + crawl -> queue -> apply everything queued."""
    stores = stores or make_stores()
    found = find(stores)
    applied = apply_queued(stores)
    result = {"found": found, "applied": applied}
    log.info("pipeline run_once: %s", result)
    return result
