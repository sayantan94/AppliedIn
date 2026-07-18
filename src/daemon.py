"""Local daemon — makes local mode behave like the cloud, always-on.

One process that does what cloud splits across managed triggers:
  • the WEB UI + API      (cloud: Vercel + API Gateway)  -> serves the dashboard
  • a discovery "cron"    (cloud: EventBridge)           -> find + enqueue
  • a queue "event source"(cloud: SQS -> agent.run)      -> apply per job

Same functions the cloud triggers call — only the scheduling differs. Run it:

    APPLIEDIN_MODE=local  (ANTHROPIC_API_KEY in .env)  uv run python -m daemon

then open http://127.0.0.1:8787
"""

from __future__ import annotations

import os
import threading
import time

from core.logging import get_logger
from core.stores import make_stores

log = get_logger(__name__)

DISCOVER_INTERVAL = int(os.environ.get("APPLIEDIN_DISCOVER_INTERVAL_SEC", str(6 * 3600)))
POLL_INTERVAL = int(os.environ.get("APPLIEDIN_POLL_INTERVAL_SEC", "5"))
WEB_PORT = int(os.environ.get("APPLIEDIN_WEB_PORT", "8787"))


def _discovery_loop() -> None:
    """The 'cron' — find + enqueue jobs on a schedule. Runs in its OWN thread so a
    long/expensive discovery cycle (many crawls) never blocks job processing."""
    from core import flags
    from discovery.handler import run_discovery

    last_discover = 0.0
    while True:
        now = time.monotonic()
        if not flags.paused() and now - last_discover >= DISCOVER_INTERVAL:
            try:
                log.info("discovery cycle: %s", run_discovery())
            except Exception:
                log.exception("discovery cycle failed")
            last_discover = now
        time.sleep(min(POLL_INTERVAL, 60))


def _sweep_found(stores) -> None:  # noqa: ANN001
    """AUTO mode only: when the queue is idle, feed a couple of waiting `found`
    jobs back into it so the backlog keeps progressing overnight. Stops feeding
    once today's applications hit the daily cap (jobs simply wait for tomorrow)."""
    from agent.run import _today_applied
    from core.config import get_settings
    from core.models import Status

    if _today_applied(stores) >= get_settings().daily_cap:
        return
    waiting = [r for r in stores.tracking.query_status(Status.FOUND)
               if not str(r.get("pk", "")).startswith("meta#")]
    for row in waiting[:2]:  # small batches — pace the LLM spend
        stores.queue.enqueue(stores.tailor_queue, {"pk": row["pk"]})
        log.info("sweep: queued waiting job %s", row["pk"])


def _worker_loop() -> None:
    """The 'event source' — drain the queue and run the pipeline per job. Its own
    thread, so a slow job (browser apply) never blocks discovery."""
    from agent.run import run_job
    from core import flags

    stores = make_stores()
    while True:
        if flags.paused():
            time.sleep(POLL_INTERVAL)
            continue
        items = stores.queue.drain(stores.tailor_queue)
        for item in items:
            try:
                log.info("pipeline: %s", run_job(item["pk"], stores))
            except Exception:
                log.exception("pipeline failed for %s", item.get("pk"))
        if not items and flags.apply_mode() == "auto":
            try:
                _sweep_found(stores)
            except Exception:
                log.exception("backlog sweep failed")
        time.sleep(POLL_INTERVAL)


def main() -> None:
    # Discovery and the pipeline worker run as SEPARATE background threads (so
    # neither blocks the other); the web server runs in the foreground.
    from server import serve

    stores = make_stores()
    if not hasattr(stores.queue, "drain"):
        raise SystemExit("daemon is for local mode; cloud uses EventBridge + SQS triggers.")

    discovery_on = os.environ.get("APPLIEDIN_DISCOVERY", "on").lower() not in (
        "off", "0", "false", "no")
    log.info("daemon up: worker + web%s (poll every %ds)",
             f" + discovery (every {DISCOVER_INTERVAL}s)" if discovery_on else " — DISCOVERY OFF",
             POLL_INTERVAL)
    if discovery_on:
        threading.Thread(target=_discovery_loop, daemon=True, name="discovery").start()
    threading.Thread(target=_worker_loop, daemon=True, name="worker").start()
    log.info("dashboard: http://127.0.0.1:%d", WEB_PORT)
    try:
        serve(port=WEB_PORT)
    except KeyboardInterrupt:
        log.info("daemon stopped")


if __name__ == "__main__":
    main()
