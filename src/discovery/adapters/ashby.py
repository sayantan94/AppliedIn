"""Ashby public job board API.

POST https://api.ashbyhq.com/posting-api/job-board/<name>?includeCompensation=true
"""

from __future__ import annotations

import httpx
from core.models import JobRecord

from ..watchlist import CompanyConfig


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
                )
            )
        return jobs
