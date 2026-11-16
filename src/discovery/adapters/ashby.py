"""Ashby public job board API.

POST https://api.ashbyhq.com/posting-api/job-board/<name>?includeCompensation=true
"""

from __future__ import annotations

import httpx

from core.models import JobRecord
from tools import posting_guard as pg

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



_LOCATION_KEYS = ("location", "secondaryLocations", "workplaceType", "isRemote")


def ashby_location_metadata(posting: dict) -> str:
    """Canonical, separately labelled source values; null means absent."""
    import json

    return "\n".join(
        f"Ashby {key}: {json.dumps(posting[key], sort_keys=True, ensure_ascii=True, separators=(',', ':'))}"
        for key in _LOCATION_KEYS if posting.get(key) is not None
    )


def ashby_jd_text(posting: dict, description: str) -> str:
    """Keep evidence ahead of the description's guard truncation boundary."""
    metadata = ashby_location_metadata(posting)
    return metadata + ("\n" if metadata and description else "") + description


def ashby_display_location(value: object) -> str:
    """Project a sole unambiguous primary place into JobRecord's string field."""
    if isinstance(value, str):
        return value.strip()[:256]
    if isinstance(value, dict):
        names = [value.get(k) for k in ("name", "displayName", "display_name")]
        names = [n.strip() for n in names if isinstance(n, str) and n.strip()]
        return names[0][:256] if names and len(set(names)) == 1 else ""
    if isinstance(value, list) and 0 < len(value) <= 8:
        names = [ashby_display_location(item) for item in value]
        return names[0] if names and all(n == names[0] and n for n in names) else ""
    return ""


class AshbyAdapter:
    ats = "ashby"

    def fetch(self, company: CompanyConfig, client: httpx.Client) -> list[JobRecord]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{company.board}"
        resp = client.get(url, params={"includeCompensation": "true"}, timeout=20)
        resp.raise_for_status()
        jobs = []
        for j in resp.json().get("jobs", []):
            inspected = pg.analyze(ashby_jd_text(
                j, str(j.get("descriptionPlain") or j.get("description") or "")
            ))
            jobs.append(
                JobRecord(
                    company=company.name,
                    job_id=str(j["id"]),
                    title=j.get("title", ""),
                    jd_url=j.get("jobUrl", ""),
                    jd_text=inspected["text"],
                    posting_guard=inspected["posting_guard"],
                    location=ashby_display_location(j.get("location")),
                    ats=self.ats,
                    # Ashby publishes publishedAt and leaves updatedAt null on the board feed.
                    posted_at=_iso(j.get("publishedAt")),
                )
            )
        return jobs
