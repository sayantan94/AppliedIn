"""SmartRecruiters public postings API.

https://api.smartrecruiters.com/v1/companies/<identifier>/postings
The list endpoint omits full JD text; we keep the summary and let tailoring
fetch detail if needed. Paginated via ``offset``/``limit``.
"""

from __future__ import annotations

import httpx

from core.models import JobRecord

from ..watchlist import CompanyConfig


class SmartRecruitersAdapter:
    ats = "smartrecruiters"

    def fetch(self, company: CompanyConfig, client: httpx.Client) -> list[JobRecord]:
        base = f"https://api.smartrecruiters.com/v1/companies/{company.board}/postings"
        jobs: list[JobRecord] = []
        offset, limit = 0, 100
        while True:
            resp = client.get(base, params={"offset": offset, "limit": limit}, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
            content = payload.get("content", [])
            for j in content:
                loc = j.get("location", {})
                jobs.append(
                    JobRecord(
                        company=company.name,
                        job_id=str(j["id"]),
                        title=j.get("name", ""),
                        jd_url=j.get("ref", ""),
                        jd_text=j.get("jobAd", {}).get("sections", {}).get("jobDescription", {})
                        .get("text", "")
                        or j.get("name", ""),
                        location=f"{loc.get('city', '')} {loc.get('country', '')}".strip(),
                        ats=self.ats,
                    )
                )
            offset += limit
            if offset >= payload.get("totalFound", len(content)) or not content:
                break
        return jobs
