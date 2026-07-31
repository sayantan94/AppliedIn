"""What a scan decided NOT to keep, and why.

A job dropped during discovery never becomes a row, so there is nothing on the
board to open and nothing to ask. That makes a scan's most common outcome its
least explicable one: a role the owner can see on the careers page simply does
not appear, and they cannot tell whether the window was too narrow, their
preferences excluded it, or the scan never saw it at all. Those need three
different actions.

So every rejection is recorded here with the fact it was judged on: the publish
date for an age drop, the preference summary for a relevance drop. Kept per
company, newest first, capped, and expiring on their own, because this is a
debugging trail rather than a record worth keeping: what a scan passed over last
Tuesday is not interesting once a newer scan has run.

Deliberately NOT the tracking store. A row there means a job in the pipeline, and
writing skipped roles into it would put things on the board that were explicitly
not wanted, which is the opposite of what the owner asked for.
"""

from __future__ import annotations

import json
import time
from typing import Any

from core.logging import get_logger

log = get_logger(__name__)

_KEY = "scan:passed"          # scan:passed:<company_lower> -> list of records
_TTL_S = 7 * 24 * 3600        # a week: long enough to explain, short enough to forget
_CAP = 60                     # rows KEPT per company, newest first
_SEEN = "scan:passed:seen"    # hash company -> how many were actually rejected


def _key(company: str) -> str:
    return f"{_KEY}:{(company or '').strip().lower() or 'unknown'}"


def record(client: Any, company: str, jobs: list, reason: str, detail: str = "") -> int:
    """Note that these postings were passed over, and why. Never raises.

    `reason` is the machine code the UI groups by ("too_old", "not_relevant").
    `detail` is the sentence a person reads.
    """
    if not jobs or client is None:
        return 0
    try:
        key, now = _key(company), time.time()
        rows = [json.dumps({
            "title": (getattr(j, "title", "") or "")[:120],
            "url": getattr(j, "jd_url", "") or "",
            "location": (getattr(j, "location", "") or "")[:60],
            "posted_at": getattr(j, "posted_at", "") or "",
            "reason": reason,
            "detail": detail[:200],
            "at": now,
        }) for j in jobs]
        pipe = client.pipeline()
        # Newest first, so a long tail of old rejections never hides today's.
        pipe.lpush(key, *reversed(rows))
        pipe.ltrim(key, 0, _CAP - 1)
        pipe.expire(key, _TTL_S)
        pipe.sadd(f"{_KEY}:companies", (company or "").strip().lower())
        # The true count, kept separately from the rows. Only _CAP rows are
        # stored, so counting them understates a noisy board: a company that
        # rejected two hundred roles read as exactly 60, which is the cap talking
        # rather than the board, and the number was quietly wrong in the one place
        # the panel exists to be trusted.
        pipe.hincrby(_SEEN, (company or "").strip().lower(), len(rows))
        pipe.expire(_SEEN, _TTL_S)
        pipe.execute()
        return len(rows)
    except Exception:  # noqa: BLE001 — an explanation must never break a scan
        log.debug("could not record passed over jobs for %s", company, exc_info=True)
        return 0


def for_company(client: Any, company: str, limit: int = _CAP) -> list[dict]:
    out = []
    try:
        for raw in (client.lrange(_key(company), 0, max(0, limit - 1)) or []):
            try:
                out.append(json.loads(raw))
            except ValueError:
                continue
    except Exception:  # noqa: BLE001
        log.debug("could not read passed over jobs for %s", company, exc_info=True)
    return out


def by_company(client: Any, sample: int = 25) -> list[dict]:
    """Every company with a trail, each with its counts and a sample of rows.

    Not a flat list with a cap. That is what this replaced, and it hid whole
    companies: the rows were sorted by time and truncated, so with 29 companies
    and 1665 records a 200 row budget was spent by the four most recent scans and
    the other 25 companies did not appear at all. A company that scanned an hour
    earlier looked identical to one that was never scanned.

    So the SUMMARY is complete and only the detail is sampled: every company is
    listed with its true totals, and each carries the newest `sample` rows to open.
    """
    out: list[dict] = []
    try:
        for co in (client.smembers(f"{_KEY}:companies") or []):
            rows = for_company(client, co, _CAP)
            if not rows:
                continue
            counts: dict[str, int] = {}
            for r in rows:
                counts[r.get("reason", "?")] = counts.get(r.get("reason", "?"), 0) + 1
            for r in rows:
                r["company"] = co
            try:
                true_total = int(client.hget(_SEEN, co) or 0) or len(rows)
            except (TypeError, ValueError):
                true_total = len(rows)
            out.append({"company": co, "total": true_total, "kept": len(rows),
                        "by_reason": counts,
                        "jobs": rows[:sample],
                        "newest": max((r.get("at", 0) for r in rows), default=0)})
    except Exception:  # noqa: BLE001
        log.debug("could not read passed over jobs", exc_info=True)
    # Most rejections first: the boards worth looking at are the noisy ones.
    out.sort(key=lambda d: (-d["total"], d["company"]))
    return out


def clear(client: Any, company: str = "") -> None:
    """Forget the trail, for one company or all of them."""
    try:
        if company:
            co = (company or "").strip().lower()
            client.delete(_key(company))
            client.srem(f"{_KEY}:companies", co)
            client.hdel(_SEEN, co)
            return
        for co in list(client.smembers(f"{_KEY}:companies") or []):
            client.delete(_key(co))
        client.delete(f"{_KEY}:companies")
        client.delete(_SEEN)
    except Exception:  # noqa: BLE001
        log.debug("could not clear passed over jobs", exc_info=True)
