"""SmartRecruiters public postings API.

https://api.smartrecruiters.com/v1/companies/<identifier>/postings
The list endpoint omits full JD text; we keep the summary and let tailoring
fetch detail if needed. Paginated via ``offset``/``limit``.
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
                        # SmartRecruiters calls it releasedDate.
                        posted_at=_iso(j.get("releasedDate")),
                    )
                )
            offset += limit
            if offset >= payload.get("totalFound", len(content)) or not content:
                break
        return jobs
