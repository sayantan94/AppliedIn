"""Lever postings API: https://api.lever.co/v0/postings/<handle>?mode=json"""

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
                    # Lever's createdAt is epoch milliseconds, normalised below.
                    posted_at=_iso(j.get("createdAt")),
                )
            )
        return jobs
