"""Per company application queues, with retry and a dead letter queue.

One flat queue drains in whatever order things were approved, so approving three
Waymo roles and four Netflix ones sends three applications to Waymo back to back
and then four to Netflix. That is the shape an employer's automation defences are
built to notice, and it is also the worst order for the owner: a single company
having a bad day blocks everything behind it.

So the work is grouped by company, which is what makes the queue legible: you can
see three pending at Waymo and four at Netflix rather than seven undifferentiated
rows. Dispatch itself is IMMEDIATE and in the order things were queued. Two
applications to the same employer back to back are fine.

Several applications may run at once, but never two to the SAME company. Two
sessions on one employer share a domain, a login and a set of cookies, and they
interleave: one can capture the other's page, and a half filled form can pick up
the other's state. Different employers are different sites and do not collide.

So a company is LEASED while one of its applications is running. Its other jobs
wait, everyone else carries on, and the lease is released the moment the
application finishes — including when it fails, or the queue would wedge itself
on the first crash.

The retry rules are the important half. A failure that is about the ENVIRONMENT
should be retried, and a failure that is about the APPLICATION must not be:

  retry            browser held by another program, a posting that would not load,
                   a timeout, a network error
  never retry      a confirmed submission, a duplicate, a guardrail refusal, an
                   unconfirmed submit

That last one is the one to be careful with. "Unconfirmed" means the form may
already have gone through, so retrying it could submit a second application under
a real name. It goes to the owner, never back into the queue.

After `MAX_ATTEMPTS` a job moves to the dead letter queue with its history, so
"tried and gave up" is countable and reviewable rather than a card that quietly
stopped moving.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .logging import get_logger

log = get_logger(__name__)

MAX_ATTEMPTS = 3

# Backoff between attempts, in seconds. Generous on purpose: the failures worth
# retrying are transient conditions in a browser, and hammering one a second
# later just spends a session to see the same thing.
# Two waits, because three attempts only ever sit between two of them: attempt 1
# fails, wait 120; attempt 2 fails, wait 600; attempt 3 fails, dead letter. A
# third entry read as a 30 minute tier nobody ever waited.
BACKOFF_S = (120, 600)

_KEY = "applyq"          # applyq:companies (set) and applyq:co:<company> (list)
_DLQ = "applyq:dlq"
_BUSY = "applyq:inflight"      # companies with an application running right now
_BUSY_PKS = "applyq:inflight:pks"   # the exact jobs, so an orphan can be told apart
_FLUSH = "applyq:flushing"     # companies being drained by hand, right now

# Outcomes that must never be retried automatically. The reasons differ but the
# rule is the same: another attempt cannot help and might do harm.
TERMINAL = frozenset({
    "already_applied",      # a duplicate under a real name is worse than a miss
    "duplicate_application",
    "guardrail",            # refusing to answer something is a decision, not a fault
    "no_sponsorship",
    "application_limit",    # the employer said no more, and that is not a fault
    "uncertain",            # may already have submitted — the owner must check
    "user_skipped",         # the owner said no; retrying would argue with them
    "stale_resume",         # handled by re-tailoring, not by re-applying
})


def _norm(company: str) -> str:
    return (company or "").strip().lower() or "unknown"


class ApplyQueue:
    """Company grouped apply queues on Redis.

    Deliberately not the generic Queue: that one is a flat list with no notion of
    who a job belongs to, when it was last tried, or how many times.
    """

    def __init__(self, client: Any) -> None:
        self.r = client

    # --- writing ----------------------------------------------------------
    def put(self, pk: str, company: str, *, attempts: int = 0,
            not_before: float = 0.0, history: list | None = None,
            queued_at: float = 0.0) -> bool:
        """Queue an application. False when that job is already waiting.

        The same pk can be offered more than once — approve-all clicked twice, a
        rescan re-approving a row, an upgrade folding in the old flat queue. Each
        copy is dispatched and then refused by the duplicate guard, so nothing is
        submitted twice, but it burns a company's turn on a job that cannot run and
        makes the queue depth lie about how much work is left.

        Only PENDING work is checked, never the in-flight set: `retry()` re-queues
        an item while its lease is still held, and matching on that would silently
        drop every retry.
        """
        co = _norm(company)
        for raw in (self.r.lrange(f"{_KEY}:co:{co}", 0, -1) or []):
            try:
                if json.loads(raw).get("pk") == pk:
                    return False
            except ValueError:            # a malformed entry should not block a put
                continue
        item = {"pk": pk, "company": company, "attempts": attempts,
                "not_before": not_before, "history": history or [],
                # Kept so dispatch can be first come first served ACROSS companies
                # while the lists stay grouped by company for visibility.
                "queued_at": queued_at or time.time()}
        self.r.sadd(f"{_KEY}:companies", co)
        self.r.rpush(f"{_KEY}:co:{co}", json.dumps(item))
        return True

    def retry(self, item: dict, reason: str) -> bool:
        """Re-queue after a retryable failure. False when it has run out of tries
        and has gone to the dead letter queue instead."""
        attempts = int(item.get("attempts", 0)) + 1
        history = list(item.get("history") or []) + [
            {"at": time.time(), "attempt": attempts, "reason": reason[:200]}]
        if attempts >= MAX_ATTEMPTS:
            self.dead_letter(item, reason, history)
            return False
        wait = BACKOFF_S[min(attempts - 1, len(BACKOFF_S) - 1)]
        self.put(item["pk"], item.get("company", ""), attempts=attempts,
                 not_before=time.time() + wait, history=history)
        log.info("apply retry %d/%d for %s in %ds (%s)",
                 attempts, MAX_ATTEMPTS, item["pk"], wait, reason[:80])
        return True

    def dead_letter(self, item: dict, reason: str, history: list | None = None) -> None:
        rec = {**item, "reason": reason[:300], "at": time.time(),
               "history": history or item.get("history") or []}
        self.r.rpush(_DLQ, json.dumps(rec))
        log.warning("apply gave up on %s after %d attempt(s): %s",
                    item.get("pk"), int(item.get("attempts", 0)) + 1, reason[:120])

    # --- reading ----------------------------------------------------------
    def next(self, only: str = "") -> dict | None:
        """Lease the next application, or None when nothing can start.

        `only` restricts the lease to one company, which is how a single employer's
        queue can be drained by hand while automatic applying is paused. It narrows
        the choice; it does not relax the rules — a company already running one is
        still skipped, and backoff still holds.

        Skips any company that already has one running, so two applications never
        hit the same employer at once. The caller MUST call `done()` afterwards,
        success or failure, to release the lease.

        None is a normal idle state: everything left may belong to a busy company
        or still be inside its retry backoff.
        """
        now = time.time()
        busy = set(self.r.smembers(_BUSY) or [])
        best: tuple[float, str, str] | None = None      # (queued_at, company, raw)

        want = _norm(only) if only else ""
        for co in list(self.r.smembers(f"{_KEY}:companies") or []):
            if want and co != want:
                continue
            if co in busy:
                continue                                # one at a time per company
            key = f"{_KEY}:co:{co}"
            if not self.r.llen(key):
                self.r.srem(f"{_KEY}:companies", co)
                continue
            for raw in (self.r.lrange(key, 0, -1) or []):
                item = json.loads(raw)
                if float(item.get("not_before") or 0) > now:
                    continue                            # still backing off
                when = float(item.get("queued_at") or 0)
                if best is None or when < best[0]:
                    best = (when, co, raw)

        if best is None:
            return None
        _, co, raw = best
        self.r.lrem(f"{_KEY}:co:{co}", 1, raw)
        item = json.loads(raw)
        self.r.sadd(_BUSY, co)
        # The pk as well as the company. A company lease cannot tell a job that is
        # genuinely running from one whose worker died, and with two jobs at one
        # employer it cannot tell WHICH is running. Recovery needs that.
        self.r.sadd(_BUSY_PKS, item["pk"])
        return item

    def done(self, item: dict) -> None:
        """Release a company's lease. Must run in a finally: a lease left behind by
        a crash would block that employer until the process restarted."""
        self.r.srem(_BUSY, _norm(item.get("company", "")))
        self.r.srem(_BUSY_PKS, item.get("pk", ""))

    # --- manual flush ------------------------------------------------------
    # Draining a company by hand runs its jobs back to back, which for four
    # applications is over half an hour of browser sessions. That has to be
    # interruptible, and the flag is what the loop checks BETWEEN jobs: stopping
    # mid form is a separate, more costly decision than deciding not to start the
    # next one. Kept in Redis rather than in the process so the dashboard can show
    # what is flushing and Stop can reach it from any request.
    def start_flush(self, company: str) -> bool:
        """Claim a company for a manual flush. False when one is already running.

        Atomic on purpose: SADD reports whether THIS caller added it, so two quick
        clicks cannot both start a loop against the same employer.
        """
        return bool(self.r.sadd(_FLUSH, _norm(company)))

    def flushing(self) -> set:
        return set(self.r.smembers(_FLUSH) or [])

    def stop_flush(self, company: str = "") -> int:
        """Ask a flush to stop after the job it is on. Everything, when unnamed."""
        if company:
            return int(self.r.srem(_FLUSH, _norm(company)) or 0)
        n = self.r.scard(_FLUSH) or 0
        self.r.delete(_FLUSH)
        return n

    def remove(self, pk: str) -> bool:
        """Take a job out of the queue. False when it was not waiting.

        Only PENDING work can be removed. A job already leased is being filled in a
        browser right now and deleting its queue entry would not stop that — it
        would only lose the record of what is running. Stopping a live application
        is Stop applying, which is a different and more costly decision.
        """
        for co in list(self.r.smembers(f"{_KEY}:companies") or []):
            key = f"{_KEY}:co:{co}"
            for raw in (self.r.lrange(key, 0, -1) or []):
                try:
                    if json.loads(raw).get("pk") != pk:
                        continue
                except ValueError:
                    continue
                self.r.lrem(key, 1, raw)
                if not self.r.llen(key):
                    self.r.srem(f"{_KEY}:companies", co)
                log.info("removed %s from the %s queue", pk, co)
                return True
        return False

    def drop_dead_letter(self, pk: str) -> int:
        """Forget a dead lettered job instead of reviving it."""
        kept, dropped = [], 0
        for raw in (self.r.lrange(_DLQ, 0, -1) or []):
            try:
                match = json.loads(raw).get("pk") == pk
            except ValueError:
                match = False
            if match:
                dropped += 1
            else:
                kept.append(raw)
        if dropped:
            self.r.delete(_DLQ)
            for raw in kept:
                self.r.rpush(_DLQ, raw)
        return dropped

    def in_flight(self) -> set:
        """The pks being applied right now. Anything the store calls `submitting`
        that is NOT in here has lost its worker."""
        return set(self.r.smembers(_BUSY_PKS) or [])

    def reset_leases(self) -> int:
        """Drop every lease. Called at startup, because a lease is in flight state
        and nothing is in flight in a process that has only just begun."""
        n = self.r.scard(_BUSY) or 0
        self.r.delete(_BUSY)
        self.r.delete(_BUSY_PKS)
        self.r.delete(_FLUSH)     # no flush survives the process running it
        return n

    # --- inspection -------------------------------------------------------
    def depth(self) -> dict:
        """Everything the dashboard needs about the queue in one read.

        `running` is which companies are mid application, which is the number that
        explains why a queued job has not started: it is either at the
        concurrency limit or its company is busy.
        """
        out = {}
        for co in (self.r.smembers(f"{_KEY}:companies") or []):
            n = self.r.llen(f"{_KEY}:co:{co}")
            if n:
                out[co] = n
        running = sorted(self.r.smembers(_BUSY) or [])
        return {"queued": out, "total": sum(out.values()),
                "running": running, "flushing": sorted(self.flushing()),
                "dlq": self.r.llen(_DLQ)}

    def pending(self, limit: int = 5000) -> list[dict]:
        """Everything waiting, in the order it will actually start.

        `depth()` answers "how many"; this answers "when is mine". Dispatch is
        first come first served by `queued_at` ACROSS companies, so that sort is
        the global order — but a company runs one at a time, so the third Microsoft
        job does not start third overall. `company_rank` is the honest per employer
        position, and `blocked` says which of the two reasons is holding it: its
        company is busy, or it is still inside a retry backoff.

        An estimate, not a promise: real order also depends on how long each
        application takes. It is ordered by the same key dispatch uses, so the next
        job to start is always the first one listed.

        `limit` is a runaway guard, NOT a display limit, which is why it is large.
        It was 60 while the board showed a flat list of the first few, and that
        quietly broke the board: the board decides a job is queued by looking for
        its pk in here, so everything past the cap rendered as UNQUEUED and sat in
        "Ready to apply" instead. With 79 waiting, Oracle disappeared from the
        queue entirely and its jobs looked like they had never been approved.
        """
        now = time.time()
        busy = set(self.r.smembers(_BUSY) or [])
        items: list[dict] = []
        for co in list(self.r.smembers(f"{_KEY}:companies") or []):
            for raw in (self.r.lrange(f"{_KEY}:co:{co}", 0, -1) or []):
                try:
                    it = json.loads(raw)
                except ValueError:
                    continue
                it["_co"] = co
                items.append(it)

        items.sort(key=lambda i: float(i.get("queued_at") or 0))
        rank: dict[str, int] = {}
        out = []
        for it in items:
            co = it["_co"]
            rank[co] = rank.get(co, 0) + 1
            waiting = float(it.get("not_before") or 0)
            blocked = ("company_busy" if co in busy
                       else "backoff" if waiting > now
                       else "")
            out.append({"pk": it.get("pk", ""), "company": it.get("company", ""),
                        "queued_at": float(it.get("queued_at") or 0),
                        "attempts": int(it.get("attempts") or 0),
                        "company_rank": rank[co], "blocked": blocked,
                        "ready_in": max(0, int(waiting - now)) if waiting > now else 0})
        return out[:limit]

    def dead_letters(self) -> list[dict]:
        return [json.loads(x) for x in (self.r.lrange(_DLQ, 0, -1) or [])]

    def revive(self, pk: str = "") -> int:
        """Put dead lettered jobs back, all of them or just one. Attempts reset:
        the owner asking again is a new decision, not a continuation."""
        revived, kept = 0, []
        for raw in (self.r.lrange(_DLQ, 0, -1) or []):
            rec = json.loads(raw)
            if pk and rec.get("pk") != pk:
                kept.append(raw)
                continue
            self.put(rec["pk"], rec.get("company", ""), history=rec.get("history"))
            revived += 1
        self.r.delete(_DLQ)
        for raw in kept:
            self.r.rpush(_DLQ, raw)
        return revived


def is_retryable(result: dict) -> tuple[bool, str]:
    """Whether an apply result should be tried again, and why.

    Reads the SAME result shape every engine returns, so the decision lives in one
    place rather than being re-derived by each caller.
    """
    # The apply path returns its outcome under `result`; the browser engines under
    # `status`. This read only `status`, so every apply came back as "" and fell
    # through to (False, "done") — the retry branch below was unreachable and no
    # transient failure was ever retried, backed off or dead lettered. A timeout
    # or a posting that would not load landed permanently in Unable and cost a
    # manual Retry each. Read both, and accept both vocabularies.
    outcome = str(result.get("status") or result.get("result") or "")
    reason = str(result.get("reason") or "")
    if outcome in ("applied", "applied_manual", "done"):
        return False, "applied"
    if outcome in ("gate", "gated"):
        return False, "waiting on the owner"
    # TERMINAL is checked BEFORE the retry branch and must stay there. It is what
    # keeps `uncertain` (may already have submitted), `already_applied` and an
    # employer's own cap from being tried a second time.
    if reason in TERMINAL or outcome in TERMINAL:
        return False, reason or outcome
    if outcome in ("failed", "unknown", "error"):
        return True, reason or outcome
    return False, outcome or "done"
