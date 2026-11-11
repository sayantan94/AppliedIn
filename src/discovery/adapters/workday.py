"""Workday adapter — BEST-EFFORT (HLD premise 1).

The ``/wday/cxs/<tenant>/<site>/jobs`` endpoint is unofficial and per-tenant.
``company.board`` holds the full base URL. Parsing is defensive: a row missing
expected fields is skipped rather than crashing the whole poll.
"""

from __future__ import annotations

import httpx

from core.logging import get_logger
from core.models import JobRecord

from ..watchlist import CompanyConfig

log = get_logger(__name__)


class WorkdayAdapter:
    ats = "workday"

    def fetch(self, company: CompanyConfig, client: httpx.Client) -> list[JobRecord]:
        base = company.board.rstrip("/")
        # board is https://<host>/wday/cxs/<tenant>/<site>/jobs; the page a person
        # opens is https://<host>/<site><externalPath>. Storing the bare path left
        # a posting nobody could read or apply to.
        public = ""
        if "/wday/cxs/" in base:
            host, _, rest = base.partition("/wday/cxs/")
            site = rest.split("/")[1] if rest.count("/") >= 1 else ""
            public = f"{host}/{site}" if site else host
        total = None
        jobs: list[JobRecord] = []
        offset, limit = 0, 20
        while True:
            try:
                resp = client.post(
                    base,
                    json={"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""},
                    timeout=20,
                )
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("workday fetch failed for %s: %s", company.name, exc)
                break

            postings = payload.get("jobPostings", [])
            for j in postings:
                path = j.get("externalPath")
                if not path:
                    continue
                jobs.append(
                    JobRecord(
                        company=company.name,
                        job_id=path.rsplit("/", 1)[-1],
                        title=j.get("title", ""),
                        jd_url=f"{public}{path}" if public else path,
                        jd_text=j.get("title", ""),  # detail requires a per-job call
                        location=j.get("locationsText", ""),
                        ats=self.ats,
                    )
                )
            offset += limit
            # Only the first page reliably carries the total; later pages report
            # 0, which read as "done" and capped every tenant at two pages.
            if total is None:
                total = payload.get("total") or 0
            if not postings or (total and offset >= total) or (not total and len(postings) < limit):
                break
        return jobs
