"""Local daemon — makes local mode behave like the cloud, always-on.

One process that does what cloud splits across managed triggers:
  • the WEB UI + API      (cloud: Vercel + API Gateway)  -> serves the dashboard
  • a discovery "cron"    (cloud: EventBridge)           -> find + enqueue
  • a queue "event source"(cloud: SQS -> agent.run)      -> apply per job

Same functions the cloud triggers call — only the scheduling differs. Run it:

    APPLIEDIN_MODE=local  (OPENAI_API_KEY in .env)  uv run python -m daemon

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
    # Checked per job, not just before the drain. drain() empties the queue, so
    # one batch can be thousands of jobs, and pausing during it did nothing: the
    # pool worked through the whole batch while the board read paused.
    from core import flags as _flags

    if _flags.paused():
        return

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




_LAST_RECLAIM = [0.0]
RECLAIM_EVERY_S = 120


def _reclaim_orphans(stores, q) -> None:  # noqa: ANN001
    """Re-queue applications the store calls in flight that nobody is running.

    Startup recovery is not enough on its own: it only catches jobs orphaned by
    the LAST restart. A worker that dies mid run, a browser session killed by
    hand, or a pause landing between the status write and the apply leaves a job
    reading `submitting` for good, and the board shows it as in flight forever
    while nothing will ever pick it up. That is worse than a visible failure,
    because it looks like progress.

    The lease is the authority on what is truly running, which is why it records
    the pk: a company lease alone cannot say WHICH of two jobs at one employer is
    the live one.
    """
    now = time.monotonic()
    if now - _LAST_RECLAIM[0] < RECLAIM_EVERY_S:
        return
    _LAST_RECLAIM[0] = now

    from core.models import Status

    live = q.in_flight()
    n = 0
    for r in stores.tracking.all():
        pk = str(r.get("pk", ""))
        if pk.startswith("meta#") or r.get("status") != Status.SUBMITTING.value:
            continue
        if pk in live:
            continue                       # genuinely being filled right now
        stores.tracking.set_status(pk, Status.TAILORED, gate_reason="approval")
        q.put(pk, r.get("company", ""))
        n += 1
    if n:
        log.warning("reclaimed %d application(s) that were marked in flight with no "
                    "worker running them", n)


def auto_dispatch_allowed(mode: str) -> bool:
    """Whether the apply worker may take work off the queue by itself.

    The tailor now puts every finished job in the queue, so draining it
    automatically in GATED mode would submit applications nobody approved — the
    queue would have quietly become an auto-apply pipeline. In gated mode it is a
    waiting room the owner empties with Process, per company.

    `assisted` is excluded for a different reason: those applications are finished
    by hand in the owner's own browser, so there is nothing for the worker to do.
    """
    return mode == "auto"


def _apply_loop() -> None:
    """APPLY worker — its own thread, so a slow browser apply never blocks the
    evaluate worker.

    Runs up to _APPLY_CONCURRENCY applications at once, never two to the same
    company: the queue leases a company while one of its jobs is in flight. Work
    starts the moment a slot frees, with no pacing.

    A retryable failure goes back with backoff; a terminal one never does. See
    core.apply_queue — an unconfirmed submit in particular must reach the owner
    rather than be retried, since it may already have gone through.
    """
    from concurrent.futures import ThreadPoolExecutor

    from agent.run import run_queued
    from core import flags
    from core.apply_queue import ApplyQueue

    stores = make_stores()
    q = ApplyQueue(stores.tracking.r)
    # Leases describe what is running NOW, and nothing is running in a process
    # that has just started. Without this a crash mid apply would block that
    # company for good.
    if (freed := q.reset_leases()):
        log.info("released %d stale company lease(s) from a previous run", freed)

    def _run_one(item: dict) -> None:
        run_queued(item, q)   # shared with the dashboard's per company drain

    # Sized to the ceiling; the live flag is enforced per dispatch below, so
    # turning concurrency down takes effect on the next application rather than
    # needing a restart.
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="apply") as pool:
        running: set = set()
        while True:
            running = {f for f in running if not f.done()}
            if flags.paused():
                time.sleep(POLL_INTERVAL)
                continue

            # Fold in anything the old flat queue still holds, so an upgrade never
            # strands work someone already approved.
            for it in stores.queue.drain(stores.apply_queue):
                if (pk := it.get("pk")):
                    q.put(pk, (stores.tracking.get(pk) or {}).get("company", ""))

            # Recovery runs FIRST and in every mode. It used to sit after the
            # dispatch checks, so adding the gated guard above it stranded every
            # orphan in gated mode: a job whose worker was killed stayed leased,
            # left the queue, and nothing ever put it back. That is the failure
            # that looks like a company quietly disappearing from the queue.
            _reclaim_orphans(stores, q)
            if not auto_dispatch_allowed(flags.apply_mode()):
                time.sleep(POLL_INTERVAL)
                continue
            if len(running) >= flags.apply_concurrency():
                time.sleep(POLL_INTERVAL)      # at the limit; let one finish
                continue
            item = q.next()
            if not item:
                time.sleep(POLL_INTERVAL)
                continue
            running.add(pool.submit(_run_one, item))


def run_discovery_once(only: list[str] | None = None, profile_id: str = "",
                       max_age_hours: float | int | None = None) -> dict:
    """ONE discovery cycle on demand (the UI 'Start discovery' button). `only`
    scopes it to the companies the user picked (None/empty = the whole
    watchlist). `profile_id` stamps every job this run finds with the identity it
    should be sent under, so a batch can go out from one address without setting
    it per job; the UI's "apply as" selector passes it. Finds + enqueues new jobs
    as `found`; does NOT score/tailor/apply — that's what `process_backlog_once`
    (the 'Process applications' button) does. Blocking — the web layer runs it in
    a background task so the click returns instantly."""
    from discovery.handler import run_discovery

    log.info("manual discovery requested (companies=%s, profile=%s)",
             only or "all", profile_id or "default")
    # manual=True: the owner is pressing a button right now, so a board left
    # paused must not veto it. Only a Stop asserted after this call ends the run.
    result = run_discovery(only=only, profile_id=profile_id,
                           max_age_hours=max_age_hours, manual=True)
    log.info("manual discovery: %s", result)
    return result


def _pass_cancelled(manual: bool = False, epoch0: int = 0) -> bool:
    """True when this pass should stop, checked per job.

    A full pass is hours of model work over the whole backlog. Reading the flag
    only at the start meant Pause and Stop could not reach one already going.

    Same split as discovery's `_stop_requested`, and for the same reason: every
    caller of `process_backlog_once` is a button, so reading `paused` here meant
    a paused board took the press, returned ok, and broke before job 1 with 1341
    jobs waiting. A manual pass stops only for a Stop asserted after it began.

    `_RUNNING["process"]` is deliberately NOT read as a stop signal any more. It
    is one boolean written by four endpoints (process, run-company, tailor-text,
    apply-role), so a tailor-text finishing next to a running pass cleared it and
    silently aborted the pass mid backlog. /actions/stop bumps the epoch, which
    is the signal that actually means the owner asked for a stop.
    """
    try:
        from core import flags as _flags

        if manual:
            return _flags.stop_epoch() != epoch0
        return bool(_flags.paused())
    except Exception:  # noqa: BLE001 - a stop check must never break a pass
        return False


def process_backlog_once(companies: list | None = None,
                         manual: bool = False) -> dict:
    """One on-demand pass over the discovered backlog (the UI 'Process
    applications' button): score + tailor EVERY waiting `found` job, then apply
    everything that qualifies — queued for the apply worker, which runs them
    a few at a time and never two to one company.
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
    # Captured before the first job, so only a Stop pressed after this point ends
    # the pass. A board already paused does not.
    epoch0 = _flags.stop_epoch()
    for row in found:
        if _pass_cancelled(manual, epoch0):
            log.info("process: stopped by the owner after %d job(s)", evaluated)
            break
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

    # 2) APPLY — hand everything to the SAME queue the apply worker drains, rather
    # than applying here. Two code paths applying concurrently was the actual
    # concurrency bug: each respected its own limit and neither knew about the
    # other, so a process run and the worker could drive the same company at once.
    from core import flags as _f
    from core.apply_queue import ApplyQueue

    q = ApplyQueue(stores.tracking.r)
    applied = 0
    for it in stores.queue.drain(stores.apply_queue):
        pk = it.get("pk", "")
        row = stores.tracking.get(pk) or {}
        if sel or skipped:
            # Scoped run: out of scope jobs go back untouched for a later pass.
            if not _in_scope(row.get("company")):
                stores.queue.enqueue(stores.apply_queue, it)
                continue
        q.put(pk, row.get("company", ""))
        applied += 1
    if applied:
        log.info("process: queued %d application(s) — the apply worker drains them "
                 "%d at a time, never two to one company", applied,
                 _f.apply_concurrency())

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
