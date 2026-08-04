"""When each company's scan finished, and what it produced.

A sweep of the whole watchlist is hours of sequential browser work. While it runs
the board can say which company is being read, but it could say nothing about the
forty that already finished: whether they found anything, how long each took, or
whether one quietly returned nothing. The log had it, one line at a time, buried
among heartbeats.

So each company records its own result as it completes. That turns a long run from
a spinner into a list you can watch fill up, and it answers the question afterwards
too: a company that found nothing at 10am is a different fact from one that was
never reached because the sweep was still going.

Kept per run rather than forever. What a scan found last Tuesday is not
interesting once a newer one has run, and an unbounded history would need pruning
rules nobody would ever tune.
"""

from __future__ import annotations

import json
import time
from typing import Any

from core.logging import get_logger

log = get_logger(__name__)

_KEY = "scan:log"             # list of finished company results, newest first
_RUN = "scan:log:run"         # when the current run started, so a new one clears
_CAP = 60                     # a watchlist is ~45; a little headroom
_TTL_S = 24 * 3600


def start_run(client: Any, total: int) -> None:
    """Mark a new sweep, clearing the previous one's results.

    Cleared at the START rather than kept and appended to, because a list mixing
    two runs cannot be read: "Adobe found nothing" means one thing this run and
    something else three runs ago.
    """
    if client is None:
        return
    try:
        pipe = client.pipeline()
        pipe.delete(_KEY)
        pipe.set(_RUN, json.dumps({"at": time.time(), "total": int(total)}), ex=_TTL_S)
        pipe.execute()
    except Exception:  # noqa: BLE001 — a progress log must not break a scan
        log.debug("could not start the scan log", exc_info=True)


def finished(client: Any, company: str, *, found: int, relevant: int,
             enqueued: int, seconds: float, note: str = "") -> None:
    """Record one company's completed scan."""
    if client is None:
        return
    try:
        row = {"company": company, "at": time.time(), "seconds": round(seconds, 1),
               "found": int(found), "relevant": int(relevant),
               "enqueued": int(enqueued), "note": note[:160]}
        pipe = client.pipeline()
        pipe.lpush(_KEY, json.dumps(row))
        pipe.ltrim(_KEY, 0, _CAP - 1)
        pipe.expire(_KEY, _TTL_S)
        pipe.execute()
    except Exception:  # noqa: BLE001
        log.debug("could not record the scan result for %s", company, exc_info=True)


def results(client: Any) -> dict:
    """This run's finished companies, newest first, with the run's own start."""
    out: dict = {"run": None, "companies": []}
    if client is None:
        return out
    try:
        if (raw := client.get(_RUN)):
            out["run"] = json.loads(raw)
        for r in (client.lrange(_KEY, 0, _CAP - 1) or []):
            try:
                out["companies"].append(json.loads(r))
            except ValueError:
                continue
    except Exception:  # noqa: BLE001
        log.debug("could not read the scan log", exc_info=True)
    return out
