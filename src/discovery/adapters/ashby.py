"""Ashby public job board API.

POST https://api.ashbyhq.com/posting-api/job-board/<name>?includeCompensation=true
"""

from __future__ import annotations

import httpx

from core.models import JobRecord

from ..watchlist import CompanyConfig


def _iso(v: object) -> str:
    """A date string from whatever the feed used. Epoch milliseconds, epoch
    seconds and an ISO string all appear across these boards; anything else
    becomes empty rather than a wrong date."""
    from datetime import datetime, timezone

    if v in (None, ""):
        return ""
    if isinstance(v, (int, float)):
        secs = float(v) / 1000 if float(v) > 1e11 else float(v)
        try:
            return datetime.fromtimestamp(secs, timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            return ""
    return str(v)


class AshbyAdapter:
    ats = "ashby"

    def fetch(self, company: CompanyConfig, client: httpx.Client) -> list[JobRecord]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{company.board}"
        resp = client.get(url, params={"includeCompensation": "true"}, timeout=20)
        resp.raise_for_status()
        jobs = []
        for j in resp.json().get("jobs", []):
            jobs.append(
                JobRecord(
                    company=company.name,
                    job_id=str(j["id"]),
                    title=j.get("title", ""),
                    jd_url=j.get("jobUrl", ""),
                    jd_text=j.get("descriptionPlain", "") or j.get("description", ""),
                    location=j.get("location", ""),
                    ats=self.ats,
                    # Ashby publishes publishedAt and leaves updatedAt null on the board feed.
                    posted_at=_iso(j.get("publishedAt")),
                )
            )
        return jobs
