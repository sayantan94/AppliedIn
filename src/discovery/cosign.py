"""Cosign (cosign.co/jobs): a live search over ~70k roles, seeded from preferences.

Cosign indexes public ATS boards (Ashby, Greenhouse, Workday, …) and exposes the
same search its own signed-out visitors use: a public Convex action. Every result
carries the ATS posting URL and the full description, so a picked role can enter
the pipeline without a crawl.

Nothing here applies. A picked role is written onto the Career Ops board and
handed to `career_ops.prepare`, so it inherits that path's guarantees rather than
restating them: the tracked-job and seen-ledger duplicate checks, the
`career_ops` discovery source that makes the pipeline prepare-only, and the rule
that only an explicit Apply authorizes a submission.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict

import httpx

from discovery import career_ops

log = logging.getLogger(__name__)

CONVEX = "https://cheerful-donkey-418.convex.cloud"
# Cosign's own client asks for 30; 100 answered "Server Error" when probed.
PAGE = 30

ROLE_TYPES = {
    "engineering": "Engineering", "product": "Product", "design": "Design",
    "data": "Data & analytics", "security": "Security", "research": "Research & science",
    "executive": "Executive", "talent": "People & recruiting", "sales": "Sales",
    "customer_success": "Customer success", "marketing": "Marketing", "finance": "Finance",
    "legal": "Legal & compliance", "operations": "Operations", "healthcare": "Healthcare",
    "education": "Education", "investing": "Investing", "other": "Other",
}

# Cosign's own city vocabulary. An unknown slug is a server error rather than an
# empty page (e.g. "bellevue"), so a city must come from this list.
CITIES = {
    "san-francisco": "San Francisco", "new-york": "New York", "seattle": "Seattle",
    "los-angeles": "Los Angeles", "boston": "Boston", "austin": "Austin",
    "chicago": "Chicago", "washington-dc": "Washington, DC", "denver": "Denver",
    "san-diego": "San Diego", "portland": "Portland", "san-jose": "San Jose",
    "palo-alto": "Palo Alto", "mountain-view": "Mountain View", "oakland": "Oakland",
    "sacramento": "Sacramento", "santa-monica": "Santa Monica", "irvine": "Irvine",
    "atlanta": "Atlanta", "miami": "Miami", "dallas": "Dallas", "houston": "Houston",
    "philadelphia": "Philadelphia", "salt-lake-city": "Salt Lake City",
    "phoenix": "Phoenix", "raleigh": "Raleigh", "boulder": "Boulder",
    "pittsburgh": "Pittsburgh", "minneapolis": "Minneapolis", "nashville": "Nashville",
    "toronto": "Toronto", "vancouver": "Vancouver", "montreal": "Montreal",
    "london": "London", "dublin": "Dublin", "berlin": "Berlin", "paris": "Paris",
    "amsterdam": "Amsterdam", "zurich": "Zurich", "munich": "Munich",
    "stockholm": "Stockholm", "tel-aviv": "Tel Aviv", "singapore": "Singapore",
    "bengaluru": "Bengaluru", "hyderabad": "Hyderabad", "tokyo": "Tokyo",
    "sydney": "Sydney",
}
US_CITIES = {
    "san-francisco", "new-york", "seattle", "los-angeles", "boston", "austin", "chicago",
    "washington-dc", "denver", "san-diego", "portland", "san-jose", "palo-alto",
    "mountain-view", "oakland", "sacramento", "santa-monica", "irvine", "atlanta", "miami",
    "dallas", "houston", "philadelphia", "salt-lake-city", "phoenix", "raleigh", "boulder",
    "pittsburgh", "minneapolis", "nashville",
}
_US_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY",
}
_US_STATES = re.compile(
    r"\b(alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|"
    r"hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|"
    r"massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|"
    r"new hampshire|new jersey|new mexico|north carolina|north dakota|ohio|oklahoma|oregon|"
    r"pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|"
    r"virginia|washington|west virginia|wisconsin|wyoming|united states|usa)\b",
    re.I,
)


def is_us(location: str, cities: list | None = None) -> bool:
    """Whether a posting lists at least one US place.

    Cosign has no country filter, so "All of the US" is an unfiltered search kept
    to postings that name a US city Cosign recognised, a state, or the country.
    A place that names none of those ("Tysons Corner") is missed rather than
    guessed at: a false match would put a foreign role in front of the owner.
    """
    if any((c or {}).get("slug") in US_CITIES for c in cities or []):
        return True
    text = location or ""
    if _US_STATES.search(text) or re.search(r"\bUS\b|\bU\.S\.", text):
        return True
    return any(code in _US_CODES for code in re.findall(r"(?:,|-)\s*([A-Z]{2})\b", text))


# Cosign accepts one city per search (a list is a server error), so several cities
# mean several searches. Five keeps a fan-out polite to a free public index.
MAX_CITIES = 5
# Preference locations that name a place Cosign files under another city.
_CITY_ALIASES = {
    "bellevue": "seattle", "redmond": "seattle", "kirkland": "seattle",
    "washington": "seattle", "bay area": "san-francisco", "sf": "san-francisco",
    "california": "san-francisco", "nyc": "new-york", "bangalore": "bengaluru",
}

_LOCK = threading.Lock()
# Picked ids are resolved against what this server fetched, never against a URL
# the browser sends back, so a staged role is always exactly what Cosign returned.
# Memory alone was not enough: every daemon restart forgot the page the owner was
# looking at, and pressing Apply on it failed. Redis keeps each result a week.
_SEEN: OrderedDict[str, dict] = OrderedDict()
_TTL = 7 * 24 * 3600
_SEEN_MAX = 3000
# "Select all" over a filled page is ~100 roles, plus a "Load more" or two.
STAGE_MAX = 200
_COUNT: dict = {"value": None, "at": 0.0}


class CosignError(RuntimeError):
    pass


def _call(kind: str, path: str, args: dict, client: httpx.Client | None = None):
    body = {"path": path, "args": args, "format": "json"}
    own = client is None
    client = client or httpx.Client(timeout=20)
    try:
        # Cosign's own client retries a bare "Server Error" twice; the index
        # answers one of those on a cold shard and succeeds straight after.
        for delay in (0.35, 1.0, None):
            try:
                reply = client.post(f"{CONVEX}/api/{kind}", json=body)
                data = reply.json()
            except (httpx.HTTPError, ValueError) as exc:
                if delay is None:
                    raise CosignError(
                        "The Cosign network is not reachable right now. Try again."
                    ) from exc
            else:
                if data.get("status") == "success":
                    return data.get("value")
                if delay is None or "Server Error" not in str(data.get("errorMessage")):
                    log.warning("cosign %s failed: %s", path, data.get("errorMessage"))
                    raise CosignError(
                        "The Cosign network could not run this search. Try other filters."
                    )
            time.sleep(delay)
    finally:
        if own:
            client.close()


def open_roles(client: httpx.Client | None = None) -> int | None:
    """The headline count. Cached, and never allowed to fail the page."""
    if _COUNT["value"] is not None and time.time() - _COUNT["at"] < 600:
        return _COUNT["value"]
    try:
        _COUNT.update(value=int(_call("query", "jobs:openRoleCount", {}, client)), at=time.time())
    except (CosignError, TypeError, ValueError):
        pass
    return _COUNT["value"]


def _city_for(locations: list[str]) -> str:
    for place in locations:
        key = place.lower().replace("(us)", "").strip()
        for slug, label in CITIES.items():
            if key == label.lower():
                return slug
        if key in _CITY_ALIASES:
            return _CITY_ALIASES[key]
    return ""


def defaults(prefs: dict) -> dict:
    """The first search: all of the US, with the rest drawn from preferences.

    The shortest title is the broadest term: "Software Engineer" also returns the
    Senior and Staff roles a longer title would narrow away.
    """
    titles = [t for t in prefs.get("titles") or [] if t.strip()]
    return {
        "term": min(titles, key=len) if titles else "",
        "role": "",
        "region": "us",
        "cities": [],
        # A one-tap shortcut in the city picker, not the default.
        "preferred": list(dict.fromkeys(
            c for c in (_city_for([p]) for p in prefs.get("locations") or []) if c
        ))[:MAX_CITIES],
        "remote": bool(prefs.get("remote_only")),
        "min_salary": 0,
    }


def examples(prefs: dict) -> list[str]:
    """What the empty search box cycles through: the owner's own searches.

    A bare keyword ("ai", "agentic") reads as noise, so only phrases are paired
    with the broadest title.
    """
    titles = [t for t in prefs.get("titles") or [] if t.strip()]
    base = min(titles, key=len) if titles else "Software Engineer"
    phrases = [k for k in prefs.get("include_keywords") or [] if len(k.split()) > 1]
    places = [p for p in prefs.get("locations") or [] if _city_for([p])]
    out = [
        *titles[:3],
        *(f"{base}, {k}" for k in phrases[:3]),
        *(f"{base} in {p}" for p in places[:1]),
    ]
    return list(dict.fromkeys(out))


def excluded_by(title: str, prefs: dict) -> str:
    """The exclude keyword this title hits, or "".

    Preferences call these "never a fit". Word boundaries keep "ios" from
    matching "Bios" and "intern" from matching "internal".
    """
    low = title.lower()
    for word in prefs.get("exclude_keywords") or []:
        word = word.strip().lower()
        if word and re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", low):
            return word
    return ""


def _args(filters: dict, cursor: str | None) -> dict:
    if filters.get("region") not in (None, "", "us"):
        raise ValueError("Choose a region from the list.")
    args: dict = {"cursor": cursor or None, "limit": PAGE}
    term = str(filters.get("term") or "").strip()[:200]
    if term:
        args["term"] = term
    if filters.get("role"):
        if filters["role"] not in ROLE_TYPES:
            raise ValueError("Choose a role type from the list.")
        args["roleType"] = filters["role"]
    if filters.get("city"):
        if filters["city"] not in CITIES:
            raise ValueError("Choose a city from the list.")
        args["city"] = filters["city"]
    if filters.get("remote"):
        args["remoteOnly"] = True
    if int(filters.get("min_salary") or 0) > 0:
        args["minSalaryUsd"] = int(filters["min_salary"])
    return args


def _view(raw: dict, prefs: dict, tracked: dict, handled: set, board: dict) -> dict | None:
    url = str(raw.get("url") or "")
    if raw.get("closed") or not url.startswith("https://") or not raw.get("title"):
        return None
    url = career_ops.canonical(url)
    state, pk = state_of(url, raw["title"], tracked, handled, board)
    return {
        "id": str(raw["postingId"]),
        "title": raw["title"].strip(),
        "company": raw.get("organizationName") or "",
        "logo": raw.get("organizationImageUrl") or "",
        "location": raw.get("location") or "",
        "remote": raw.get("remote"),
        "team": raw.get("team") or "",
        "employment": raw.get("employmentType") or "",
        "salary_min": raw.get("salaryMinUsd"),
        "salary_max": raw.get("salaryMaxUsd"),
        "posted": raw.get("postedAt") or raw.get("firstSeenAt"),
        "url": url,
        "description": raw.get("description") or "",
        "excluded": excluded_by(raw["title"], prefs),
        "state": state,
        "pk": pk,
    }


def known() -> tuple[dict, dict, set, dict]:
    """What a result is checked against: preferences, the URLs already tracked
    (to their application), the URLs already handled, and the Career Ops board."""
    prefs = career_ops.search_preferences()
    stores = career_ops.make_stores()
    tracked = {
        career_ops.canonical(r["jd_url"]): r["pk"] for r in stores.tracking.all() if r.get("jd_url")
    }
    from tools import seen

    handled = {career_ops.canonical(u) for u in seen.load()}
    with career_ops._LOCK:
        board = career_ops._read()["jobs"]
    return prefs, tracked, handled, board


def state_of(url: str, title: str, tracked: dict, handled: set, board: dict) -> tuple[str, str]:
    """A result's state ("new", "pipeline", "handled", …) and its application, if any."""
    identity = career_ops.normalize({"url": url, "title": title}, "", "cosign")["id"]
    if url in tracked:
        return "pipeline", tracked[url]
    if url in handled:
        return "handled", ""
    if identity in board and board[identity]["state"] != "new":
        return board[identity]["state"], board[identity].get("pk", "")
    return "new", ""


def _redis():
    r = getattr(career_ops.make_stores().tracking, "r", None)
    return r if hasattr(r, "mget") else None


def _remember(rows: list[dict]) -> None:
    with _LOCK:
        for row in rows:
            _SEEN[row["id"]] = row
            _SEEN.move_to_end(row["id"])
        while len(_SEEN) > _SEEN_MAX:
            _SEEN.popitem(last=False)
    r = _redis()
    if r is not None:
        pipe = r.pipeline()
        for row in rows:
            pipe.set(f"cosign:job:{row['id']}", json.dumps(row), ex=_TTL)
        pipe.execute()


def _recall(ids: list[str]) -> list[dict | None]:
    with _LOCK:
        found = [_SEEN.get(i) for i in ids]
    missing = [i for i, row in zip(ids, found, strict=True) if row is None]
    r = _redis() if missing else None
    if r is not None:
        keys = [f"cosign:job:{i}" for i in missing]
        stored = dict(zip(missing, r.mget(keys), strict=True))
        found = [
            row or (json.loads(stored[i]) if stored.get(i) else None)
            for i, row in zip(ids, found, strict=True)
        ]
    return found


def search(filters: dict, cursor: str | None = None, client: httpx.Client | None = None) -> dict:
    # A city already names a place, so the US filter only applies without one.
    us_only = filters.get("region") == "us" and not filters.get("city")
    prefs, tracked, handled, board = known()
    jobs, done = [], False
    # Filtering to the US can empty a page of a global index, so read on until
    # there is a useful page or a bounded number of requests has been spent.
    for _ in range(4 if us_only else 1):
        page = _call("action", "jobSearchAction:search", _args(filters, cursor), client) or {}
        for raw in page.get("page") or []:
            if us_only and not is_us(raw.get("location", ""), raw.get("cities")):
                continue
            row = _view(raw, prefs, tracked, handled, board)
            if row is None:
                continue
            jobs.append(row)
        done = bool(page.get("isDone"))
        cursor = None if done else page.get("continueCursor")
        if done or len(jobs) >= PAGE // 2:
            break
    _remember(jobs)
    return {"jobs": jobs, "cursor": cursor, "done": done}


def stage(ids: list[str], *, apply_requested: bool = False, network: str = "cosign") -> dict:
    """Put picked roles on the board, then hand them to the Career Ops path.

    `network` names the index the roles came from (a key of `career_ops.INDEXES`).
    Both indexes share this path, so their roles get the same guarantees.
    """
    ids = list(dict.fromkeys(ids))
    if not ids or len(ids) > STAGE_MAX:
        raise ValueError(f"Choose between 1 and {STAGE_MAX} roles at a time.")
    picked = _recall(ids)
    if any(p is None for p in picked):
        raise ValueError("Some of these results are over a week old. Search again to refresh them.")
    board_ids = []
    with career_ops._LOCK:
        data = career_ops._read()
        for job in picked:
            salary = _salary(job)
            row = career_ops.normalize(
                {
                    "url": job["url"],
                    "title": job["title"],
                    "description": job["description"],
                    "location": job["location"],
                    "postedAt": job["posted"],
                    # The index's own posting id, where the URL's last segment is
                    # not one (an ADP page, a careers page keyed by query).
                    "id": job.get("posting_id") or None,
                    "why": f"Found in the {career_ops.INDEXES[network]}"
                    + (f" · {salary}" if salary else ""),
                    "verification": network,
                },
                job["company"],
                network,
            )
            old = data["jobs"].get(row["id"])
            if old:
                row.update({k: old[k] for k in ("state", "pk", "first_seen", "job_id") if k in old})
            data["jobs"][row["id"]] = row
            board_ids.append(row["id"])
        career_ops._write(data)
    # prepare() takes 50 at a time. Batching here, not in the browser, keeps a
    # large selection to ONE apply run, so roles are submitted one after another
    # rather than by several runs racing each other.
    result = {"prepared": 0, "duplicates": 0, "pks": []}
    for at in range(0, len(board_ids), 50):
        part = career_ops.prepare(board_ids[at : at + 50], apply_requested=apply_requested)
        result["prepared"] += part["prepared"]
        result["duplicates"] += part["duplicates"]
        result["pks"] += part["pks"]
    # Which tracked application each picked role became, duplicates included, so
    # the board can follow every one of them in its side pane.
    with career_ops._LOCK:
        rows = career_ops._read()["jobs"]
    pairs = zip(picked, board_ids, strict=True)
    result["tracked"] = {job["id"]: rows[bid]["pk"] for job, bid in pairs if rows[bid].get("pk")}
    with _LOCK:
        for job in picked:
            job["state"] = "pipeline"
            job["pk"] = result["tracked"].get(job["id"], job.get("pk", ""))
    return result


def _salary(job: dict) -> str:
    low, high = job.get("salary_min"), job.get("salary_max")
    fmt = lambda v: f"${round(v / 1000)}K"  # noqa: E731
    if low and high:
        return f"{fmt(low)}–{fmt(high)}"
    return fmt(low or high) if (low or high) else ""


STEPS = ("Score", "Tailor", "Check", "Apply", "Submitted")


def _steps(prog: dict, row: dict) -> dict:
    """Where an application is on Score → Tailor → Check → Apply → Submitted."""
    phase, status = prog["phase"], row.get("status", "")
    if phase == "applied":
        return {"at": 4, "state": "done"}
    if phase == "attention":
        if status == "skipped":
            return {"at": 0, "state": "stopped"}
        if row.get("fail_kind") == "no_resume":
            return {"at": 1, "state": "stopped"}
        return {"at": 3, "state": "stopped"}
    at = {"queued": 0, "score": 0, "tailor": 2 if prog["label"] == "Checking résumé" else 1,
          "waiting": 3, "apply": 3}.get(phase, 0)
    return {"at": at, "state": "waiting" if phase in ("queued", "waiting") else "active"}


def _moment(event: dict) -> dict | None:
    """One line of an application's timeline, in words, or None for plumbing."""
    kind, agent = event.get("kind", ""), event.get("agent", "")
    detail = str(event.get("detail") or "").strip()
    text, tone = "", ""
    if kind == "discovered":
        # Older events say "Cosign:"; newer ones "Cosign network:".
        text = ("Added from the Cosign network" if detail.startswith("Cosign")
                else "Added from the a16z portfolio" if detail.startswith("a16z")
                else "Added to the pipeline")
    elif kind == "running" and not agent:
        text = "You asked to apply" if detail.startswith("Apply selected") else ""
    elif kind == "response" and agent == "scorer":
        try:
            verdict = json.loads(detail)
            text = f"Scored {verdict.get('score')}/10 — {verdict.get('reasoning', '')}"
        except (ValueError, AttributeError):
            text = "Scored against your preferences"
    elif kind == "result" and agent == "tailor" and detail == "save_tailored_resume":
        text = "Résumé tailored and saved"
    elif kind == "result" and agent == "critic" and detail == "exit_loop":
        text = "Résumé checked"
    elif kind == "running" and agent in ("applier", "browser"):
        # The browser also streams its raw JSON report; the "applied" event
        # already says the same thing in words.
        text = "" if detail.startswith("{") else detail.replace("🌐 ", "")
    elif kind == "gate":
        text, tone = detail, "ask"
    elif kind in ("error", "failed"):
        text, tone = detail, "bad"
    elif kind == "applied":
        text, tone = detail or "Applied", "good"
    if not text:
        return None
    return {"at": event.get("at", ""), "text": (text[:1].upper() + text[1:])[:400], "tone": tone}


def progress(pks: list[str]) -> dict:
    """Live state of roles the owner sent from this board, for the side pane.

    Read only: the same receipts Career Ops shows, plus the step each one has
    reached and a readable timeline of what happened to it.
    """
    from core.apply_queue import ApplyQueue
    from core.events import recent
    from discovery.career_progress import application_progress

    stores = career_ops.make_stores()
    wanted = set(pks)
    latest: dict = {}
    timeline: dict = {}
    for event in recent(3000):  # newest first
        pk = event.get("pk")
        if pk not in wanted:
            continue
        if event.get("agent") in ("scorer", "tailor", "critic", "applier", "browser"):
            latest.setdefault(pk, event)
        moment = _moment(event)
        if moment and len(timeline.setdefault(pk, [])) < 30:
            timeline[pk].append(moment)
    tracking = stores.tracking
    flight = ApplyQueue(tracking.r).in_flight() if hasattr(tracking, "r") else set()
    out = {}
    for pk in dict.fromkeys(pks):
        row = tracking.get(pk)
        if not row:
            continue
        prog = application_progress(row, latest.get(pk), in_flight=pk in flight)
        out[pk] = {
            **prog,
            "steps": _steps(prog, row),
            "timeline": list(reversed(timeline.get(pk, []))),
            "title": row.get("title", ""),
            "company": row.get("company", ""),
            "url": row.get("jd_url", ""),
            "requested": bool(row.get("apply_requested_at")),
        }
    return out
