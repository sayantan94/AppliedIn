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
BACKOFF_S = (120, 600, 1800)

_KEY = "applyq"          # applyq:companies (set) and applyq:co:<company> (list)
_DLQ = "applyq:dlq"
_BUSY = "applyq:inflight"   # companies with an application running right now

# Outcomes that must never be retried automatically. The reasons differ but the
# rule is the same: another attempt cannot help and might do harm.
TERMINAL = frozenset({
    "already_applied",      # a duplicate under a real name is worse than a miss
    "duplicate_application",
    "guardrail",            # refusing to answer something is a decision, not a fault
    "no_sponsorship",
    "application_limit",    # the employer said no more, and that is not a fault
    "uncertain",            # may already have submitted — the owner must check
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
            queued_at: float = 0.0) -> None:
        co = _norm(company)
        item = {"pk": pk, "company": company, "attempts": attempts,
                "not_before": not_before, "history": history or [],
                # Kept so dispatch can be first come first served ACROSS companies
                # while the lists stay grouped by company for visibility.
                "queued_at": queued_at or time.time()}
        self.r.sadd(f"{_KEY}:companies", co)
        self.r.rpush(f"{_KEY}:co:{co}", json.dumps(item))

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
    def next(self) -> dict | None:
        """Lease the next application, or None when nothing can start.

        Skips any company that already has one running, so two applications never
        hit the same employer at once. The caller MUST call `done()` afterwards,
        success or failure, to release the lease.

        None is a normal idle state: everything left may belong to a busy company
        or still be inside its retry backoff.
        """
        now = time.time()
        busy = set(self.r.smembers(_BUSY) or [])
        best: tuple[float, str, str] | None = None      # (queued_at, company, raw)

        for co in list(self.r.smembers(f"{_KEY}:companies") or []):
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
        self.r.sadd(_BUSY, co)
        return json.loads(raw)

    def done(self, item: dict) -> None:
        """Release a company's lease. Must run in a finally: a lease left behind by
        a crash would block that employer until the process restarted."""
        self.r.srem(_BUSY, _norm(item.get("company", "")))

    def reset_leases(self) -> int:
        """Drop every lease. Called at startup, because a lease is in flight state
        and nothing is in flight in a process that has only just begun."""
        n = self.r.scard(_BUSY) or 0
        self.r.delete(_BUSY)
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
                "running": running, "dlq": self.r.llen(_DLQ)}

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
    status = str(result.get("status") or "")
    reason = str(result.get("reason") or "")
    if status in ("applied", "applied_manual"):
        return False, "applied"
    if status == "gate":
        return False, "waiting on the owner"
    if reason in TERMINAL or status in TERMINAL:
        return False, reason or status
    if status in ("failed", "unknown", "error"):
        return True, reason or status
    return False, status or "done"
