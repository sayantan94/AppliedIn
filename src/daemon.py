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


def loop() -> None:
    from agent.run import run_job
    from discovery.handler import run_discovery

    stores = make_stores()
    if not hasattr(stores.queue, "drain"):
        raise SystemExit("daemon is for local mode; cloud uses EventBridge + SQS triggers.")

    log.info("daemon up: discover every %ds, poll every %ds", DISCOVER_INTERVAL, POLL_INTERVAL)
    last_discover = 0.0
    while True:
        now = time.monotonic()
        if now - last_discover >= DISCOVER_INTERVAL:  # the "cron"
            try:
                log.info("discovery cycle: %s", run_discovery())
            except Exception:
                log.exception("discovery cycle failed")
            last_discover = now

        for item in stores.queue.drain(stores.tailor_queue):  # the "event source"
            try:
                log.info("pipeline: %s", run_job(item["pk"], stores))
            except Exception:
                log.exception("pipeline failed for %s", item.get("pk"))

        time.sleep(POLL_INTERVAL)


def main() -> None:
    # Pipeline (find + apply) runs in the background; the web server (dashboard +
    # API) runs in the foreground so the UI fetches live data from the store.
    from server import serve

    threading.Thread(target=loop, daemon=True).start()
    log.info("dashboard: http://127.0.0.1:%d", WEB_PORT)
    try:
        serve(port=WEB_PORT)
    except KeyboardInterrupt:
        log.info("daemon stopped")


if __name__ == "__main__":
    main()
