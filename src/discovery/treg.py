"""On-demand job search through Treg's Hirebase endpoint."""

import hashlib
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

from core import config
from discovery import career_ops, cosign
from tools.jd import _text

PAGE = 10
ENDPOINT = "https://treg.to/call/hirebase.jobs.search"
ENV_FILE = career_ops.ROOT / ".env"
SETUP = "Add your Treg team API key to AppliedIn’s .env as TREG_TOKEN, then choose Check again."


def team_key() -> str:
    if config.TREG_ENV_TOKEN is not None:
        return config.TREG_ENV_TOKEN.strip()
    return (dotenv_values(ENV_FILE).get("TREG_TOKEN") or "").strip()


class TregError(RuntimeError):
    pass


def _view(raw: dict, known: tuple) -> dict | None:
    title = str(raw.get("job_title") or "").strip()
    company = str(raw.get("company_name") or "").strip()
    url = str(raw.get("application_link") or "").strip()
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or not title or not company:
            return None
        if parts.hostname in ("jobs.ashbyhq.com", "jobs.lever.co", "jobs.eu.lever.co"):
            path = parts.path.rstrip("/").split("/")
            if len(path) >= 4 and path[-1] in ("application", "apply"):
                url = parts._replace(path="/".join(path[:-1])).geturl()
        url = career_ops.canonical(url)
    except ValueError:
        return None
    prefs, tracked, handled, board = known
    state, pk = cosign.state_of(url, title, tracked, handled, board)
    pay = raw.get("salary_range") or {}
    yearly = (
        isinstance(pay, dict) and pay.get("currency") == "USD" and pay.get("period") == "yearly"
    )
    try:
        posted = datetime.fromisoformat(raw.get("date_posted") or "")
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=UTC)
        posted = int(posted.timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        posted = None
    # One employer posting can yield a separate Hirebase record for each location.
    return {
        "id": "treg:" + hashlib.sha256(url.encode()).hexdigest()[:24],
        "posting_id": str(raw.get("platform_job_id") or ""),
        "url": url,
        "title": title,
        "company": company,
        "state": state,
        "pk": pk,
        "logo": str(raw.get("company_logo") or ""),
        "location": str(raw.get("location_raw") or ""),
        "remote": raw.get("location_type") == "Remote",
        "team": str(raw.get("team") or ""),
        "employment": str(raw.get("job_type") or ""),
        "posted": posted,
        "description": _text(str(raw.get("description") or "")),
        "salary_min": pay.get("min") if yearly else None,
        "salary_max": pay.get("max") if yearly else None,
        "excluded": cosign.excluded_by(title, prefs),
    }


def search(filters: dict, cursor: str | None = None, client: httpx.Client | None = None) -> dict:
    token = team_key()
    if not token:
        raise TregError(SETUP)
    page = int(cursor or 1)
    if page < 1:
        raise ValueError("Choose a valid results page.")
    body = {"page": page, "limit": PAGE, "sort_by": "date_posted", "sort_order": "desc"}
    if term := filters.get("term", "").strip():
        body["job_titles"] = [term]
    if city := filters.get("city"):
        if city not in cosign.CITIES:
            raise ValueError("Choose a city from the list.")
        place = {"city": cosign.CITIES[city]}
        if city in cosign.US_CITIES:
            place["country"] = "United States"
        body["geo_locations"] = [place]
    elif filters.get("region") == "us":
        body["geo_locations"] = [{"country": "United States"}]
    if filters.get("remote"):
        body["location_types"] = ["Remote"]
    if minimum := filters.get("min_salary"):
        body.update(salary={"min": minimum}, currency="USD")
    try:
        post = client.post if client is not None else httpx.post
        reply = post(ENDPOINT, headers={"X-Treg-Token": token}, json=body, timeout=30)
        if reply.status_code in (401, 403):
            raise TregError(
                "Treg rejected the team key. Update TREG_TOKEN in .env and search again."
            )
        if reply.status_code == 402:
            raise TregError("Your Treg balance is too low for this search.")
        if reply.status_code == 429:
            raise TregError("Treg’s usage limit was reached. Try again later.")
        reply.raise_for_status()
        data = reply.json()
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise ValueError("Missing jobs")
        pages = int(data["total_pages"])
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        raise TregError("Treg could not return this page of jobs.") from exc
    known = cosign.known()
    jobs = {}
    for raw in data["jobs"]:
        if not isinstance(raw, dict) or not (row := _view(raw, known)):
            continue
        existing = jobs.setdefault(row["id"], row)
        where = row["location"]
        if where and where not in existing["location"].split(" · "):
            existing["location"] = " · ".join(filter(None, [existing["location"], where]))
    rows = list(jobs.values())
    cosign._remember(rows)
    return {"jobs": rows, "done": page >= pages, "cursor": str(page + 1) if page < pages else None}


def stage(ids: list[str], *, apply_requested: bool = False) -> dict:
    if any(not re.fullmatch(r"treg:[0-9a-f]{24}", item) for item in ids):
        raise ValueError("Choose roles from the Treg results.")
    return cosign.stage(ids, apply_requested=apply_requested, network="treg")
