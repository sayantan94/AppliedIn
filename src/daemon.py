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

import math
import os
import threading
import time
from typing import Any
import sys

# A typo or extra argument in a synthetic-only invocation must NEVER fall
# through into the production worker/dashboard startup. Library imports are
# unaffected, and the ordinary no-argument daemon still starts as before.
if __name__ == "__main__" and sys.argv[1:] not in (
        [], ["--synthetic-browser-fixture"]):
    raise SystemExit(
        "Unsupported daemon arguments: use 'python -m daemon' with no arguments "
        "or exactly 'python -m daemon --synthetic-browser-fixture'.")

# The one-shot fixture must exit BEFORE core.stores imports core.config, dotenv
# and LiteLLM. Every normal -m daemon and import daemon retains the old globals.
if __name__ == "__main__" and sys.argv[1:] == ["--synthetic-browser-fixture"]:
    _fixture_keys = (
        "HOME", "PATH", "VIRTUAL_ENV", "XDG_RUNTIME_DIR",
        "BSK_HOME", "APPLIEDIN_CONFIG_DIR", "LANG", "LC_ALL",
    )
    _fixture_environment = {k: os.environ[k] for k in _fixture_keys
                            if k in os.environ and os.environ[k]}
    os.environ.clear()
    os.environ.update(_fixture_environment)
    os.environ.update(PYTHON_DOTENV_DISABLED="1",
                      LITELLM_LOCAL_MODEL_COST_MAP="True",
                      BSK_AUTO_START="0")
    from tools.browser_skill_fixture import run as _run_synthetic_fixture

    raise SystemExit(_run_synthetic_fixture())

from core.logging import get_logger
from core.stores import make_stores
from core.ids import is_internal_pk

log = get_logger(__name__)

DISCOVER_INTERVAL = int(os.environ.get("APPLIEDIN_DISCOVER_INTERVAL_SEC", str(6 * 3600)))
POLL_INTERVAL = int(os.environ.get("APPLIEDIN_POLL_INTERVAL_SEC", "5"))
WEB_PORT = int(os.environ.get("APPLIEDIN_WEB_PORT", "8787"))


def _discovery_due(last_raw: str, now: float) -> float:
    """Wall-clock time the next scheduled discovery cycle is due.

    One interval after the persisted cycle start, so an overdue cycle is due at
    once and a recent one waits only for the remainder. An absent, unparsable or
    non-finite value counts as absent (one interval after `now`, the behavior
    before persistence existed); a value in the future is clamped to `now`."""
    try:
        last = float(last_raw)
    except (TypeError, ValueError):
        return now + DISCOVER_INTERVAL
    if not math.isfinite(last):
        return now + DISCOVER_INTERVAL
    return min(last, now) + DISCOVER_INTERVAL


def _discovery_loop() -> None:
    """The 'cron' — find + enqueue jobs on a schedule. Runs in its OWN thread so a
    long/expensive discovery cycle (many crawls) never blocks job processing."""
    from core import flags
    from discovery.handler import run_discovery

    # Resume the persisted schedule, never a boot action. Starting the clock at
    # zero made every restart sweep the whole watchlist at once — eight restarts
    # in an afternoon meant eight full sweeps, racing whatever single-company run
    # the owner had actually asked for. Starting it at boot instead made the
    # opposite error: a daemon restarted every few hours never reached the
    # interval, so the scheduled cycle never ran. The due time is read ONCE into
    # memory and advanced in memory, so a failed persistence write cannot make
    # the cycle repeat every tick.
    next_due = _discovery_due(flags.get_flag("last_discovery_at"), time.time())
    log.info("next scheduled discovery in %dm — use Discover to run one now",
             max(0, int(next_due - time.time())) // 60)
    while True:
        now = time.time()
        try:
            from discovery.career_ops import scheduled_tick
            scheduled_tick()
        except Exception:
            log.exception("Career Ops schedule check failed")
        if not flags.paused() and now >= next_due:
            next_due = now + DISCOVER_INTERVAL
            flags.set_flag("last_discovery_at", repr(now))
            try:
                log.info("discovery cycle: %s", run_discovery())
            except Exception:
                log.exception("discovery cycle failed")
        time.sleep(min(POLL_INTERVAL, 60))


def _sweep_found(stores) -> None:  # noqa: ANN001
    """When the evaluate queue is idle, feed waiting `found` jobs so the backlog
    keeps getting scored + tailored 24/7 — the whole backlog gets assessed
    continuously and the good ones queue up to apply."""
    from core.models import Status
    from core import flags
    from agent.run import _reader_eligible

    skipped = flags.skipped_companies()
    waiting = [r for r in stores.tracking.query_status(Status.FOUND)
               if r.get("company", "").strip().lower() not in skipped
               and not is_internal_pk(r.get("pk", "")) and _reader_eligible(r)]
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

    from agent.run import _reader_patch, run_job
    from core import flags

    stores = make_stores()
    if flags.paused():  # pause must interrupt a long batch, not just start one
        stores.queue.enqueue(stores.tailor_queue, {"pk": pk})
        return
    admitted = None
    try:
        admitted = stores.tracking.get(pk)
        # Re-read the skip for queued work too: a cap can be exhausted after sweep.
        # Leave the row found so removing the lead's skip resumes normal evaluation.
        if admitted and admitted.get("company", "").strip().lower() in flags.skipped_companies():
            return
        log.info("pipeline: %s", run_job(pk, stores))
    except Exception:
        # NEVER get stuck: mark the job errored so it leaves the `found`
        # pool and isn't re-swept into an infinite error loop. Move on.
        log.exception("pipeline failed for %s", pk)
        try:
            from core.models import Status
            if admitted is not None:
                _reader_patch(pk, admitted, stores, {
                    "status": Status.ERROR, "error": "pipeline error — see logs"})
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


LEASE_GRACE_S = 20 * 60      # a re-tailor is ~2 min; a model call stuck on a rate limit is not
_LEASE_SEEN: dict = {}       # pk -> when a lease was first seen with no application behind it


def _hold_possible_submission(stores, row) -> bool:  # noqa: ANN001
    """Independent pre-click evidence survives stale full-row tracking writes."""
    from tools.submit_hold import blocked
    if not blocked(row['pk'], row, tracking=stores.tracking):
        return False
    from core.models import Status

    stores.tracking.set_status(
        row["pk"], Status.NEEDS_HUMAN, gate_reason="submit_uncertain",
        fail_kind="uncertain", fail_reason="possible submission; check the employer portal before retrying",
        gate_pending={"question": "possible submission; check the employer portal before retrying"})
    return True


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
    rows = stores.tracking.all()
    by_pk = {str(r.get("pk", "")): r for r in rows}
    for r in rows:
        pk = str(r.get("pk", ""))
        if is_internal_pk(pk) or r.get("status") != Status.SUBMITTING.value:
            continue
        if pk in live:
            continue                       # genuinely being filled right now
        if _hold_possible_submission(stores, r):
            continue  # no automatic second application after a crash
        stores.tracking.set_status(pk, Status.TAILORED, gate_reason="approval")
        q.put(pk, r.get("company", ""))
        n += 1
    if n:
        log.warning("reclaimed %d application(s) that were marked in flight with no "
                    "worker running them", n)

    # The mirror case: a LEASE with no application behind it. An Apply press
    # re-tailors a stale row before applying and holds the company's lease while
    # it does; a model call that never returns then holds it forever, the row
    # reads `tailoring` for hours, and every other job at that company waits on
    # a ghost — Netflix sat like that for three and a half hours. A lease whose
    # row is not `submitting` gets a grace period for the re-tailor, then goes.
    for pk in list(live):
        r = by_pk.get(pk)
        st = str((r or {}).get("status") or "")
        if st == Status.SUBMITTING.value:
            _LEASE_SEEN.pop(pk, None)
            continue
        first = _LEASE_SEEN.setdefault(pk, now)
        if now - first < LEASE_GRACE_S:
            continue
        company = (r or {}).get("company", "") or pk.split("#", 1)[0]
        q.done({"pk": pk, "company": company})
        _LEASE_SEEN.pop(pk, None)
        if r and st == Status.TAILORING.value:
            stores.tracking.set_status(pk, Status.FOUND, gate_reason="", fail_reason="")
        log.warning("released %s's lease held by %s for %d min with no application "
                    "running (row was %r)", company, pk, int((now - first) / 60), st)


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


def next_dispatchable(q: Any, mode: str) -> dict | None:
    """The next application this worker may start, given the board's mode.

    In gated (and assisted) mode this is restricted to an application the owner
    has just answered a question for. Everything else keeps waiting for Process,
    which is the whole point of gated mode: the tailor queues every finished job,
    so draining the queue by itself would submit applications nobody approved.

    An answered question is not that. That job was already started by the owner,
    the browser already filled the form, and it stopped on a single field with no
    approved answer. Continuing it authorises nothing new — and leaving it to sit
    is what made answering a gate look like it did nothing at all.
    """
    return q.next(priority_only=not auto_dispatch_allowed(mode))


_RELEASE_CHECK_SECONDS = 30.0


def _release_auto_pause_if_connected() -> bool:
    """Lift the system's own browser-disconnect pause once the browser is back.

    Read-only toward the browser (`connection_status` never launches anything) and
    limited to a pause the system marked as its own while the configured engine is
    BrowserSkill, the only engine with a connection witness. Anything else, or any
    fault while checking, releases nothing; a manual pause carries no marker."""
    from core import flags

    if flags.get_flag("paused_auto") != flags.AUTO_PAUSE_MARKER:
        return False
    try:
        from tools import browser_profile, browser_runtime

        if browser_runtime.configuration()["engine"] != "browser_skill":
            return False
        if browser_profile.connection_status().get("state") != "connected":
            return False
    except Exception:
        log.debug("auto-pause release check failed", exc_info=True)
        return False
    if flags.release_auto_pause():
        log.info("RESUMED applying: the browser is connected again")
        return True
    return False


def paused_dispatchable(q: Any, reason: str) -> dict | None:
    """What may start while applying is paused: only roles the owner pressed Apply on.

    Pressing Apply is a manual instruction for that exact role, and it has always
    run through an owner's pause (see `career_apply.run_selected`). Leaving it to
    the pause meant a browser fault mid run parked it until someone approved it
    again, which is how two roles sat for twelve days waiting on a question
    nobody was asked. A pause the SYSTEM set still holds: `reason` is set only for
    a disconnected extension or a signed-out CLI, and every attempt would fail.
    """
    if reason:
        return None
    return q.next(requested_only=True)


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
        last_release_check = float("-inf")
        while True:
            running = {f for f in running if not f.done()}
            # Fold in anything the old flat queue still holds, so an upgrade never
            # strands work someone already approved. It runs while paused too: a
            # role requeued after a browser fault arrives here, and it must keep
            # the mark that the owner asked for it.
            for it in stores.queue.drain(stores.apply_queue):
                if (pk := it.get("pk")):
                    row = stores.tracking.get(pk) or {}
                    q.put(pk, row.get("company", ""),
                          requested=bool(row.get("apply_requested_at")))
            if flags.paused():
                if time.monotonic() - last_release_check >= _RELEASE_CHECK_SECONDS:
                    last_release_check = time.monotonic()
                    if _release_auto_pause_if_connected():
                        continue
                item = None
                if len(running) < flags.apply_concurrency():
                    item = paused_dispatchable(q, flags.get_flag("paused_reason"))
                if item:
                    running.add(pool.submit(_run_one, item))
                time.sleep(POLL_INTERVAL)
                continue

            # Recovery runs FIRST and in every mode. It used to sit after the
            # dispatch checks, so adding the gated guard above it stranded every
            # orphan in gated mode: a job whose worker was killed stayed leased,
            # left the queue, and nothing ever put it back. That is the failure
            # that looks like a company quietly disappearing from the queue.
            _reclaim_orphans(stores, q)
            if len(running) >= flags.apply_concurrency():
                time.sleep(POLL_INTERVAL)      # at the limit; let one finish
                continue
            # Gated mode no longer means "do not look at the queue". It means the
            # worker may take ONLY what the owner has already acted on — an
            # application that stopped to ask a question and has been answered.
            item = next_dispatchable(q, flags.apply_mode())
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
                         manual: bool = False, prepare_only: bool = False) -> dict:
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

    from agent.run import _apply_direct, _reader_eligible, _reader_patch, prefetch_browser_jds, run_job
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
             if not r.get("rescore_requested")
             and not is_internal_pk(r.get("pk", "")) and _reader_eligible(r)]
    if sel or skipped:
        found = [r for r in found if _in_scope(r.get("company"))]
    from core import preparation

    epoch0 = _flags.stop_epoch()
    with preparation.track(found) as progress:
        # Browser-only postings (Meta) are read a batch per session HERE, before the
        # per-row pass, so each row below finds its description already on the row
        # and never opens a browser of its own. Without this every Meta row cost a
        # five-minute session that yielded to any application in flight.
        try:
            prefetch_browser_jds(found, stores)
        except Exception:
            log.exception("process: browser prefetch failed — rows will be read one at a time")
        evaluated = 0
        # Captured before the first job, so only a Stop pressed after this point ends
        # the pass. A board already paused does not.
        for row in found:
            if _pass_cancelled(manual, epoch0):
                log.info("process: stopped by the owner after %d job(s)", evaluated)
                break
            progress.start(row)
            try:
                log.info("process: %s", run_job(row["pk"], stores, prepare_only=True)
                         if prepare_only else run_job(row["pk"], stores))
                evaluated += 1
            except Exception:
                log.exception("process: evaluate failed for %s", row.get("pk"))
                try:
                    _reader_patch(row["pk"], row, stores, {
                        "status": Status.ERROR, "error": "pipeline error — see logs"})
                except Exception:
                    log.exception("could not mark %s errored", row.get("pk"))

            finally:
                progress.complete((stores.tracking.get(row["pk"]) or {}).get("status", ""))

    if prepare_only:
        return {"evaluated": evaluated, "applied": 0}

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
        if is_internal_pk(pk):
            continue
        st = r.get("status")
        if st == "submitting":
            if _hold_possible_submission(stores, r):
                release_claim(pk, stores)
                continue
            stores.tracking.set_status(pk, Status.TAILORED)
            stores.queue.enqueue(stores.apply_queue, {"pk": pk})
            release_claim(pk, stores)
            n += 1
        elif st == "tailoring":  # score/tailor was killed mid-run → back to found
            stores.tracking.set_status(pk, Status.FOUND)
            release_claim(pk, stores)  # else every retry is refused until the TTL
            t += 1
        elif st == "found":
            # A Retry pressed mid-run resets the row to found and bounces off the
            # run's own claim. If the restart then kills that run, the row is
            # `found` with a live claim nobody will ever release: every Apply is
            # refused as "already being processed" until the TTL. Nothing runs
            # before this pass, so any claim here is an orphan.
            release_claim(pk, stores)
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


def _start_imap_poller(stores, settings=None) -> bool:
    from core import reply_ingest
    from core.config import get_settings

    settings = settings or get_settings()
    if (settings.mode != "local" or not reply_ingest.enabled()
            or not reply_ingest.poll_interval()
            or not reply_ingest.local_credentials(settings, stores.secrets)):
        return False
    threading.Thread(target=reply_ingest.run_poller, daemon=True,
                     name="imap-replies").start()
    return True


def _start_funding_poller(settings=None) -> bool:
    """Only the local, explicitly opted-in and valid source set may start."""
    from core.config import get_settings

    settings = settings or get_settings()
    if settings.mode != "local" or not settings.funding_enabled:
        return False
    from discovery import funding

    if not funding.ready(settings):
        log.warning("funding poller disabled: no enabled validated sources or invalid limits")
        return False
    threading.Thread(target=funding.run_poller, args=(settings,), daemon=True,
                     name="funding-poller").start()
    return True



def _outreach_followup_loop() -> None:
    """Opt-in local email-only schedule; every send still passes guards."""
    from core.config import get_settings
    from core.outreach_ledger import Ledger, OutreachHold
    from core.outreach_email import send_approved

    while True:
        settings = get_settings()
        if (settings.mode == "local" and settings.outreach_enabled
                and settings.outreach_followups_auto):
            try:
                ledger = Ledger(settings)
                stores = make_stores(settings)
                if settings.outreach_followup_variant_id:
                    for item in ledger.due_followups(limit=5):
                        try:
                            send_approved(
                                settings=settings, company_id=item["company_id"],
                                contact_page_url=item["contact_url"],
                                exact_address=item["recipient"], person_name=item["person_name"],
                                variant_id=settings.outreach_followup_variant_id,
                                stores=stores, stage=item["stage"],
                                expand_followups=settings.outreach_followups_expanded)
                        except (OutreachHold, ValueError, OSError):
                            log.warning("outreach follow-up held for operator review")
            except Exception:
                log.warning("outreach scheduler held; check private configuration")
        time.sleep(300)


def _start_outreach_followups(settings=None) -> bool:
    from core.config import get_settings
    from core.outreach_ledger import Ledger

    settings = settings or get_settings()
    if settings.mode != "local" or not settings.outreach_enabled:
        return False
    # Reconcile crash-orphan states even if automatic follow-ups are disabled.
    Ledger(settings).hold_orphaned_attempts()
    if not settings.outreach_followups_auto:
        return False
    threading.Thread(target=_outreach_followup_loop, daemon=True,
                     name="outreach-followups").start()
    return True


def _start_outreach_pixel(settings=None) -> bool:
    """Only an explicitly enabled operator-verified separate loopback service."""
    from core.config import get_settings
    from core.outreach_tracking import pixel_base, serve_pixel

    settings = settings or get_settings()
    if pixel_base(settings) is None:
        return False
    port = getattr(settings, "outreach_pixel_loopback_port", 0)
    if type(port) is not int or not 1024 <= port <= 65535:
        log.warning("outreach pixel disabled: dedicated loopback port is missing")
        return False
    threading.Thread(target=serve_pixel, args=(settings,), daemon=True,
                     name="outreach-pixel-only").start()
    return True


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
    from core.config import get_settings
    _start_imap_poller(stores, get_settings())
    _start_funding_poller(get_settings())
    _start_outreach_followups(get_settings())
    _start_outreach_pixel(get_settings())
    log.info("dashboard: http://127.0.0.1:%d", WEB_PORT)
    try:
        serve(port=WEB_PORT)
    except KeyboardInterrupt:
        log.info("daemon stopped")


if __name__ == "__main__":
    main()
