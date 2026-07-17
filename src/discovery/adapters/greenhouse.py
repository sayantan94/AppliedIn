"""Greenhouse public job board API.

Feed: https://boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true
``content=true`` returns the HTML JD inline, so no per-job follow-up call.
"""

from __future__ import annotations

import re

import httpx
from core.models import JobRecord

from ..watchlist import CompanyConfig

_TAGS = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    return _TAGS.sub(" ", s or "").replace("&nbsp;", " ").strip()


class GreenhouseAdapter:
    ats = "greenhouse"

    def fetch(self, company: CompanyConfig, client: httpx.Client) -> list[JobRecord]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{company.board}/jobs?content=true"
        resp = client.get(url, timeout=20)
        resp.raise_for_status()
        jobs = []
        for j in resp.json().get("jobs", []):
            jobs.append(
                JobRecord(
                    company=company.name,
                    job_id=str(j["id"]),
                    title=j.get("title", ""),
                    jd_url=j.get("absolute_url", ""),
                    jd_text=_strip_html(j.get("content", "")),
                    location=(j.get("location") or {}).get("name", ""),
                    ats=self.ats,
                )
            )
        return jobs
