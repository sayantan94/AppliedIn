"""Lever postings API: https://api.lever.co/v0/postings/<handle>?mode=json"""

from __future__ import annotations

import httpx
from appliedin_core.models import JobRecord

from ..watchlist import CompanyConfig


class LeverAdapter:
    ats = "lever"

    def fetch(self, company: CompanyConfig, client: httpx.Client) -> list[JobRecord]:
        url = f"https://api.lever.co/v0/postings/{company.board}?mode=json"
        resp = client.get(url, timeout=20)
        resp.raise_for_status()
        jobs = []
        for j in resp.json():
            jobs.append(
                JobRecord(
                    company=company.name,
                    job_id=str(j["id"]),
                    title=j.get("text", ""),
                    jd_url=j.get("hostedUrl", ""),
                    jd_text=j.get("descriptionPlain", "") or j.get("description", ""),
                    location=(j.get("categories") or {}).get("location", ""),
                    ats=self.ats,
                )
            )
        return jobs
