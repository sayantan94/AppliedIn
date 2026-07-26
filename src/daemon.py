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

    # Start the clock now, not at zero. A zero here means the first tick is
    # already overdue, so every restart swept the whole watchlist immediately —
    # eight restarts in an afternoon meant eight full sweeps, racing whatever
    # single-company run the owner had actually asked for and burying the board
    # in jobs nobody requested. The schedule is a schedule, not a boot action.
    last_discover = time.monotonic()
    log.info("first scheduled discovery in %dh — use Discover to run one now",
             DISCOVER_INTERVAL // 3600)
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
    """When the evaluate queue is idle, feed waiting `found` jobs so the backlog
    keeps getting scored + tailored 24/7 — the whole backlog gets assessed
    continuously and the good ones queue up to apply."""
    from core.models import Status

    waiting = [r for r in stores.tracking.query_status(Status.FOUND)
               if not str(r.get("pk", "")).startswith("meta#")]
    for row in waiting[:3]:  # small batches — pace the LLM spend
        stores.queue.enqueue(stores.tailor_queue, {"pk": row["pk"]})
        log.info("sweep: queued waiting job %s", row["pk"])


# Concurrent score+tailor jobs. Evaluating is pure NETWORK wait (multi-turn LLM
# calls), not CPU, so serial evaluation was the pipeline's worst bottleneck: one
# slow tailor blocked the entire backlog behind it, and a discovery cycle that
# finds 150 relevant roles took hours to clear while the board sat on "tailoring 1".
# Four lanes scoring at once, while a relevance screen reads three hundred
# postings, is what exhausted a 500k tokens-per-minute budget. Two keeps the
# backlog moving without the pipeline spending its time backing off.
_EVAL_LANES = int(os.environ.get("APPLIEDIN_EVAL_LANES", "2"))


def _evaluate_one(pk: str) -> None:
    """Score + tailor ONE job. Each lane builds its OWN Stores bundle rather than
    sharing one across threads — the same rule the apply lanes follow."""
    from agent.run import run_job
    from core import flags

    stores = make_stores()
    if flags.paused():  # pause must interrupt a long batch, not just start one
        stores.queue.enqueue(stores.tailor_queue, {"pk": pk})
        return
    try:
        log.info("pipeline: %s", run_job(pk, stores))
    except Exception:
        # NEVER get stuck: mark the job errored so it leaves the `found`
        # pool and isn't re-swept into an infinite error loop. Move on.
        log.exception("pipeline failed for %s", pk)
        try:
            from core.models import Status
            stores.tracking.set_status(pk, Status.ERROR,
                                       error="pipeline error — see logs")
        except Exception:
            log.exception("could not mark %s errored", pk)


def _worker_loop() -> None:
    """EVALUATE worker — drain the tailor queue and score + tailor each job
    (LLM-only). Runs _EVAL_LANES jobs CONCURRENTLY so one slow tailor never
    stalls the backlog. Auto-approved jobs are handed to the apply queue, NOT
    applied here, so a slow browser apply never blocks evaluation. Runs 24/7."""
    from concurrent.futures import ThreadPoolExecutor

    from core import flags

    stores = make_stores()
    while True:
        if flags.paused():
            time.sleep(POLL_INTERVAL)
            continue
        items = stores.queue.drain(stores.tailor_queue)
        pks = [pk for it in items if (pk := it.get("pk"))]
        if pks:
            log.info("evaluate: %d job(s), %d lanes", len(pks), _EVAL_LANES)
            with ThreadPoolExecutor(max_workers=_EVAL_LANES,
                                    thread_name_prefix="eval") as pool:
                list(pool.map(_evaluate_one, pks))
        else:  # queue idle → keep the backlog moving (both modes)
            try:
                _sweep_found(stores)
            except Exception:
                log.exception("backlog sweep failed")
        time.sleep(POLL_INTERVAL)


def _apply_lanes() -> int:
    """How many applications may run at once.

    The Chrome engine acts in the owner's ONE browser, so lanes cannot be
    isolated the way separate Playwright profiles were: two sessions clicking in
    the same window interleave and both fail. Anything else keeps the old
    concurrency.
    """
    from core.config import get_settings

    return 1 if (get_settings().apply_engine or "").lower() == "chrome" else 3


_APPLY_LANES = _apply_lanes()


def _apply_loop() -> None:
    """APPLY worker — its OWN thread, so slow browser applies never block the
    evaluate worker. Submits up to _APPLY_LANES jobs CONCURRENTLY (each on its own
    Chrome profile). All applies run in ONE event loop (never one asyncio.run per
    thread — browser-use shares async objects that otherwise cross loops and
    crash). Pause-aware. Runs 24/7."""
    import asyncio

    from agent.run import _apply_direct
    from core import flags
    from core.config import get_settings
    from core.stores import make_stores as _mk
    from tools.browser_apply import set_profile_override

    stores = make_stores()
    base = (getattr(get_settings(), "browser_profile_dir", "") or "").strip()

    async def _apply_batch(pks: list) -> None:
        lanes: asyncio.Queue[str] = asyncio.Queue()
        for i in range(_APPLY_LANES):
            lanes.put_nowait(base if i == 0 else (f"{base}-{i}" if base else ""))

        async def _one(pk: str) -> None:
            lane = await lanes.get()  # blocks until a lane frees → caps concurrency
            try:
                set_profile_override(lane)  # task-local → this apply's Chrome profile
                log.info("apply: %s", await _apply_direct(pk, _mk()))
            except Exception:
                log.exception("apply failed for %s", pk)
            finally:
                lanes.put_nowait(lane)

        await asyncio.gather(*(_one(pk) for pk in pks))

    while True:
        if flags.paused():
            time.sleep(POLL_INTERVAL)
            continue
        items = stores.queue.drain(stores.apply_queue)
        if not items:
            time.sleep(POLL_INTERVAL)
            continue
        # Take only ONE lane-batch; re-queue the rest so they PERSIST in Redis
        # (never held in memory — a restart mustn't strand them). Next cycle pops
        # the next batch, re-checking pause each time.
        batch = items[:_APPLY_LANES]
        for rest in items[_APPLY_LANES:]:
            stores.queue.enqueue(stores.apply_queue, rest)
        try:
            asyncio.run(_apply_batch([it["pk"] for it in batch]))
        except Exception:
            log.exception("apply batch failed")
        time.sleep(POLL_INTERVAL)


def run_discovery_once(only: list[str] | None = None) -> dict:
    """ONE discovery cycle on demand (the UI 'Start discovery' button). `only`
    scopes it to the companies the user picked (None/empty = the whole
    watchlist). Finds + enqueues new jobs as `found`; does NOT score/tailor/apply
    — that's what `process_backlog_once` (the 'Process applications' button) does.
    Blocking — the web layer runs it in a background task so the click returns
    instantly."""
    from discovery.handler import run_discovery

    log.info("manual discovery requested (companies=%s)", only or "all")
    result = run_discovery(only=only)
    log.info("manual discovery: %s", result)
    return result


def process_backlog_once(companies: list | None = None) -> dict:
    """One on-demand pass over the discovered backlog (the UI 'Process
    applications' button): score + tailor EVERY waiting `found` job, then apply
    everything that qualifies — _APPLY_LANES at a time, up to the daily cap.
    ``companies`` (names, case-insensitive) scopes the pass to just those
    companies' jobs — everything else stays untouched in the backlog/queue for a
    later run. None/empty = the whole backlog.
    Self-contained (does its own draining), so it works whether or not the 24/7
    loops are running. Blocking — run it in a background thread."""
    import asyncio

    from agent.run import _apply_direct, run_job
    from core import flags as _flags
    from core.config import get_settings
    from core.models import Status
    from tools.browser_apply import set_profile_override

    stores = make_stores()
    sel = {c.strip().lower() for c in (companies or []) if c and c.strip()}
    # Un-scoped runs honor the owner's per-company skip toggles; an explicit
    # selection overrides a skip (picking a skipped company means it).
    skipped = set() if sel else _flags.skipped_companies()

    def _in_scope(company: str) -> bool:
        n = (company or "").strip().lower()
        return (n in sel) if sel else (n not in skipped)

    # 1) EVALUATE — score + tailor every waiting `found` job. Qualifying jobs are
    #    enqueued to the apply queue by run_job; low scorers are skipped there.
    found = [r for r in stores.tracking.query_status(Status.FOUND)
             if not str(r.get("pk", "")).startswith("meta#")]
    if sel or skipped:
        found = [r for r in found if _in_scope(r.get("company"))]
    evaluated = 0
    for row in found:
        try:
            log.info("process: %s", run_job(row["pk"], stores))
            evaluated += 1
        except Exception:
            log.exception("process: evaluate failed for %s", row.get("pk"))
            try:
                stores.tracking.set_status(row["pk"], Status.ERROR,
                                           error="pipeline error — see logs")
            except Exception:
                log.exception("could not mark %s errored", row.get("pk"))

    # 2) APPLY — drain the apply queue, _APPLY_LANES concurrently, until empty.
    base = (getattr(get_settings(), "browser_profile_dir", "") or "").strip()

    async def _apply_batch(pks: list) -> None:
        lanes: asyncio.Queue[str] = asyncio.Queue()
        for i in range(_APPLY_LANES):
            lanes.put_nowait(base if i == 0 else (f"{base}-{i}" if base else ""))

        async def _one(pk: str) -> None:
            lane = await lanes.get()
            try:
                set_profile_override(lane)
                log.info("process apply: %s", await _apply_direct(pk, make_stores()))
            except Exception:
                log.exception("process: apply failed for %s", pk)
            finally:
                lanes.put_nowait(lane)

        await asyncio.gather(*(_one(pk) for pk in pks))

    applied = 0
    while True:
        items = stores.queue.drain(stores.apply_queue)
        if not items:
            break
        if sel or skipped:
            # Scoped/skip-filtered run: apply only in-scope companies' jobs;
            # everything else goes straight back on the queue for a later pass.
            keep, defer = [], []
            for it in items:
                row = stores.tracking.get(it.get("pk", "")) or {}
                (keep if _in_scope(row.get("company")) else defer).append(it)
            for it in defer:
                stores.queue.enqueue(stores.apply_queue, it)
            if not keep:
                break  # only out-of-scope jobs remain — leave them queued
            items = keep
        batch = items[:_APPLY_LANES]
        for rest in items[_APPLY_LANES:]:
            stores.queue.enqueue(stores.apply_queue, rest)  # persist the overflow
        try:
            asyncio.run(_apply_batch([it["pk"] for it in batch]))
            applied += len(batch)
        except Exception:
            log.exception("process: apply batch failed")

    result = {"evaluated": evaluated, "applied": applied}
    log.info("process backlog done: %s", result)
    return result


def _recover_orphans(stores) -> None:  # noqa: ANN001
    """On startup, any SUBMITTING job is an ORPHAN — its browser apply was killed
    by the last restart, so it never finished. Reset it to TAILORED and re-queue it
    so the apply worker retries it. Nothing stays stuck in 'applying' forever."""
    from agent.run import release_claim
    from core.models import Status

    n = t = 0
    for r in stores.tracking.all():
        pk = r.get("pk", "")
        if str(pk).startswith("meta#"):
            continue
        st = r.get("status")
        if st == "submitting":
            stores.tracking.set_status(pk, Status.TAILORED)
            stores.queue.enqueue(stores.apply_queue, {"pk": pk})
            release_claim(pk, stores)
            n += 1
        elif st == "tailoring":  # score/tailor was killed mid-run → back to found
            stores.tracking.set_status(pk, Status.FOUND)
            release_claim(pk, stores)  # else every retry is refused until the TTL
            t += 1
    if n:
        log.info("recovered %d orphaned 'submitting' job(s) → re-queued to apply", n)
    if t:
        log.info("recovered %d orphaned 'tailoring' job(s) → reset to found", t)


def _heartbeat_loop() -> None:
    """Publish which worker loops are ALIVE, so a process without them can't
    masquerade as a healthy pipeline (`python -m server` serves the board and
    answers /stats while discovering, tailoring and applying exactly nothing).

    Runs in its OWN thread and only inspects thread liveness — never work
    progress — so a worker legitimately busy for ten minutes inside one browser
    apply still reads as alive."""
    from core import flags

    names = {"discovery", "evaluate", "apply"}
    while True:
        try:
            flags.beat_workers(sorted({t.name for t in threading.enumerate()} & names))
        except Exception:
            log.debug("heartbeat failed", exc_info=True)
        time.sleep(10)


def main() -> None:
    # Discovery and the pipeline worker run as SEPARATE background threads (so
    # neither blocks the other); the web server runs in the foreground.
    from server import serve

    stores = make_stores()
    if not hasattr(stores.queue, "drain"):
        raise SystemExit("daemon is for local mode; cloud uses EventBridge + SQS triggers.")

    _recover_orphans(stores)  # never leave a killed apply stuck at 'submitting'
    discovery_on = os.environ.get("APPLIEDIN_DISCOVERY", "on").lower() not in (
        "off", "0", "false", "no")
    log.info("daemon up: evaluate + apply workers + web%s (poll every %ds)",
             f" + discovery (every {DISCOVER_INTERVAL}s)" if discovery_on else " — DISCOVERY OFF",
             POLL_INTERVAL)
    if discovery_on:
        threading.Thread(target=_discovery_loop, daemon=True, name="discovery").start()
    threading.Thread(target=_worker_loop, daemon=True, name="evaluate").start()
    threading.Thread(target=_apply_loop, daemon=True, name="apply").start()
    threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat").start()
    log.info("dashboard: http://127.0.0.1:%d", WEB_PORT)
    try:
        serve(port=WEB_PORT)
    except KeyboardInterrupt:
        log.info("daemon stopped")


if __name__ == "__main__":
    main()
