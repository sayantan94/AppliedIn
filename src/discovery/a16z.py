"""a16z portfolio jobs (jobs.a16z.com): ~18k roles at Andreessen Horowitz companies.

The board is a Next.js site. Its listing is served by one server action,
`loadMorePublicJobs`, which takes the same filters as the page's own search and
returns a page of roles with each one's ATS posting URL. Next.js gives that
action a new id on every deploy, so the id is read from the site's own scripts
and read again whenever a call is refused.

A listing carries no description. That is fine: the pipeline reads any posting
shorter than a page from its URL before scoring (`agent.run._jd_text`).

Nothing here applies. A picked role goes through `cosign.stage`, the same
Career Ops path Cosign uses, so it inherits that path's guarantees: the tracked
job and seen-ledger duplicate checks, prepare-only discovery, and the rule that
only an explicit Apply authorizes a submission.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime

import httpx

from discovery import cosign

log = logging.getLogger(__name__)

SITE = "https://jobs.a16z.com"
ACTION = "loadMorePublicJobs"
PAGE = 50
STAGE_MAX = cosign.STAGE_MAX
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppliedIn"}

# The board's own role facets, keeping the ones that name a kind of work; many of
# its facets are a single company's department names ("X-BAT Division").
ROLE_TYPES = {
    "engineering": "Engineering", "software-engineer": "Software engineer",
    "senior-software-engineer": "Senior software engineer", "applied-ai": "Applied AI",
    "research": "Research", "data-scientist": "Data science", "security": "Security",
    "product-manager": "Product management", "design": "Design",
    "mechanical-engineer": "Mechanical engineering",
    "electrical-engineer": "Electrical engineering",
    "sales": "Sales", "account-executive": "Account executive", "marketing": "Marketing",
    "customer-success": "Customer success", "business-development": "Business development",
    "operations": "Operations", "finance": "Finance", "legal": "Legal", "people": "People",
}

# The board's own location values. A value it does not know is an empty result,
# not an error, so a place must come from this list. "All of the US" is
# `country:us`, a real filter here rather than Cosign's read-ahead.
CITIES = {
    "metro:san-francisco-bay-area": "San Francisco Bay Area",
    "metro:new-york-city-metropolitan-area": "New York City area",
    "metro:greater-seattle-area": "Seattle area",
    "metro:greater-los-angeles-area": "Los Angeles area",
    "metro:austin-texas-metropolitan-area": "Austin",
    "metro:greater-boston": "Boston",
    "metro:dallas-fort-worth-area": "Dallas/Fort Worth",
    "metro:greater-san-diego-area": "San Diego",
    "metro:chicago-area": "Chicago",
    "metro:atlanta": "Atlanta",
    "metro:houston-texas-area": "Houston",
    "metro:miami-fort-lauderdale-area": "Miami",
    "metro:phoenix-arizona-area": "Phoenix",
    "metro:greater-denver-area": "Denver",
    "country:ca": "Canada", "country:gb": "United Kingdom", "country:ie": "Ireland",
    "country:de": "Germany", "country:fr": "France", "country:nl": "Netherlands",
    "country:ch": "Switzerland", "country:il": "Israel", "country:in": "India",
    "country:sg": "Singapore", "country:jp": "Japan", "country:au": "Australia",
}
MAX_CITIES = cosign.MAX_CITIES
# Preference locations, by the word that names them, to the place they file under.
_PLACE_WORDS = {
    "metro:san-francisco-bay-area": ("san francisco", "bay area", "sf", "palo alto",
                                     "mountain view", "san jose", "oakland", "california"),
    "metro:new-york-city-metropolitan-area": ("new york", "nyc", "brooklyn"),
    "metro:greater-seattle-area": ("seattle", "bellevue", "redmond", "kirkland"),
    "metro:greater-los-angeles-area": ("los angeles", "santa monica", "irvine", "la"),
    "metro:austin-texas-metropolitan-area": ("austin",),
    "metro:greater-boston": ("boston", "cambridge"),
    "metro:dallas-fort-worth-area": ("dallas", "fort worth"),
    "metro:greater-san-diego-area": ("san diego",),
    "metro:chicago-area": ("chicago",),
    "metro:atlanta": ("atlanta",),
    "metro:houston-texas-area": ("houston",),
    "metro:miami-fort-lauderdale-area": ("miami",),
    "metro:phoenix-arizona-area": ("phoenix",),
    "metro:greater-denver-area": ("denver", "boulder"),
    "country:ca": ("canada", "toronto", "vancouver", "montreal"),
    "country:gb": ("london", "united kingdom", "uk"),
    "country:ie": ("dublin", "ireland"),
    "country:de": ("berlin", "munich", "germany"),
    "country:fr": ("paris", "france"),
    "country:nl": ("amsterdam", "netherlands"),
    "country:ch": ("zurich", "switzerland"),
    "country:il": ("tel aviv", "israel"),
    "country:in": ("bengaluru", "bangalore", "hyderabad", "india"),
    "country:sg": ("singapore",),
    "country:jp": ("tokyo", "japan"),
    "country:au": ("sydney", "australia"),
}

_LOCK = threading.Lock()
_ACTION_ID: dict = {"value": None}
_COUNT: dict = {"value": None, "at": 0.0}
_ID_RX = re.compile(r'createServerReference\)\("([0-9a-f]{20,})",[^)]*?"' + ACTION + r'"\)')


class A16zError(RuntimeError):
    pass


def _find_action_id(client: httpx.Client) -> str:
    """The action's current id, read from the scripts the jobs page loads."""
    page = client.get(f"{SITE}/jobs", headers=_UA).text
    for path in dict.fromkeys(re.findall(r'/_next/static/chunks/[^"\']+?\.js', page)):
        hit = _ID_RX.search(client.get(SITE + path, headers=_UA).text)
        if hit:
            return hit.group(1)
    raise A16zError("The a16z jobs board changed and can't be searched right now.")


def _parse(text: str) -> dict | None:
    """The page of roles out of a React Server Components reply: one line per
    value, `<id>:<json>`, and the page is the line that carries `jobs`."""
    for line in text.splitlines():
        head, _, body = line.partition(":")
        if not re.fullmatch(r"[0-9a-f]+", head) or not body.startswith("{"):
            continue
        try:
            value = json.loads(body)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("jobs"), list):
            return value
    return None


def _call(args: dict, page: int, limit: int = PAGE, client: httpx.Client | None = None) -> dict:
    own = client is None
    client = client or httpx.Client(timeout=25, follow_redirects=True)
    try:
        # A deploy retires the action id; the second attempt reads the new one.
        for _ in range(2):
            with _LOCK:
                action = _ACTION_ID["value"]
            try:
                if not action:
                    action = _find_action_id(client)
                    with _LOCK:
                        _ACTION_ID["value"] = action
                reply = client.post(
                    f"{SITE}/jobs",
                    headers={**_UA, "Next-Action": action, "Accept": "text/x-component",
                             "Content-Type": "text/plain;charset=UTF-8"},
                    content=json.dumps([args, {"page": page, "limit": limit}]),
                )
            except httpx.HTTPError as exc:
                raise A16zError(
                    "The a16z jobs board is not reachable right now. Try again."
                ) from exc
            data = _parse(reply.text) if reply.status_code == 200 else None
            if data is not None:
                return data
            log.warning("a16z %s refused (HTTP %s); re-reading its id", ACTION, reply.status_code)
            with _LOCK:
                _ACTION_ID["value"] = None
        raise A16zError("The a16z jobs board could not run this search. Try again.")
    finally:
        if own:
            client.close()


def open_roles(client: httpx.Client | None = None) -> int | None:
    """The headline count. Cached, and never allowed to fail the page."""
    if _COUNT["value"] is not None and time.time() - _COUNT["at"] < 600:
        return _COUNT["value"]
    try:
        total = _call({"sort": "recent"}, 0, limit=1, client=client).get("total")
        _COUNT.update(value=int(total), at=time.time())
    except (A16zError, TypeError, ValueError):
        pass
    return _COUNT["value"]


def place_for(location: str) -> str:
    """The board's place for a preference location, or "" when it has none."""
    low = f" {re.sub(r'[^a-z ]', ' ', location.lower())} "
    for value, words in _PLACE_WORDS.items():
        if any(f" {w} " in low for w in words):
            return value
    return ""


def defaults(prefs: dict) -> dict:
    """The first search: all of the US, the broadest title, the rest from preferences."""
    titles = [t for t in prefs.get("titles") or [] if t.strip()]
    return {
        "term": min(titles, key=len) if titles else "",
        "role": "",
        "region": "us",
        "cities": [],
        "preferred": list(dict.fromkeys(
            p for p in (place_for(x) for x in prefs.get("locations") or []) if p
        ))[:MAX_CITIES],
        "remote": bool(prefs.get("remote_only")),
        "min_salary": 0,
    }


def _args(filters: dict) -> dict:
    if filters.get("region") not in (None, "", "us"):
        raise ValueError("Choose a region from the list.")
    args: dict = {"sort": "recent"}
    term = str(filters.get("term") or "").strip()[:200]
    if term:
        args["q"] = term
    if filters.get("role"):
        if filters["role"] not in ROLE_TYPES:
            raise ValueError("Choose a role type from the list.")
        args["roles"] = [filters["role"]]
    if filters.get("city"):
        if filters["city"] not in CITIES:
            raise ValueError("Choose a place from the list.")
        args["locations"] = [filters["city"]]
    elif filters.get("region") == "us":
        args["locations"] = ["country:us"]
    if filters.get("remote"):
        args["remote"] = True
    if int(filters.get("min_salary") or 0) > 0:
        args["salaryMin"] = int(filters["min_salary"])
    return args


def posting_url(url: str) -> str:
    """The posting, not its form. a16z links Ashby and Lever roles at their apply
    step (`…/<id>/application`, `…/<id>/apply`); without the step the URL is the
    one the native adapters track, so duplicate checks see the same role, and its
    last segment is the posting id rather than the word "application"."""
    return re.sub(r"/(application|apply)/?$", "", url.split("#")[0])


def _ms(iso: str | None) -> int | None:
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _text(value) -> str:  # noqa: ANN001
    # React Server Components escape a leading "$" as "$$".
    text = str(value or "").strip()
    return text[1:] if text.startswith("$$") else text


def _view(raw: dict, prefs: dict, tracked: dict, handled: set, board: dict) -> dict | None:
    url = posting_url(str(raw.get("apply_url") or ""))
    title = _text(raw.get("title"))
    if raw.get("active") is False or not raw.get("id"):
        return None
    if not url.startswith("https://") or not title:
        return None
    url = cosign.career_ops.canonical(url)
    state, pk = cosign.state_of(url, title, tracked, handled, board)
    yearly_usd = raw.get("salary_currency") == "USD" and raw.get("salary_period") in (None, "year")
    return {
        "id": "a16z:" + str(raw["id"]),
        "posting_id": str(raw.get("ats_posting_id") or ""),
        "title": title,
        "company": _text(raw.get("company_name")),
        "logo": raw.get("company_logo") or "",
        "location": _text(raw.get("location")),
        "remote": bool(raw.get("remote")),
        "team": _text(raw.get("team") or raw.get("department")),
        "employment": _text(raw.get("employment_type")),
        "salary_min": raw.get("salary_min") if yearly_usd else None,
        "salary_max": raw.get("salary_max") if yearly_usd else None,
        "posted": _ms(raw.get("posted_at")),
        "url": url,
        "description": "",
        "excluded": cosign.excluded_by(title, prefs),
        "state": state,
        "pk": pk,
    }


def search(filters: dict, cursor: str | None = None, client: httpx.Client | None = None) -> dict:
    try:
        page = max(0, int(cursor or 0))
    except ValueError as exc:
        raise ValueError("That page of results has expired. Search again.") from exc
    reply = _call(_args(filters), page, client=client)
    prefs, tracked, handled, board = cosign.known()
    jobs = [row for raw in reply.get("jobs") or []
            if (row := _view(raw, prefs, tracked, handled, board)) is not None]
    done = bool(reply.get("isDone")) or not reply.get("jobs")
    # Picked ids resolve against what this server fetched, as with Cosign.
    cosign._remember(jobs)
    return {"jobs": jobs, "cursor": None if done else str(page + 1), "done": done,
            "total": reply.get("total")}


def stage(ids: list[str], *, apply_requested: bool = False) -> dict:
    if any(not str(i).startswith("a16z:") for i in ids):
        raise ValueError("Choose roles from the a16z results.")
    return cosign.stage(ids, apply_requested=apply_requested, network="a16z")
