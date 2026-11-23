"""Local web server — serves the dashboard UI + a data API over the store.

In local mode the daemon starts this: it serves the static `web/` dashboard on
localhost and exposes the same endpoints the dashboard calls (`/applications`,
`/stats`, `/actions/*`), reading the Redis-backed tracking store. In cloud the
dashboard is on Vercel and hits an API Gateway with the same routes over
DynamoDB — same contract, different host.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request, HTTPException
from fastapi.responses import Response, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.config import get_settings
from core.models import Status
from core.stores import make_stores
from core.ids import is_internal_pk
from tools import submit_hold

_WEB = Path(__file__).resolve().parents[1] / "web"

# In-process flags so the dashboard can show whether a manual run is active and
# grey out its button. Background tasks run in this same process, so a plain dict
# is enough — no need to round-trip through Redis for a transient UI hint.
_RUNNING = {"discover": False, "process": False}


# Slow-request log. The daemon has no access log, so a saturated interpreter
# gave no clue which requests were costing it. Any request slower than this is
# logged with its method, path, wall time and the CPU its endpoint used on its
# own thread (a sync endpoint's worker thread; for an async endpoint, the event
# loop thread, which includes work interleaved with it).
_SLOW_REQUEST_S = 1.0
_wall_clock = time.perf_counter
_request_cpu: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "_request_cpu", default=None)
_timing_log = logging.getLogger("server.timing")


def _cpu_timed(call):  # noqa: ANN001, ANN202
    """Wrap an endpoint so it reports its own thread CPU to the current request."""
    if getattr(call, "_cpu_timed", False):
        return call
    if inspect.iscoroutinefunction(call):
        @functools.wraps(call)
        async def timed(*args, **kwargs):  # noqa: ANN202
            sink, start = _request_cpu.get(), time.thread_time()
            try:
                return await call(*args, **kwargs)
            finally:
                if sink is not None:
                    sink.append(time.thread_time() - start)
    else:
        @functools.wraps(call)
        def timed(*args, **kwargs):  # noqa: ANN202
            sink, start = _request_cpu.get(), time.thread_time()
            try:
                return call(*args, **kwargs)
            finally:
                if sink is not None:
                    sink.append(time.thread_time() - start)
    timed._cpu_timed = True
    return timed


def _install_request_timing(app: FastAPI) -> None:
    """Time every API route registered so far and log the slow requests."""
    from fastapi.routing import APIRoute

    for route in app.routes:
        if isinstance(route, APIRoute) and route.dependant.call is not None:
            route.dependant.call = _cpu_timed(route.dependant.call)

    @app.middleware("http")
    async def _log_slow_requests(request, call_next):  # noqa: ANN001, ANN202
        sink: list = []
        token = _request_cpu.set(sink)
        start = _wall_clock()
        try:
            return await call_next(request)
        finally:
            _request_cpu.reset(token)
            wall = _wall_clock() - start
            if wall > _SLOW_REQUEST_S:
                cpu = f"{sum(sink):.3f}s" if sink else "n/a"
                _timing_log.warning("slow request: %s %s wall=%.3fs thread_cpu=%s",
                                    request.method, request.url.path, wall, cpu)


# A /stats poll runs every three seconds. Keep each JSON read batch bounded:
# the all-time count is already on the status index, so only the date filter
# needs to look at submitted rows.
_SUBMITTED_BATCH_SIZE = 256


def _count_submitted_today(tracking) -> int:
    """Count submitted rows dated today in the daemon host's local calendar."""
    local_today = datetime.now(timezone.utc).astimezone().date()
    redis = tracking.r
    members = redis.pipeline()
    members.smembers("status:applied")
    members.smembers("status:applied_manual")
    pks = list({
        pk.decode("utf-8") if isinstance(pk, bytes) else pk
        for group in members.execute() for pk in group
    })

    today = 0
    for start in range(0, len(pks), _SUBMITTED_BATCH_SIZE):
        batch = redis.pipeline()
        for pk in pks[start:start + _SUBMITTED_BATCH_SIZE]:
            batch.get(f"app:{pk}")
        for raw in batch.execute():
            if not raw:
                continue
            try:
                row = json.loads(raw)
                stamp = row.get("applied_at") if isinstance(row, dict) else None
                if not isinstance(stamp, str) or not stamp.strip():
                    continue
                applied_at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if applied_at.tzinfo is not None and applied_at.astimezone().date() == local_today:
                    today += 1
            except (TypeError, ValueError):
                # Legacy/invalid timestamps are never today, but the status
                # index still includes their rows in the all-time total.
                continue
    return today


def _discovery_running() -> bool:
    """True while ANY discovery is in flight, not just one this module started.

    The daemon's scheduled sweep calls `run_discovery` directly and never touches
    the flag above, so a scheduled scan used to be invisible here: the dashboard
    showed idle, the stop control stayed hidden, and pressing Discover reported
    success while the single-flight lock quietly refused it. The lock is the real
    answer to "is a scan running", so ask it.
    """
    from discovery import handler as _handler

    return bool(_RUNNING["discover"] or _handler.scanning())


def _scanning_company(name: str) -> bool:
    """True while THIS company is being scanned. The question worth asking before
    refusing a scan — that another company is being scanned is not a reason."""
    from discovery import handler as _handler

    return (name or "").strip().lower() in _handler.scanning()


def _rescreen_company(name: str) -> int:
    """Re-apply a company's CURRENT rules to the jobs already on its board.

    Only `found` and rules-skipped rows move. Anything tailored, applied or in
    flight is left exactly as it is: a résumé already written is work the owner
    paid for, and a submitted application cannot be unsubmitted by a filter.
    """
    from core import flags
    from discovery.relevance import relevant
    from discovery.watchlist import load_preferences

    settings = get_settings()
    base = load_preferences(Path(settings.config_dir) / "preferences.yaml")
    prefs = flags.effective_prefs(name, base)
    kws = flags.company_filter(name)

    stores = make_stores(settings)
    low = (name or "").strip().lower()
    rows = [r for r in stores.tracking.all()
            if (r.get("company") or "").lower() == low
            and r.get("status") in ("found", "skipped")
            and not is_internal_pk(r.get("pk", ""))]
    if not rows:
        return 0

    # One screen call for the whole set rather than one per row: the relevance
    # screen is a model call and this can be a hundred jobs.
    from core.models import JobRecord

    cands = [JobRecord(company=name, job_id=str(r["pk"]).split("#", 1)[-1],
                       title=r.get("title") or "", jd_url=r.get("jd_url") or "",
                       jd_text=r.get("jd_text") or r.get("title") or "", ats="custom")
             for r in rows]
    keep = {j.jd_url or j.title for j in relevant(cands, prefs)}
    if kws:
        keep = {k for k in keep
                if any(flags.title_matches_filter(t, kws)
                       for t in [k] + [r.get("title", "") for r in rows
                                       if (r.get("jd_url") or r.get("title")) == k])}

    moved = 0
    for r in rows:
        key = r.get("jd_url") or r.get("title") or ""
        ok = key in keep
        st = r.get("status")
        if st == "found" and not ok:
            stores.tracking.set_status(r["pk"], Status.SKIPPED, skip_reason="preferences")
            moved += 1
        elif st == "skipped" and r.get("skip_reason") in ("preferences", "title_filter") and ok:
            stores.tracking.set_status(r["pk"], Status.FOUND, skip_reason="")
            moved += 1
    return moved


def _applying_now() -> int:
    """Applications being filled at this moment, from the queue's leases."""
    try:
        from core.apply_queue import ApplyQueue

        return len(ApplyQueue(make_stores(get_settings()).tracking.r).depth()["running"])
    except Exception:  # noqa: BLE001 — a stat must never break the dashboard
        return 0


def _throttle_state() -> dict:
    """Whether model calls are being held back, and for how long.

    A pipeline that has gone quiet because it is waiting out a rate limit looks
    identical to one that is stuck, so it says which.
    """
    from core.throttle import state

    return state()


def _company_from_url(url: str) -> str:
    """A usable employer name from a job URL, when nothing better is known.

    Boards put the employer in different places, and taking the wrong one names
    the row after the software: `oracle.wd5.myworkdayjobs.com` became "Myworkdayjobs"
    and `boards.greenhouse.io/stripe` would become "Greenhouse". A row named after
    the ATS cannot be filtered, queued per company, or matched to per-company
    preferences — it is effectively lost on the board.

      greenhouse / lever / ashby   first path segment    /stripe/jobs/1  -> Stripe
      workday                      FIRST subdomain       oracle.wd5.…    -> Oracle
      anything else                the registered name   careers.acme.com -> Acme
    """
    from urllib.parse import urlparse

    u = urlparse(url if "://" in url else "https://" + url)
    host = (u.hostname or "").lower()
    parts = [p for p in (u.path or "").split("/") if p]

    if any(b in host for b in ("greenhouse.io", "lever.co", "ashbyhq.com")):
        # The embed form carries the employer in ?for=, and its path is all board
        # words — without this it reads the software's name off the host instead.
        from urllib.parse import parse_qs

        if (for_ := parse_qs(u.query or "").get("for", [""])[0].strip()):
            return for_.replace("-", " ").replace("_", " ").title()
        for p in parts:
            if p not in ("embed", "job_app", "jobs", "job", "en", "careers"):
                return p.replace("-", " ").replace("_", " ").title()
    if "myworkdayjobs.com" in host or "workday.com" in host:
        first = host.split(".")[0]
        if first not in ("www", "jobs", "careers"):
            return first.replace("-", " ").title()
    # A vendor host names the employer nowhere useful: eeho.fa.us2.oraclecloud.com
    # is Oracle's own portal, and "Oraclecloud" is the product, not the employer.
    for vendor, name in (("oraclecloud.com", "Oracle"), ("myworkday", ""),
                         ("icims.com", ""), ("smartrecruiters.com", "")):
        if vendor in host and name:
            return name
    bits = [b for b in host.split(".") if b not in ("www", "jobs", "careers", "apply", "boards")]
    # Drop the public suffix: acme.co.uk -> Acme, careers.acme.com -> Acme.
    if len(bits) >= 2 and bits[-2] in ("co", "com", "org", "net", "ac"):
        bits = bits[:-2]
    elif len(bits) >= 1:
        bits = bits[:-1]
    return (bits[-1].replace("-", " ").title() if bits else "Company")


def _closed_reason(row: dict) -> str | None:
    """A plain sentence for why a job is in a terminal state — so the UI can say
    WHY something closed instead of just showing a grey tag."""
    status = row.get("status", "")
    if status == "skipped":
        reason, score = row.get("skip_reason"), row.get("match_score")
        if reason == "low_score":
            return f"Match score {score}/10 was below the bar — skipped before tailoring."
        if reason == "review_rejected":
            return "You rejected this role during review. Undo returns it to the review queue."
        if reason == "user_skipped":
            return "You skipped this one."
        return f"Skipped: {reason}." if reason else "Skipped."
    if status == "job_gone":
        return "The posting was taken down before we could apply."
    if status == "failed":
        return row.get("fail_reason") or "The application could not be submitted."
    if status == "error":
        return row.get("error") or "Hit an error during the pipeline (see the logs)."
    if status == "capped":
        return "Daily application cap reached — parked for the next run."
    return None


def _is_job_row(row: dict) -> bool:
    """Real application rows only. Internal bookkeeping (crawl watermarks,
    `meta#…`) shares the tracking table but isn't a job — hide it from the board
    so it doesn't render as a blank card or inflate the counts."""
    return not is_internal_pk(row.get("pk") or "")

def reply_ingest_label(label) -> str:
    from core.reply_classifier import validated_label
    return validated_label(label)
def _reply_sort_timestamp(value) -> float:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return float("-inf")





def _project_submit_hold(pk: str, raw_row: dict, tracking, *, no_independent_key=False) -> str:
    """Expose the existing hold decision without creating or clearing a hold.

    A missing Redis client does not prove a clear hold on local/unknown stores.
    Only the explicitly keyless cloud deployment can treat that as clear.
    """
    try:
        if submit_hold.blocked(pk, raw_row, tracking=tracking):
            return "blocked"
        if getattr(tracking, "r", None) is None:
            return "clear" if no_independent_key else "unknown"
        return "clear"
    except Exception:
        return "unknown"


def _owner_gate_tokens(row: dict) -> dict:
    """Opaque exact-question identity and generation for a bound direct gate.

    Neither leaks the field label, selector, URL or any value; the identity is
    a one-way digest and the generation is random.
    """
    from tools.browser_skill_apply import owner_question_identity, owner_skip_eligible
    pending = row.get("gate_pending") or {}
    question = pending.get("form_question")
    generation = pending.get("generation")
    if (row.get("status") != "needs_human" or row.get("gate_call_id") != "direct"
            or row.get("gate_source") != "applier" or not isinstance(question, dict)
            or not isinstance(generation, str) or not generation):
        return {"gate_question_identity": "", "gate_generation": "",
                "gate_skip_eligible": False}
    return {"gate_question_identity": owner_question_identity(row.get("pk") or "", question),
            "gate_generation": generation,
            "gate_skip_eligible": owner_skip_eligible(question)}


def _to_ui(row: dict, artifacts) -> dict:
    """Map a tracking row to the shape the dashboard expects."""
    def link(key):
        # Serve artifacts same-origin (/artifact/<key>) so the browser can load
        # them — a local file:// presign can't be shown from an http page. Keys
        # contain '#' (pk = company#job_id), so URL-encode it (# starts a
        # fragment) while keeping the path slashes.
        #
        # The key alone is not proof the bytes are still there. A row keeps its
        # key forever, but `.local/artifacts/` was recreated once while the rows
        # lived on in Redis and 127 of them lost their PDF. Linking regardless
        # put a 404 inside the viewer's iframe — a blank white panel, no error,
        # while the "No PDF stored for this application" message that says
        # exactly what happened sat unreachable behind a truthy resume_url.
        from urllib.parse import quote

        k = row.get(key)
        if not k or not artifacts.exists(k):
            return None
        # The key is stable and re-tailoring rewrites it in place, so the URL has
        # to say which version it is. Chrome's PDF viewer reuses a document it has
        # already rendered for a URL it has already seen, and the owner read the
        # previous résumé after re-tailoring one.
        v = artifacts.version(k)
        return f"/artifact/{quote(k, safe='/')}" + (f"?v={v}" if v else "")

    from tools import posting_guard as pg

    projected = pg.projection(row.get("jd_text") or "", row.get("posting_guard"))
    visible_guard = dict(projected["posting_guard"])
    visible_guard["messages"] = pg.reason_messages(visible_guard)
    events = row.get("events") or []
    return {
        "pk": row.get("pk"),
        "company": row.get("display_company") or row.get("company", ""),
        "title": row.get("title", ""),
        "status": row.get("status", ""),
        "match_score": row.get("match_score"),
        "score_override": row.get("score_override") is True,
        "gate_reason": row.get("gate_reason"),
        "gate_has_form_question": isinstance(
            (row.get("gate_pending") or {}).get("form_question"), dict),
        "gate_has_call_id": bool(row.get("gate_call_id")),
        **_owner_gate_tokens(row),
        **({"gate_generation": row.get("gate_generation"),
            "review_state": row.get("review_state"),
            "review_expires_at": row.get("review_expires_at")}
           if row.get("gate_reason") == "submit_review" else {}),
        "possible_submission": row.get("possible_submission") is True,
        "fail_kind": row.get("fail_kind") or "",
        "fail_reason": row.get("fail_reason") or "",
        "jd_read_error": row.get("jd_read_error") or "",
        "tailored_at": row.get("tailored_at") or next(
            (e.get("at", "") for e in events if e.get("status") == "tailored"), ""),
        "retailored_at": row.get("retailored_at") or "",
        "posted_at": row.get("posted_at") or "",
        "applied_at": row.get("applied_at") or "",
        "gate_question": (row.get("gate_pending") or {}).get("question"),
        "skip_reason": row.get("skip_reason"),
        "review_skipped": bool(row.get("review_skipped")),
        "review_rejected": row.get("skip_reason") == "review_rejected",
        "closed_reason": _closed_reason(row),
        "resume_version": row.get("resume_version"),
        "resume_url": link("resume_s3_key"),
        # What the box shows. The note in force IS the note in the box, so the
        # owner edits it rather than adding to an invisible pile.
        "tailor_note": row.get("tailor_note") or "",
        "reapplied_from": row.get("reapplied_from") or "",
        "reapplied_as": row.get("reapplied_as") or [],
        "reapply_n": row.get("reapply_n") or 0,
        "profile_id": row.get("profile_id") or "",
        "has_diff": bool(row.get("resume_tex_key")),
        "jd_url": row.get("jd_url"),
        # The posting itself, so the owner can read what the résumé was tailored
        # against without leaving the board and losing their place.
        "jd_text": projected["text"][:20000],
        "posting_guard": visible_guard,
        "location": row.get("location", ""),
        "profile_id": row.get("profile_id", ""),
        "screenshot_url": link("screenshot_s3_key"),
        "confirmation_id": row.get("confirmation_id"),
        "discovered_at": row.get("discovered_at") or (events[0]["at"] if events else None),
        "updated_at": (events[-1]["at"] if events else None),
        "timeline": [{"label": e.get("detail") or e.get("step"), "at": e.get("at"), "done": True}
                     for e in events],
        "fields": row.get("fields") or [],
    }


def _pick_option(value: str, options: list) -> str:
    """The option that MEANS `value` — never a bare substring match.

    "No" is a substring of "North Korea", which is how a sanctions question once
    resolved to the country option instead of the negative one. Exact match wins;
    otherwise the value must appear as a whole word, longest option first so
    "None of the above" is preferred over a shorter accidental hit.
    """
    import re

    v = (value or "").strip().lower()
    if not v:
        return ""
    for o in options:
        if str(o).strip().lower() == v:
            return str(o)
    for o in sorted(options, key=lambda x: len(str(x)), reverse=True):
        if re.search(rf"\b{re.escape(v)}\b", str(o).lower()):
            return str(o)
    return ""


def _split_name(label: str, full: str) -> str:
    """First/Last name fields from a single 'Full name' fact.

    Without this both fields receive the whole name, which is what a mapper does
    when the profile holds one name and the form wants two.
    """
    parts = (full or "").split()
    low = (label or "").lower()
    if len(parts) < 2:
        return full
    if "last" in low or "surname" in low or "family name" in low:
        return parts[-1]
    if "first" in low or "given name" in low:
        return parts[0]
    return full


def _render_preferences_yaml(prefs) -> str:  # noqa: ANN001
    """Write preferences.yaml with its documentation intact.

    Dumping the model straight to YAML would work but would strip every comment,
    and this file is meant to stay hand-editable — the comments explain what each
    list actually does to the two screens that read it. So the prose is a template
    and only the values are generated.
    """
    import yaml

    def block(items: list) -> str:
        # Dump the whole list (never item-by-item: safe_dump of a bare scalar
        # appends a '...' document-end marker) and indent it under its key.
        if not items:
            return "  []\n"
        dumped = yaml.safe_dump(items, default_flow_style=False,
                                allow_unicode=True, sort_keys=False)
        return "".join(f"  {ln}\n" for ln in dumped.splitlines())

    notes = (prefs.notes or "").rstrip()
    notes_block = "".join(f"  {ln}\n" if ln.strip() else "\n"
                          for ln in notes.splitlines()) if notes else ""
    return f"""\
# What the discovery AGENT looks for. These are HINTS, not hardcoded rules — the
# stage-1 relevance agent (src/discovery/relevance.py) reads them as a brief and
# judges each posting's title, counting role variants (SDE, SWE, Backend /
# Platform / ML / AI Engineer, …) as fits. The deeper stage-2 LLM score
# (min_match_score) is the second, per-job gate.
#
# Edited from the dashboard (Preferences) or by hand — both are fine. Every stage
# re-reads this file per job, so a change applies to the very next one scored.

# Target roles, in priority order — the crawl searches these top-down and the
# scorer weighs seniority fit.
titles:
{block(prefs.titles)}
# Soft signals that RAISE fit (not required).
include_keywords:
{block(prefs.include_keywords)}
seniority:
{block(prefs.seniority)}
# Never a fit — technical-sounding NON-engineering roles that leak through.
exclude_keywords:
{block(prefs.exclude_keywords)}
# Preferred locations. A blank/unknown location on a posting is allowed.
locations:
{block(prefs.locations)}
remote_only: {str(prefs.remote_only).lower()}

# Free-text HARD CONSTRAINTS — read by BOTH the title screen and the JD scorer.
# Anything violating these is rejected (the scorer caps it at <= 2).
notes: |
{notes_block}
# Stage-2: minimum LLM relevance score (0-10) for a job to proceed to apply.
min_match_score: {int(prefs.min_match_score)}

# How many NEW postings one company run hands to the pipeline, best-first.
# Tailoring is the expensive stage and a big board can match dozens. 0 = no cap.
max_new_per_run: {int(prefs.max_new_per_run)}

# Candidate's public GitHub — the tailor reads the repos (names, languages,
# topics) as extra context to reword bullets toward each JD.
github: {prefs.github or '""'}
"""


def _manual_role_identity(url: str) -> dict | None:
    """Closed URL/tenant/posting grammar for manual row identity, not a reader."""
    import hashlib
    import re
    import tldextract
    from urllib.parse import parse_qsl, urlsplit

    token = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
    try:
        u = urlsplit(url)
        host = (u.hostname or "").lower().rstrip(".")
        if (u.scheme not in {"http", "https"} or not host or not
                re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host
                or u.username is not None or u.password is not None
                or u.port not in (None, 80 if u.scheme == "http" else 443)
                or "\\" in u.path or "%" in u.path or "//" in u.path):
            return None
        parts = [part for part in u.path.split("/") if part]
        query = parse_qsl(u.query, keep_blank_values=True)
    except ValueError:
        return None

    # Only explicit LinkedIn *job* identifiers are aliases. trackingId, generic
    # numbers and arbitrary jobId query parameters do not prove an alias.
    aliases = [v for k, v in query if k.lower().replace("-", "_")
               in {"linkedin_job_id", "linkedinjobid", "li_job_id"}]
    if any(not re.fullmatch(r"[0-9]{6,20}", value) for value in aliases):
        return None
    linkedin_id = aliases[0] if aliases else ""
    if any(value != linkedin_id for value in aliases):
        return None

    ats, tenant, posting, context = "custom", "", "", ""
    if host in {"linkedin.com", "www.linkedin.com"}:
        if len(parts) != 3 or parts[:2] != ["jobs", "view"] or not re.fullmatch(
                r"[0-9]{6,20}", parts[2]):
            return None
        if linkedin_id and linkedin_id != parts[2]:
            return None
        return {"ats": "linkedin", "company": "", "job_id": "",
                "linkedin_id": parts[2]}

    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io",
                "job-boards.eu.greenhouse.io"}:
        ats = "greenhouse"
        if len(parts) != 3 or parts[1] != "jobs":
            return None
        tenant, posting = parts[0], parts[2]
    elif host in {"jobs.ashbyhq.com", "jobs.lever.co", "jobs.eu.lever.co"}:
        ats = "ashby" if host == "jobs.ashbyhq.com" else "lever"
        if len(parts) != 2:
            return None
        tenant, posting = parts
    elif host == "apply.workable.com":
        ats = "workable"
        if len(parts) != 3 or parts[1] != "j":
            return None
        tenant, posting = parts[0], parts[2]
    elif host == "jobs.smartrecruiters.com":
        ats = "smartrecruiters"
        if len(parts) != 2:
            return None
        tenant, posting = parts[0], parts[1].split("-", 1)[0]
    else:
        workday = re.fullmatch(r"([a-z0-9_-]+)\.(wd[0-9]+)\.myworkdayjobs\.com", host)
        eightfold = re.fullmatch(r"([a-z0-9_-]+)\.eightfold\.ai", host)
        personio = re.fullmatch(r"([a-z0-9_-]+)\.jobs\.personio\.(?:de|com)", host)
        if workday:
            ats, tenant = "workday", workday[1]
            if len(parts) in (4, 5) and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
                parts = parts[1:]
            if len(parts) not in (3, 4) or parts[1] != "job":
                return None
            context = parts[0]
            posting = "/".join(parts[2:])
        elif eightfold:
            ats, tenant = "eightfold", eightfold[1]
            if len(parts) != 3 or parts[:2] != ["careers", "job"]:
                return None
            posting = parts[2]
        elif personio:
            ats, tenant = "personio", personio[1]
            if len(parts) != 2 or parts[0] != "job" or not parts[1].isdigit():
                return None
            posting = parts[1]

    if ats != "custom":
        if (not token.fullmatch(tenant)
                or any(not token.fullmatch(p) for p in posting.split("/"))
                or (context and not token.fullmatch(context))):
            return None
        # The tenant (not the vendor hostname or body-supplied company) owns
        # the readable employer. Freeze this component in the storage pk.
        label = tenant
        # A Workday posting can contain path segments. The separator is
        # outside the accepted token alphabet, so replacing '/' is bijective.
        identity_id = f"{ats}:{tenant.lower()}:{context.lower()}:{posting.replace('/', '.')}"
    else:
        # Keep the exact hostname/path posting identity. Resolve a conservative
        # employer hint with the bundled offline public suffix list (com.vn).
        registered = tldextract.TLDExtract(
            suffix_list_urls=(), extra_suffixes=("test", "example"))(host)
        if not registered.suffix or not registered.domain or not parts:
            return None
        label = registered.domain
        if not token.fullmatch(label):
            return None
        meaningful = sorted((k, v) for k, v in query
                            if not k.lower().startswith("utm_")
                            and k.lower() not in {"trackingid", "fbclid", "gclid"}
                            and k.lower().replace("-", "_") not in {
                                "linkedin_job_id", "linkedinjobid", "li_job_id"})
        signature = repr((host, u.path.rstrip("/"), meaningful))
        identity_id = "custom:" + hashlib.sha256(signature.encode()).hexdigest()[:24]

    generic = {"company", "unknown", "jobs", "job", "careers", "apply",
               "greenhouse", "ashby", "ashbyhq", "lever", "linkedin",
               "workday", "workable", "personio", "smartrecruiters",
               "breezy", "homerun", "phenom", "eightfold", "jobvite",
               "myworkdayjobs", "teamtailor", "recruitee"}
    provider_host = (host.endswith((".breezy.hr", ".homerun.co", ".recruitee.com",
                                  ".myworkdayjobs.com", ".eightfold.ai",
                                  ".jobs.personio.com", ".jobs.personio.de"))
                     or host in {"breezy.hr", "homerun.co", "recruitee.com",
                                 "apply.workable.com",
                                 "jobs.smartrecruiters.com", "jobs.ashbyhq.com",
                                 "boards.greenhouse.io", "jobs.lever.co"})
    generic_label = (label.lower() in generic
                     or re.search(r"(?:^|[-_])(?:careers|jobs|recruiting)$",
                                  label, re.I) is not None)
    if ats != "custom" and generic_label:
        return None
    # Known ATS paths expose an independent employer-bearing tenant; unknown
    # shared-provider hosts do not. Keep posting identity for the page read.
    company = ("" if ats == "custom" and (provider_host or generic_label) else
               " ".join(p for p in re.split(r"[-_]+", label) if p).title())
    return {"ats": ats, "company": company, "job_id": identity_id,
            "linkedin_id": linkedin_id}

def _manual_existing_role(tracking, identity: dict, source_url: str = "") -> tuple[dict | None, bool]:
    """Prefer canonical pks; recover historic rows using stored URL/hash proof.

    A LinkedIn query parameter is not proof that two distinct ATS postings
    are the same. Cross-ATS merges must not be driven by that parameter.
    """
    import hashlib
    from core.ids import make_pk

    pk = (make_pk(identity["company"], identity["job_id"])
          if identity["company"] and identity["job_id"] else "")
    direct = tracking.get(pk) if pk else None
    matches = {pk: direct} if direct else {}

    if identity["ats"] == "linkedin":
        # Preserve the already-supported single linked-row lookup, while
        # requiring its ATS identity and source verification to be intact.
        # This is not independent LinkedIn-to-ATS equivalence evidence.
        linkedin_id = identity["linkedin_id"]
        for row in tracking.all():
            if not isinstance(row, dict) or not row.get("jd_url"):
                continue
            previous = _manual_role_identity(row["jd_url"])
            if (not previous or previous["linkedin_id"] != linkedin_id
                    or not previous["company"] or not previous["job_id"]
                    or row.get("ats") != previous["ats"]
                    or row.get("job_id") != previous["job_id"]
                    or (row.get("posting_guard") or {}).get("source_verified") is not True
                    or row.get("pk") != make_pk(row.get("company") or "",
                                               row.get("job_id") or "")):
                continue
            matches[row["pk"]] = row
    elif source_url:
        # Canonical historical rows can have a different employer hint in
        # their pk. Confirm URL-derived posting identity before any new pk.
        for row in tracking.all():
            if (not isinstance(row, dict) or row.get("ats") != identity["ats"]
                    or row.get("job_id") != identity["job_id"]
                    or not row.get("jd_url") or not row.get("company")
                    or row.get("pk") != make_pk(row["company"], row["job_id"])):
                continue
            previous = _manual_role_identity(row["jd_url"])
            if (previous and previous["ats"] == identity["ats"]
                    and previous["job_id"] == identity["job_id"]):
                matches[row["pk"]] = row
        # Pre-#372 apply-role hashed the entire *stored* source URL. Prove
        # that historic URL/hash pair before comparing parsed ATS posting IDs:
        # decorative query/slash differences must not evade a protected row.
        for row in tracking.all():
            if (not isinstance(row, dict) or row.get("ats") != "custom"
                    or not isinstance(row.get("jd_url"), str) or not row["jd_url"]
                    or not isinstance(row.get("job_id"), str)
                    or not isinstance(row.get("company"), str) or not row["company"]
                    or row["job_id"] != hashlib.sha1(
                        row["jd_url"].encode()).hexdigest()[:12]
                    or row.get("pk") != make_pk(row["company"], row["job_id"])):
                continue
            previous = _manual_role_identity(row["jd_url"])
            # The canonical job_id includes vendor, tenant, context and posting.
            # No rewrite of the old pk, job_id, company or source is warranted.
            if (not previous or previous["ats"] != identity["ats"]
                    or previous["job_id"] != identity["job_id"]):
                continue
            matches[row["pk"]] = row

    if len(matches) > 1:
        return None, True
    return next(iter(matches.values()), None), False


def _manual_update_display(stores, pk: str, updates: dict) -> bool:
    """Patch presentation fields without resetting status or submission state."""
    from core.storage.base import REPAIR_READER_FIELDS

    current = stores.tracking.get_current(pk)
    if not current or current.get("status") not in {
        "found", "skipped", "error", "failed", "tailored",
    }:
        return False
    patch = {k: v for k, v in updates.items()
             if v is not None and current.get(k) != v}
    if not patch:
        return True
    reader = {k: current[k] for k in REPAIR_READER_FIELDS if k in current}
    return bool(stores.tracking.update_if_status(
        pk, current["status"], patch, expected_reader=reader))


def _commit_manual_posting(pk: str, stores, inspected: dict) -> bool:
    """Bind a manual role's sanitized JD and evidence before changing status."""
    from tools import jd, posting_guard as pg

    current = stores.tracking.get_current(pk)
    if not current or current.get("status") not in {
        "found", "skipped", "error", "failed", "tailored", "needs_human",
    }:
        return False
    text, guard = inspected.get("text"), inspected.get("posting_guard")
    if not isinstance(text, str) or not pg.valid(text, guard):
        return False
    return bool(stores.tracking.update_posting_guard(
        pk, current, {"text": text, "posting_guard": guard},
        source_witness=jd.matched_source_witness(current, inspected)))




def _require_submit_review_first_party(request: Request) -> None:
    """Strict *route-local* first-party containment, not local client auth.

    Host/Origin are spoofable by an already-trusted loopback process. A packet
    GET may omit Origin for a direct/same-origin browser request; both POSTs
    require an exact Origin before reading or changing any review state.
    """
    from ipaddress import ip_address

    def deny():
        raise HTTPException(403, "Submit review requires a local first-party request.",
                            headers={"Cache-Control": "no-store"})

    if get_settings().mode != "local":
        deny()
    peer = getattr(request.client, "host", "") if request.client else ""
    try:
        if not ip_address(peer).is_loopback:
            deny()
    except ValueError:
        deny()
    server = request.scope.get("server")
    if (not isinstance(server, (tuple, list)) or len(server) < 2
            or type(server[1]) is not int or not 0 < server[1] <= 65535):
        deny()
    port = server[1]
    allowed = {f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"}
    hosts = request.headers.getlist("host")
    origins = request.headers.getlist("origin")
    fetch_sites = request.headers.getlist("sec-fetch-site")
    if (len(hosts) != 1 or hosts[0] not in allowed
            or len(origins) > 1 or len(fetch_sites) > 1):
        deny()
    # Exact equality also rules out extension schemes, credentials, alternate
    # ports, paths and queries; duplicate/conflicting headers are never trusted.
    if origins:
        if origins[0] != "http://" + hosts[0]:
            deny()
    elif request.method != "GET" or (fetch_sites and
                                     fetch_sites[0] not in {"same-origin", "none"}):
        deny()
    if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        deny()


def _retry_refusal_text(refusal: dict) -> str:
    result, reason = refusal.get("result"), refusal.get("reason")
    if result == "already_done":
        return f"Nothing to retry: the job is {refusal.get('status')}."
    if result == "missing":
        return "No such job."
    if result == "already_running":
        return "This job is already being worked on."
    return f"Retry refused: {reason or result}."


def _review_row_conflict(row: dict | None) -> bool:
    from tools.submit_review import guarded_row
    return guarded_row(row)


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Route registration/import must not touch the private store or Redis.
        # Revoke orphaned generations only on ASGI startup, before requests.
        submit_review.recover_orphans()
        yield

    app = FastAPI(title="AppliedIn", lifespan=lifespan)
    from discovery.career_ops_api import router as career_ops_router
    app.include_router(career_ops_router)
    from discovery.cosign_api import router as cosign_router
    app.include_router(cosign_router)
    settings = get_settings()
    from core.outreach_manual import register_routes as register_outreach_routes
    register_outreach_routes(app, settings)
    from tools import submit_review

    @app.get("/steering")
    def get_steering():
        from core.steering import read
        return read()

    @app.post("/steering")
    def save_steering(body: dict):
        from fastapi import HTTPException

        from core.steering import ConflictError, save

        try:
            return save(body.get("content"), body.get("revision"))
        except ConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.middleware("http")
    async def revalidate_dashboard(request, call_next):
        response = await call_next(request)
        if request.url.path in ("/", "/dashboard.html", "/app.js", "/styles.css", "/config.js"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # The browser extension is a DRIVER, not the app: it runs on the employer's
    # page and calls back here for every decision. That means cross-origin
    # requests from a chrome-extension:// origin, which the browser blocks by
    # default. Scoped to a local server the owner runs themselves.
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^(chrome-extension|moz-extension)://.*$",
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/config.js")
    def config_js():
        # Live config: same-origin API, real data (not demo). Overrides web/config.js.
        return Response('window.APPLIEDIN_CONFIG = {demo:false, env:"local", apiUrl:""};\n',
                        media_type="application/javascript")

    @app.get("/applications/{pk:path}/submit-review")
    def submit_review_packet(pk: str, request: Request):
        _require_submit_review_first_party(request)
        try:
            return JSONResponse(submit_review.packet_for(pk),
                                headers={"Cache-Control": "no-store"})
        except submit_review.Refused:
            raise HTTPException(409, "No current approvable review packet.",
                                headers={"Cache-Control": "no-store"}) from None

    @app.get("/applications")
    def applications():
        stores = make_stores(settings)
        rows = [r for r in stores.tracking.all() if _is_job_row(r)]
        replies, reply_error = [], None
        from core import reply_ingest
        credentials = reply_ingest.local_credentials(settings)
        if settings.mode == "local" and reply_ingest.enabled() and credentials:
            try:
                replies = reply_ingest.read_records(settings)
            except Exception:
                reply_error = "Recruiter replies are temporarily unavailable."
        grouped = {}
        for reply in replies:
            pk = reply.get("pk")
            if pk and pk != "UNASSIGNED":
                grouped.setdefault(pk, []).append({
                    "record_id": reply.get("record_id"),
                    "label": reply_ingest_label(reply.get("label")),
                    "date": reply.get("date") or "",
                    "sender": reply.get("sender") or "",
                    "subject": reply.get("subject") or "",
                })
        for items in grouped.values():
            items.sort(key=lambda item: _reply_sort_timestamp(item["date"]), reverse=True)
        # Hold evaluation uses the RAW row and the existing independent key before
        # the dashboard drops storage-only evidence. No state is written here.
        payload = {"items": [
            _to_ui(r, stores.artifacts) | {
                "replies": grouped.get(r.get("pk"), []),
                "submit_hold_state": _project_submit_hold(
                    r.get("pk") or "", r, stores.tracking,
                    no_independent_key=settings.mode == "cloud"),
                "submit_hold_reason": submit_hold.REASON,
            }
            for r in rows
        ]}
        if reply_error:
            payload.update(replies=[], error=reply_error)
        return payload

    @app.get("/replies/unassigned")
    def unassigned_replies():
        from core import reply_ingest
        credentials = reply_ingest.local_credentials(settings)
        if settings.mode != "local" or not reply_ingest.enabled() or not credentials:
            return {"replies": []}
        try:
            records = reply_ingest.read_records(settings)
        except Exception:
            return {"replies": [], "error": "Recruiter replies are temporarily unavailable."}
        return {"replies": [
            {"record_id": r.get("record_id"),
             "label": reply_ingest_label(r.get("label")),
             "date": r.get("date") or "", "sender": r.get("sender") or "",
             "subject": r.get("subject") or ""}
            for r in records if r.get("pk") == "UNASSIGNED"]}

    @app.post("/replies/{record_id}/assign")
    def assign_reply(record_id: str, body: dict):
        from core import reply_ingest
        pk = str((body or {}).get("pk") or "")
        if settings.mode != "local" or not reply_ingest.enabled() or not reply_ingest.local_credentials(settings):
            return {"ok": False, "error": "Recruiter replies are unavailable."}
        try:
            records = reply_ingest.read_records(settings)
        except Exception:
            return {"ok": False, "error": "Recruiter replies are temporarily unavailable."}
        current = next((record for record in records if record.get("record_id") == record_id), None)
        stores = make_stores(settings)
        row = stores.tracking.get(pk) if pk and not is_internal_pk(pk) else None
        if not row or row.get("status") not in (Status.APPLIED, Status.APPLIED_MANUAL,
                                                  Status.APPLIED.value, Status.APPLIED_MANUAL.value):
            return {"ok": False, "error": "Choose an existing applied application."}
        if current is None:
            return {"ok": False, "error": "Reply not found."}
        if current.get("pk") == pk:
            return {"ok": True}
        try:
            result = reply_ingest.assign(record_id, pk, settings)
        except Exception:
            return {"ok": False, "error": "Recruiter replies are temporarily unavailable."}
        if result == "not_found":
            return {"ok": False, "error": "Reply not found."}
        if result == "conflict":
            return {"ok": False, "error": "Reply was already assigned to another application."}
        return {"ok": True}
    @app.get("/application-tracker")
    def application_tracker():
        return {"items": make_stores(settings).tracking.application_notes()}

    @app.post("/application-tracker/{pk}")
    def save_application_tracker(pk: str, body: dict):
        from core.application_tracker import validate
        stores = make_stores(settings)
        if is_internal_pk(pk) or not stores.tracking.get(pk):
            return {"ok": False, "error": "Application not found."}
        try:
            note = validate(body)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        stores.tracking.save_application_note(pk, note)
        return {"ok": True, "item": note}

    @app.get("/setup-health")
    def setup_health():
        import shutil

        from core.llm_access import missing_access
        checks = []
        resume = Path(settings.config_dir).parent / "resume" / "base.tex"
        has_resume = resume.is_file() and bool(resume.read_text().strip())
        checks.append({"name": "Résumé", "state": "ready" if has_resume else "action",
                       "detail": "Base résumé available." if has_resume else
                       "Add your résumé at resume/base.tex before preparing jobs."})
        access_problems = missing_access(settings)
        checks.append({"name": "Model access",
                       "state": "ready" if not access_problems else "action",
                       "detail": "Model access configured; validity is checked on use."
                       if not access_problems else " ".join(access_problems)})
        render = bool(shutil.which("tectonic"))
        checks.append({"name": "PDF rendering", "state": "ready" if render else "action",
                       "detail": "PDF renderer installed." if render else
                       "Run ./appliedin setup to install the PDF renderer."})
        from tools.browser_runtime import setup_check
        checks.append(setup_check(settings))
        from core import reply_ingest
        credentials = reply_ingest.local_credentials(settings)
        if settings.mode != "local" or not reply_ingest.enabled() or not credentials:
            checks.append({"name": "IMAP replies", "state": "check",
                           "detail": "Optional local IMAP reply ingest is not configured."})
        else:
            try:
                from core.imap_readonly import ReadOnlyIMAP
                with ReadOnlyIMAP(credentials["host"], credentials["username"],
                                   credentials["password"], credentials["mailbox"]):
                    pass
                checks.append({"name": "IMAP replies", "state": "ready",
                               "detail": "Read-only IMAP authentication succeeded."})
            except Exception:
                checks.append({"name": "IMAP replies", "state": "action",
                               "detail": "IMAP authentication failed; verify local setup."})
        return {"checks": checks}

    @app.post("/actions/stop-company")
    def stop_company_scan(body: dict):
        from discovery.handler import stop_company
        name = str(body.get("name") or "").strip()
        return {"ok": stop_company(name), "company": name}

    @app.get("/stats")
    def stats():
        from core import preparation
        from discovery import handler as _handler

        stores = make_stores(settings)
        # Keep the all-time total on the status index, not a full-store walk.
        counts = stores.tracking.status_counts()
        total_submitted = counts.get("applied", 0) + counts.get("applied_manual", 0)
        try:
            today_submitted = _count_submitted_today(stores.tracking)
        except Exception:  # noqa: BLE001 - a stat must never break the dashboard
            # Some status-only backends have no Redis client; an unavailable
            # submitted-row read must not hide the all-time status-index count.
            today_submitted = 0
        from core import flags
        preparation_runs = preparation.snapshot()
        from tools.browser_runtime import browser_status
        queue_age_seconds = None
        try:
            import math
            import time

            from core.apply_queue import ApplyQueue

            pending = ApplyQueue(stores.tracking.r).pending(actionable=True)
            oldest = None
            for item in pending:
                stamp = float(item.get("queued_at") or 0)
                if stamp > 0 and math.isfinite(stamp):
                    oldest = stamp if oldest is None else min(oldest, stamp)
            if pending:
                # Legacy unstamped rows cannot date the queue to the epoch.
                queue_age_seconds = max(0, int(time.time() - oldest)) if oldest is not None else 0
        except Exception:  # noqa: BLE001 - a stat must never break the dashboard
            pass
        return {"today_submitted": today_submitted,
                "total_submitted": total_submitted,
                "llm_error": flags.llm_error(),
                "browser_status": browser_status(settings),
                "queue_age_seconds": queue_age_seconds, "paused": flags.paused(),
                "apply_mode": flags.apply_mode(),
                "submit_review": flags.submit_review_enabled(),
                "headless": flags.browser_headless(),
                "auto_min_score": settings.auto_min_score,
                "counts_by_status": counts,
                "discovering": _discovery_running(),
                "processing": _RUNNING["process"] or any(b["active"] for b in preparation_runs),
                "preparation": preparation_runs,
                # WHICH companies, not just whether any. Scans became per company
                # when the global lock was replaced by claims, but the board still
                # had only a yes or no, so it disabled the Run button whenever
                # anything anywhere was scanning: starting Apple made every other
                # company unstartable, which is exactly what the per company work
                # was meant to allow.
                # The one company being READ right now, plus progress. The claim
                # set is the whole batch, so reporting it made a 47 company sweep
                # look like 47 simultaneous crawls with one identical clock, and a
                # working run indistinguishable from a hung one.
                "scanning": sorted(_handler.scanning()),
                "scan_active": _handler.active(),
                # How many applications are being filled right now. In /stats
                # rather than only in /apply-queue because the deck needs it every
                # poll to know whether to offer Stop applying, and that panel is
                # usually closed.
                "applying": _applying_now(),
                "throttle": _throttle_state(),
                # Empty = healthy. ["daemon"] = NOTHING is running the pipeline
                # (e.g. someone started `python -m server`, which serves this very
                # response but has no workers). Named loops = that thread died.
                "workers_down": flags.workers_down(),
                # `found` = discovered but not yet processed; the number the
                # 'Process applications' button will act on.
                "found_waiting": counts.get("found", 0)}

    # ── Browser-extension ("assisted apply") API ─────────────────────────────
    # The extension runs in the owner's OWN Chrome, so the employer sees a real
    # session with real history rather than an automated browser — which is what
    # trips bot detection. It reads the form, asks here what to put in it, fills,
    # and leaves the person to review and submit. The LLM work stays server-side:
    # the extension never holds a key and never invents an answer.

    @app.get("/extension/context")
    def extension_context(url: str = "", company: str = ""):
        """Who this posting belongs to, plus the résumé and rules for the site."""
        from discovery.handler import company_for_url
        from tools.company_skills import load_skill

        stores = make_stores(settings)
        name = company or company_for_url(url) or ""
        row = next((r for r in stores.tracking.all()
                    if r.get("jd_url") and url and
                    r["jd_url"].split("?")[0] in url), None)
        if row and not name:
            name = row.get("company", "")
        skill = load_skill(url, name)
        resume = None
        if row and row.get("resume_s3_key"):
            from urllib.parse import quote
            resume = f"/artifact/{quote(row['resume_s3_key'], safe='/')}"
        return {
            "company": name,
            "title": (row or {}).get("title", ""),
            "pk": (row or {}).get("pk", ""),
            "status": (row or {}).get("status", ""),
            "resume_url": resume,
            "resume_name": f"{(stores.answer_bank.all_facts(name) or {}).get('Full name', 'Resume')}"
                           " Resume.pdf",
            "site_rules": skill.notes,
            "known_site": bool(skill.names),
        }

    @app.post("/extension/plan")
    def extension_plan(body: dict):
        """LLM-driven fill plan for the fields the extension found on the page.

        Same mapper the pipeline uses: the model chooses WHICH approved fact
        answers each field (or that it needs an essay), and values are substituted
        here in code — so a field can only ever receive an owner-approved answer or
        a drafted essay, never something the model made up.
        """
        # The guard helpers moved to claude_chrome when the five apply files were
        # collapsed into three; this import was not moved with them, so every call
        # here raised ImportError and the endpoint 500d. The extension's assisted
        # apply calls it for every posting, so that path has been dead since.
        from tools.browser_apply import _map_fields
        from tools.claude_chrome import (
            _SAFE_OPTION_RX,
            _SANCTIONS_RX,
            _safe_sanctions_answer,
        )
        from tools.narrative import draft_answer

        url = (body or {}).get("url", "")
        company = (body or {}).get("company", "")
        fields = (body or {}).get("fields") or []
        from tools import posting_guard as pg

        raw_jd_text = (body or {}).get("jd_text", "")
        inspected = pg.analyze(raw_jd_text, source_required=True)
        jd_text = inspected["text"]
        guard = inspected["posting_guard"]
        stores = make_stores(settings)
        # Extension-only calls have no durable row. A supplied pk does not
        # associate itself: the owner must have seen this exact tracked
        # generation, company and posting URL.
        pk = (body or {}).get("pk")
        expected = (body or {}).get("posting_guard_generation")
        if isinstance(pk, str) and pk and isinstance(expected, str) and expected:
            try:
                current = stores.tracking.get_current(pk)
                if (current and current.get("pk") == pk
                        and current.get("company") == company
                        and current.get("jd_url") == url
                        and pg.valid(current.get("jd_text") or "",
                                     current.get("posting_guard"))
                        and current["posting_guard"]["generation"] == expected):
                    # A browser-extension snippet is text-only provenance;
                    # persist its positive findings, not its provisional
                    # missing-HTML marker on an independently verified row.
                    # Reinspection of cleaned text loses the very instructions
                    # removed by the guard. Transfer original-source findings;
                    # the extension has no HTML witness of its own.
                    findings = pg.analyze(raw_jd_text)["posting_guard"]
                    combined = pg.merge(current.get("jd_text") or "", findings,
                                        current["posting_guard"])
                    if stores.tracking.update_posting_guard(pk, current, combined):
                        persisted = stores.tracking.get_current(pk)
                        if persisted and pg.valid(persisted.get("jd_text") or "",
                                                 persisted.get("posting_guard")):
                            guard = persisted["posting_guard"]
                    else:
                        guard = pg.projection(jd_text, None)["posting_guard"]
            except Exception:
                # A missing current row or storage fault is never represented
                # to the popup as a successful durable safety annotation.
                guard = pg.projection(jd_text, None)["posting_guard"]
        facts = stores.answer_bank.all_facts(company) or {}

        try:
            mapped = _map_fields(fields, facts, company, jd_text)
        except Exception as exc:  # noqa: BLE001 — degrade, never 500 the popup
            return {"ok": False, "error": f"mapping failed: {exc}"}

        by_label = {str(f.get("label", "")): f for f in fields}
        values, essays, missing = {}, [], []
        for label, key in (mapped or {}).items():
            field = by_label.get(label) or {}
            if key == "SKIP":
                continue
            if key == "ESSAY":
                try:
                    drafted = draft_answer(label, company, jd_text)
                    answer = (drafted or {}).get("answer") or ""
                except Exception:  # noqa: BLE001
                    answer = ""
                if answer:
                    values[label] = answer
                    essays.append(label)
                else:
                    missing.append(label)
                continue
            answer = facts.get(key, "")
            if not answer:
                missing.append(label)
                continue
            value = _split_name(label, str(answer)) if "name" in label.lower() else str(answer)
            opts = [str(o) for o in (field.get("options") or [])]
            if opts:
                # A choice must end up as one of the ACTUAL options — an answer the
                # form has no option for cannot be selected, and leaving generic
                # wording ("No") invites the driver to substring-match it onto
                # something else entirely ("North Korea").
                value = _pick_option(value, opts) or value
            # LAST word on a sanctions question, after the option is resolved:
            # doing this earlier let a safe "No" be re-expanded into a country option.
            safe = _safe_sanctions_answer(label, [value])
            if safe and safe[0] != value:
                value = safe[0]
            if opts and value not in opts:
                snapped = _pick_option(value, opts)
                if not snapped and _SANCTIONS_RX.search(label or ""):
                    snapped = next((o for o in opts if _SAFE_OPTION_RX.match(o)), "")
                if snapped:
                    value = snapped
                else:
                    # ANY field with options must end up on one of them. A
                    # "Veteran Status" whose choices are full sentences cannot be
                    # answered "No", and returning it anyway promises the driver
                    # something it cannot enter.
                    missing.append(label)
                    continue
            values[label] = value
        return {"ok": True, "values": values, "essays": essays, "missing": missing,
                "posting_guard": {**guard, "messages": pg.reason_messages(guard)},
                "unanswered": [str(f.get("label")) for f in fields
                               if f.get("required") and str(f.get("label")) not in values]}

    @app.get("/extension/queue")
    def extension_queue():
        """Applications waiting for the owner to finish by hand.

        This is the handoff the extension exists for. A job lands here when the
        pipeline cannot finish it ITSELF — a CAPTCHA or a sign-in wall it must not
        touch — and when the owner runs in assisted mode, where the pipeline
        deliberately stops at TAILORED. Either way the work is already done: the
        résumé is tailored and the answers are known, so finishing is a matter of
        opening the page and letting the extension fill it.
        """
        from core import flags

        stores = make_stores(settings)
        blocked = {"captcha", "no_account", "unknown_field"}
        out = []
        for r in stores.tracking.all():
            if not _is_job_row(r) or not r.get("jd_url"):
                continue
            status, reason = r.get("status"), r.get("gate_reason") or ""
            if status == "needs_human" and reason in blocked:
                why = {"captcha": "a security check the pipeline must not solve",
                       "no_account": "the portal wants a sign-in",
                       "unknown_field": "a field it could not answer"}[reason]
            elif status == "failed" and r.get("fail_kind") == "spam_flagged":
                why = "the automated attempt was flagged — your own browser will not be"
            elif status == "tailored" and flags.assisted():
                why = "ready to send"
            else:
                continue
            out.append({"pk": r["pk"], "company": r.get("company", ""),
                        "title": r.get("title", ""), "url": r.get("jd_url"),
                        "location": r.get("location", ""), "why": why,
                        "score": r.get("match_score")})
        out.sort(key=lambda j: -(j.get("score") or 0))
        return {"mode": flags.apply_mode(), "jobs": out}

    @app.post("/extension/applied")
    def extension_applied(body: dict):
        """The owner confirmed they submitted it — record it on the board."""
        pk = (body or {}).get("pk", "")
        if not pk:
            return {"ok": False, "error": "pk required"}
        stores = make_stores(settings)
        if not stores.tracking.get(pk):
            return {"ok": False, "error": "unknown job"}
        if _review_row_conflict(stores.tracking.get(pk)):
            return {"ok": False, "error": "Submit review is held or closed; start a separate fresh application run."}
        stores.tracking.set_status(
            pk, Status.APPLIED_MANUAL,
            confirmation_id=str((body or {}).get("confirmation") or
                                "submitted via the browser extension")[:200],
            gate_pending=None, gate_reason="")
        from core.events import emit
        emit("applied", pk=pk, agent="extension",
             detail="submitted by hand via the extension")
        return {"ok": True}

    @app.get("/job-log/{pk:path}")
    def job_log(pk: str, limit: int = 500):
        """EVERYTHING the agent did on one job — the full event stream (each step,
        tool call, and result), oldest first. The board shows a one-line summary
        per card; this is the whole story behind it, so a stuck or refused apply
        is never a mystery. Includes the job posting URL for that job."""
        import json

        from core.events import HISTORY, _redis

        row = make_stores(settings).tracking.get(pk) or {}
        try:
            raw = _redis().lrange(HISTORY, 0, 1999)
        except Exception:  # noqa: BLE001
            raw = []
        events = []
        for item in raw:
            try:
                e = json.loads(item)
            except Exception:  # noqa: BLE001
                continue
            if e.get("pk") == pk:
                events.append(e)
        events.reverse()  # history is newest-first; read it like a transcript
        return {"pk": pk, "jd_url": row.get("jd_url", ""),
                "company": row.get("company", ""), "title": row.get("title", ""),
                "status": row.get("status", ""), "events": events[-limit:]}

    @app.get("/profiles")
    def get_profiles():
        """The identities applications can go out under."""
        from core import profiles as prof

        items, default = prof.load()
        # What each profile has been used for, counted from the rows. The panel
        # showed an email and nothing else, so "a new one for Netflix" meant
        # guessing which had already been spent there.
        try:
            used = prof.usage(make_stores(settings))
        except Exception:  # noqa: BLE001 — the list must render even if counting fails
            used = {}
        return {"default": default,
                "company_profiles": prof.bindings(),
                "profiles": [{"id": x.id, "label": x.label, "email": x.email,
                              "phone": x.phone, "kind": x.kind, "limit": x.limit,
                              "style": x.style,
                              "usage": used.get(x.id, {"applied": 0, "unsent": 0,
                                                       "in_flight": 0, "companies": {}})}
                             for x in items]}

    def _set_company_profile(company: str, profile_id: str, background) -> dict:  # noqa: ANN001
        """The one place a company's standing profile is set, from either door.

        The company page's "Apply as" and the queue's dropdown used to be two
        mechanisms: the first wrote a preference only discovery read, so choosing
        it on Netflix's page stamped postings found later and left the 72 already
        on the board exactly as they were. Now both come here. Every un-sent row
        is re-pointed and its résumé re-rendered; queued rows come out and go
        back in under the new profile; failed ones are revived; already-sent ones
        keep their identity and are counted. Rows with no résumé yet are tailored
        under the new profile in the background and queued as they finish, so
        this ends with something to press Apply on.
        """
        from core import profiles as prof
        from core.apply_queue import ApplyQueue
        from core.events import emit

        company = (company or "").strip()
        if not company:
            return {"ok": False, "error": "no company"}
        if not profile_id:
            prof.unbind(company)
            emit("running", agent="workflow", company=company,
                 pk=f"meta#run#{company.lower()}",
                 detail=f"{company}: no standing profile — the default applies again")
            return {"ok": True, "company": company, "profile": "", "unbound": True}
        profile = prof.get(profile_id)
        if not profile:
            return {"ok": False, "error": "no such profile"}
        if profile.kind == "rotating":
            return {"ok": False, "error": "that is a rotating template — use Rotate & queue"}
        stores = make_stores(settings)
        q = ApplyQueue(stores.tracking.r)
        out = prof.assign_company(company, profile, stores, q)
        if not out.get("ok"):
            return out
        untailored = out.pop("untailored", [])
        emit("running", agent="workflow", company=company, pk=f"meta#run#{company.lower()}",
             detail=(f"{company}: every application now goes out as {profile.label} · "
                     f"{out['repointed']} re-pointed · {out['queued']} queued"
                     + (f" · {len(untailored)} being tailored" if untailored else "")
                     + (f" · {out['revived']} revived" if out["revived"] else "")
                     + (f" · {out['left_alone']} already sent, left alone" if out["left_alone"] else "")))
        if untailored:
            background.add_task(_tailor_and_queue, company, untailored, stores, q)
        out["tailoring"] = len(untailored)
        return out

    @app.post("/actions/reapply/{pk:path}")
    def reapply_job(pk: str, body: dict, background: BackgroundTasks):
        """Apply again to a posting already applied to, under a different profile.

        A new row for the same posting — the applied one is history and keeps
        its confirmation. The new row is tailored under the new identity in the
        background and queued as it finishes, then waits at the gate like any
        other job. Refused when the identity already applied to this posting.
        """
        from core import profiles as prof
        from core.apply_queue import ApplyQueue
        from core.events import emit

        profile = prof.get(str((body or {}).get("profile_id") or ""))
        if not profile:
            return {"ok": False, "error": "no such profile"}
        stores = make_stores(settings)
        if _review_row_conflict(stores.tracking.get(pk) or {}):
            return {"ok": False, "error": "Submit review is held or closed; start a separate fresh application run."}
        if submit_hold.blocked(pk, stores.tracking.get(pk) or {}, tracking=stores.tracking):
            return {'ok': False, 'error': submit_hold.REASON}
        out = prof.reapply(pk, profile, stores)
        if not out.get("ok"):
            return out
        row = stores.tracking.get(out["pk"]) or {}
        emit("running", pk=out["pk"], agent="workflow", url=row.get("jd_url"),
             detail=f"applying again as {profile.label} — tailoring first")
        q = ApplyQueue(stores.tracking.r)
        background.add_task(_tailor_and_queue, row.get("company") or "", [out["pk"]], stores, q)
        return out

    @app.post("/actions/company-profile")
    def company_profile(body: dict, background: BackgroundTasks):
        """Set the profile every application at ONE company goes out under."""
        return _set_company_profile(str((body or {}).get("company") or ""),
                                    str((body or {}).get("profile_id") or ""), background)

    @app.post("/profiles")
    def set_profiles(body: dict):
        """Replace the profile list. Each needs at least an email; a phone is
        optional. The default is used by any job that hasn't chosen one."""
        from core import profiles as prof

        try:
            items, default = prof.save((body or {}).get("profiles") or [],
                                       (body or {}).get("default", ""))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "default": default,
                "profiles": [{"id": x.id, "label": x.label, "email": x.email,
                              "phone": x.phone, "kind": x.kind, "limit": x.limit,
                              "style": x.style} for x in items]}

    @app.get("/rotation")
    def get_rotation():
        """Which companies rotate their address, and how much of the current one
        is left. The count is derived from the rows themselves, so it is what
        actually happened rather than a number something remembered to update."""
        from core import rotation

        stores = make_stores(settings)
        out = []
        for company in rotation.bindings():
            state = rotation.state(company, stores)
            if state:
                state["aliases"] = rotation.aliases(company)
                out.append(state)
        return {"companies": out, "styles": list(rotation.STYLES)}

    @app.post("/actions/rotation")
    def set_rotation(body: dict):
        """Bind a company to a rotating profile, or unbind it with an empty id.

        Unbinding never deletes an alias: the ledger is the record of which
        address received which employer's mail, and a job already stamped with
        one keeps applying under it.
        """
        from core import rotation

        company = str((body or {}).get("company") or "").strip()
        if not company:
            return {"ok": False, "error": "no company"}
        profile_id = str((body or {}).get("profile_id") or "").strip()
        if not profile_id:
            return {"ok": True, "company": company, "bound": rotation.unbind(company)}
        try:
            row = rotation.bind(company, profile_id,
                                limit=int((body or {}).get("limit") or 0),
                                style=str((body or {}).get("style") or ""))
        except ValueError as exc:   # a combination that could not work, said plainly
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "company": company, **row}

    @app.post("/actions/rotation-backfill")
    def backfill_rotation(body: dict):
        """Re-point this company's un-sent jobs at its rotating address.

        Turning rotation on only changes what happens NEXT, and a board with
        three hundred tailored OpenAI roles on it is mostly already-decided work
        still pointing at the original address. This moves what can be moved —
        as many as the current alias has room for — and re-renders each résumé
        from its saved .tex, so no model is called.
        """
        from core import rotation

        company = str((body or {}).get("company") or "").strip()
        if not company:
            return {"ok": False, "error": "no company"}
        return rotation.backfill(company, make_stores(settings))

    @app.post("/actions/rotate-and-approve")
    def rotate_and_approve(body: dict, background: BackgroundTasks):
        """One company, one press: re-point what is in flight at the rotating
        address, then approve and queue it.

        The three steps have to happen together or they fight each other. Jobs
        already sitting in the apply queue were queued under the OLD address, and
        the queue item is what gets dispatched — so they are dropped first and
        re-queued after the re-point, or the very applications this was meant to
        fix go out under the address being retired.

        It cannot run away: the re-point stops at whatever the current alias has
        room for, so this approves at most that many. A row still gated on a REAL
        question — a portal exercise, a clearance answer — is left alone, because
        "no questions asked" is about not asking again for an approval already
        implied by this press, not about answering something nobody has.
        """
        from core import rotation
        from core.apply_queue import ApplyQueue

        company = str((body or {}).get("company") or "").strip()
        if not company:
            return {"ok": False, "error": "no company"}
        stores = make_stores(settings)
        q = ApplyQueue(stores.tracking.r)

        # "__all__" is every company that rotates — the one press that re-points,
        # tailors, queues and runs the lot. Each company is still handled on its
        # own terms: its own address, its own limit, its own queue lease.
        if company == "__all__":
            names = list(rotation.bindings())
            if not names:
                return {"ok": False, "error": "no company rotates yet"}
            runs = [rotation.rotate_and_approve(n, stores, q) for n in names]
            runs = [r for r in runs if r.get("ok")]
            work = [(r["company"], r.pop("untailored", [])) for r in runs]
            run_now = bool((body or {}).get("run"))

            def _work_all() -> None:
                for name, pks in work:
                    _tailor_and_queue(name, pks, stores, q)
                    if run_now:
                        _flush_loop(name, q)

            if any(pks for _, pks in work) or run_now:
                background.add_task(_work_all)
            total = {k: sum(int(r.get(k) or 0) for r in runs)
                     for k in ("moved", "queued", "left", "blocked", "dequeued",
                               "tailoring")}
            return {"ok": True, "company": "__all__", "companies": len(runs),
                    "running": run_now, **total,
                    "each": [{"company": r["company"], "email": r["email"],
                              "queued": r["queued"], "left": r["left"]} for r in runs]}

        out = rotation.rotate_and_approve(company, stores, q)
        if not out.get("ok"):
            return out

        # A job the rotation reached before tailoring has no résumé to send, so
        # "approve everything" has to include making one. It runs in the
        # background and queues itself the moment it is tailored, under the same
        # address — which is why the profile is stamped first.
        untailored = out.pop("untailored", [])
        run_now = bool((body or {}).get("run"))

        def _work() -> None:
            _tailor_and_queue(company, untailored, stores, q)
            # Only if asked. By default this button ENDS at the queue: queueing is
            # reversible and reviewable, and starting half an hour of browser
            # sessions is a separate decision with a separate button.
            if run_now:
                _flush_loop(company, q)

        if untailored or run_now:
            background.add_task(_work)
        out["running"] = run_now and bool(out.get("queued") or untailored)
        return out

    @app.post("/actions/rotation-retire")
    def retire_rotation(body: dict):
        """Retire the address in use so the next application starts on a fresh
        one. Offered when a board refuses under its own cap while our count was
        still below the limit: the alias is spent whatever we counted."""
        from core import rotation

        company = str((body or {}).get("company") or "").strip()
        return {"ok": bool(company) and rotation.retire(company), "company": company}

    @app.post("/actions/job-profile/{pk:path}")
    def set_job_profile(pk: str, body: dict):
        """Choose which identity THIS application goes out under.

        Re-tailoring is required for the change to reach the PDF, since the
        contact line is written at render time — so a job that has already been
        tailored is put back to `found`, and its next run produces a résumé whose
        details match the form.
        """
        from core import profiles as prof

        stores = make_stores(settings)
        row = stores.tracking.get(pk)
        if not row:
            return {"ok": False, "error": "unknown job"}
        if _review_row_conflict(row):
            return {"ok": False, "error": "Submit review is held or closed; start a separate fresh application run."}
        profile_id = str((body or {}).get("profile_id") or "")
        if profile_id and not prof.get(profile_id):
            return {"ok": False, "error": f"no profile {profile_id!r}"}

        stores.tracking.set_status(pk, row.get("status"), profile_id=profile_id)
        # Only the contact line changes, and the tailoring is already saved — so
        # re-render from the stored .tex rather than sending the job back through
        # the tailor. Switching identity costs nothing.
        rendered = prof.retarget(pk, prof.resolve(profile_id), stores)
        from core.events import emit
        emit("running", pk=pk, agent="workflow",
             detail=(f"profile → {profile_id or 'default'}"
                     + (" · résumé re-rendered to match" if rendered else "")))
        return {"ok": True, "profile_id": profile_id, "rerendered": rendered}

    @app.post("/actions/apply-profile-to-all")
    def apply_profile_to_all(body: dict):
        """Point every tailored job at one profile, re-rendering each résumé.

        Changing who you apply as is a decision about the whole board more often
        than about one job, and doing it job-by-job through the drawer is the kind
        of chore that stops people using the feature at all. No model is called.
        """
        from core import profiles as prof

        profile_id = str((body or {}).get("profile_id") or "")
        profile = prof.resolve(profile_id)
        if not profile:
            return {"ok": False, "error": "no such profile"}
        stores = make_stores(settings)
        done, skipped = 0, 0
        for row in stores.tracking.all():
            pk = row.get("pk", "")
            if is_internal_pk(pk) or not row.get("resume_tex_key"):
                continue
            if row.get("status") in ("applied", "applied_manual", "submitting"):
                skipped += 1          # already sent — its identity is history now
                continue
            stores.tracking.set_status(pk, row.get("status"), profile_id=profile.id)
            if prof.retarget(pk, profile, stores):
                done += 1
        from core.events import emit
        emit("running", agent="workflow", pk="meta#profiles",
             detail=f"{done} résumé(s) re-rendered as {profile.label}")
        return {"ok": True, "profile": profile.id, "rerendered": done,
                "left_alone": skipped}

    @app.get("/preferences")
    def get_preferences():
        """The job-matching preferences the discovery screen and scorer read."""
        import yaml

        path = Path(settings.config_dir) / "preferences.yaml"
        data = yaml.safe_load(path.read_text()) if path.exists() else {}
        return data or {}

    @app.post("/preferences")
    def set_preferences(body: dict):
        """Save job-matching preferences from the dashboard.

        Validated through the same Preferences model discovery uses, then written
        as commented YAML — the file stays hand-editable, and every stage reads it
        from disk on each run, so a save takes effect on the very next job."""
        from discovery.watchlist import Preferences

        try:
            prefs = Preferences(**(body or {}))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"invalid preferences: {exc}"}
        path = Path(settings.config_dir) / "preferences.yaml"
        try:
            path.write_text(_render_preferences_yaml(prefs))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        from core.events import emit
        emit("running", agent="workflow", pk="meta#prefs",
             detail="Job-matching preferences updated — they apply to the next job scored.")
        return {"ok": True, "preferences": prefs.model_dump()}

    @app.get("/memory")
    def memory():
        """The durable markdown diary of pipeline outcomes (applied/needs-you/failed)."""
        from core.memory import read
        return Response(read(), media_type="text/markdown; charset=utf-8")

    @app.get("/companies")
    def companies():
        """The watchlist company names — so the dashboard can offer a picker and
        run discovery for just the selected companies instead of all of them."""
        from core import flags
        from discovery.handler import list_watchlist_companies
        return {"companies": list_watchlist_companies(),
                "skipped": sorted(flags.skipped_companies()),
                "filters": flags.company_filters(),
                # Per-company overrides plus the fields they may override, so the
                # dashboard can show what a company actually screens on without a
                # second round trip per row.
                "prefs": flags.company_prefs(),
                "pref_fields": list(flags.COMPANY_PREF_FIELDS)}

    @app.post("/actions/company-prefs")
    def company_prefs(body: dict, background: BackgroundTasks):
        """Override the job preferences for ONE company.

        Send only the fields you are changing. A field sent as null, "" or []
        is REMOVED, which is how a company goes back to inheriting the global
        preference — that has to be explicit, because an empty list is also a
        legitimate value and the two would otherwise be indistinguishable.
        """
        from core import flags
        name = ((body or {}).get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "name required"}
        over = dict((body or {}).get("overrides") or {})
        for k in ("titles", "include_keywords", "exclude_keywords", "seniority",
                  "locations"):
            if k in over and isinstance(over[k], str):
                over[k] = [s.strip() for s in over[k].replace("\n", ",").split(",")
                           if s.strip()]
        for k in ("min_match_score", "max_new_per_run"):
            if k in over and over[k] not in (None, ""):
                try:
                    over[k] = int(over[k])
                except (TypeError, ValueError):
                    return {"ok": False, "error": f"{k} must be a number"}
        # "Apply as" on the company page is the standing profile, and setting it
        # has to reach the rows already on the board — not only postings found
        # later, which is all it used to do. Same path as the queue's dropdown.
        profile_change = "profile_id" in over
        wanted = str(over.pop("profile_id", "") or "")
        # `{}` means "reset every override to global" and must stay meaning that.
        # A save that carried ONLY a profile change leaves `over` empty here, and
        # passing that through would wipe the company's titles and seniority
        # rules as a side effect of choosing who applies.
        if over or not profile_change:
            flags.set_company_pref(name, over)
        if profile_change:
            outcome = _set_company_profile(name, wanted, background)
            if not outcome.get("ok"):
                return outcome

        # Re-screen what is ALREADY on the board. Without this the new rules only
        # governed future finds, so a company kept a backlog screened under rules
        # the owner had just replaced — and the whole point of narrowing a company
        # is usually to get rid of what is sitting there. The title filter
        # endpoint has always reconciled; this one did not, which made two
        # controls that look alike behave differently.
        moved = _rescreen_company(name)
        return {"ok": True, "prefs": flags.company_prefs(), "rescreened": moved}

    @app.post("/actions/clear-llm-error")
    def clear_llm_error():
        """Dismiss the top-level LLM-failure banner."""
        from core import flags
        flags.set_flag("llm_error", "")
        return {"ok": True}

    @app.post("/actions/skip-company")
    def skip_company(body: dict):
        """Toggle a company's skip state. Skipped companies sit out un-scoped
        Discover/Process runs; explicitly picking one in the UI overrides."""
        from core import flags
        name = ((body or {}).get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "name required"}
        skipped = flags.set_company_skip(name, bool((body or {}).get("skip")))
        return {"ok": True, "skipped": sorted(skipped)}

    @app.post("/actions/run-company")
    def run_company(body: dict, background: BackgroundTasks):
        """One-company end-to-end workflow: (add to watchlist if new) → discover
        just that company → score + tailor its findings. In gated apply mode the
        run STOPS at tailored/approval — nothing is submitted."""
        name = ((body or {}).get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "name required"}
        # Refuse only if THIS company is already being scanned. It used to refuse
        # whenever any discovery OR any backlog processing was running, which meant
        # a scan of one employer blocked every other one, and scoring the backlog —
        # which touches no company's careers page at all — blocked scanning
        # entirely. Neither shares anything with a scan of a different company.
        if _scanning_company(name):
            return {"ok": False, "status": "already_running",
                    "blocked_by": "discover", "company": name}
        careers_url = ((body or {}).get("careers_url") or "").strip()
        profile_id = str((body or {}).get("profile_id") or "")
        from discovery.handler import add_watchlist_company, list_watchlist_companies
        if name.lower() not in (c.lower() for c in list_watchlist_companies()):
            added = add_watchlist_company(name, careers_url)
            if not added.get("ok"):
                return added

        def _run() -> None:
            import logging

            from core.events import emit
            from daemon import process_backlog_once
            from discovery.handler import run_discovery
            _RUNNING["discover"] = True
            found = {}
            try:
                emit("running", agent="workflow", company=name,
                     pk=f"meta#run#{name.lower()}",
                     detail=f"▶ {name}: one-company run — discovering…")
                found = run_discovery(only=[name], profile_id=profile_id, manual=True)
                n_new = (found.get("enqueued") or 0) + (found.get("crawled") or 0)
                emit("running", agent="workflow", company=name,
                     pk=f"meta#run#{name.lower()}",
                     detail=f"{name}: discovery finished — {n_new} new posting(s); "
                            f"scoring + tailoring the backlog…")
            except Exception:
                logging.getLogger("server").exception("run-company discover failed")
            finally:
                _RUNNING["discover"] = False
            if not found or any(r.get("note") == "Stopped by you"
                                for r in found.get("companies", [])):
                return
            _RUNNING["process"] = True
            try:
                process_backlog_once(companies=[name], manual=True, prepare_only=True)
                # Report the outcome, not the fact that the code reached the end.
                # "Tailored jobs await your approval" when nothing was found sends
                # the owner to look at an empty board and doubt the board.
                stores = make_stores(settings)
                waiting = sum(1 for r in stores.tracking.all()
                              if (r.get("company") or "").strip().lower() == name.strip().lower()
                              and r.get("status") == "tailored")
                receipts = found.get("companies") or []
                receipt = receipts[0] if receipts else {}
                if waiting:
                    msg = f"{name}: {waiting} résumé(s) ready for review"
                elif receipt.get("note"):
                    msg = f"{name}: {receipt['note']}"
                elif receipt.get("found") is not None:
                    msg = (f"{name}: scan complete — {receipt['found']} postings returned, "
                           f"{receipt.get('relevant') or 0} matched, {n_new} new. "
                           "Review Passed over for exclusions; existing jobs are not added twice.")
                else:
                    msg = f"{name}: scan complete. No new jobs added; see scan results for details."
                emit("applied", agent="workflow", company=name,
                     pk=f"meta#run#{name.lower()}", detail=msg)
            except Exception:
                logging.getLogger("server").exception("run-company process failed")
            finally:
                _RUNNING["process"] = False

        background.add_task(_run)
        return {"ok": True, "status": "running", "company": name}

    @app.post("/actions/company-filter")
    def company_filter(body: dict):
        """Set a company's title filter (e.g. Rivian -> only 'Staff' titles).
        `titles` may be a list or a comma/newline-separated string; empty clears
        it. Retroactively skips already-found jobs that no longer match, and
        un-skips ones that do — so the board reflects the filter immediately."""
        from core import flags
        name = ((body or {}).get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "name required"}
        raw = (body or {}).get("titles", [])
        kws = ([k for k in raw] if isinstance(raw, list)
               else [k for k in raw.replace("\n", ",").split(",")])
        kws = [k.strip() for k in kws if k and k.strip()]
        flags.set_company_filter(name, kws)

        # Reconcile the current backlog for this company.
        stores = make_stores(settings)
        low = name.lower()
        moved = 0
        for r in stores.tracking.all():
            if (r.get("company") or "").lower() != low:
                continue
            st, title = r.get("status"), r.get("title") or ""
            ok = flags.title_matches_filter(title, kws)
            if st == "found" and not ok:
                stores.tracking.set_status(r["pk"], Status.SKIPPED,
                                           skip_reason="title_filter")
                moved += 1
            elif st == "skipped" and r.get("skip_reason") == "title_filter" and ok:
                stores.tracking.set_status(r["pk"], Status.FOUND, skip_reason="")
                moved += 1
        return {"ok": True, "filters": flags.company_filters(), "reconciled": moved}

    @app.post("/actions/run-job/{pk}")
    def run_one_job(pk: str, background: BackgroundTasks):
        """Run ONE found/tailored job through score + tailor right now (the card's
        'Run now'). Gated apply mode stops it at ready-to-apply."""
        row = make_stores(settings).tracking.get(pk)
        if not row:
            return {"ok": False, "error": "unknown job"}

        def _run() -> None:
            import logging

            from agent.run import _reader_patch, run_job
            stores = make_stores(settings)
            admitted = stores.tracking.get(pk) or {}
            try:
                run_job(pk, stores, prepare_only=True)
            except Exception as exc:
                # The claim is released on failure; the card must not stay WORKING.
                _reader_patch(pk, admitted, stores, {
                    "status": Status.ERROR,
                    "error": f"Preparation failed: {type(exc).__name__}"})
                logging.getLogger("server").exception("run-job failed for %s", pk)

        background.add_task(_run)
        return {"ok": True, "status": "running", "pk": pk}

    @app.post("/actions/force-apply/{pk:path}")
    def force_apply(pk: str, background: BackgroundTasks):
        """Override only a low match score; tailoring still ends at final review."""
        from core.apply_queue import ApplyQueue

        stores = make_stores(settings)
        row = stores.tracking.get(pk) or {}
        q = ApplyQueue(stores.tracking.r)
        if (_review_row_conflict(row) or row.get("status") != "skipped" or row.get("skip_reason") != "low_score"
                or submit_hold.blocked(pk, row, tracking=stores.tracking)
                or row.get("confirmation_id") or pk in q.in_flight()
                or any(item["pk"] == pk for item in q.pending())):
            return {"ok": False, "error": "Force apply is only available for an unsent role skipped for a low match score."}
        from datetime import datetime, timezone

        stores.tracking.set_status(pk, Status.FOUND, score_override=True,
                                   score_override_at=datetime.now(timezone.utc).isoformat(),
                                   skip_reason="", fail_reason="", fail_kind="", error="")

        def prepare() -> None:
            from agent.run import _reader_patch, run_job

            admitted = stores.tracking.get(pk) or {}
            try:
                # Explicitly preparation-only even when automatic applying is on.
                run_job(pk, stores, prepare_only=True)
            except Exception:
                import logging

                logging.getLogger("server").exception("forced preparation failed for %s", pk)
                _reader_patch(pk, admitted, stores, {
                    "status": Status.ERROR, "error": "Preparation failed — see Logs and retry."})

        background.add_task(prepare)
        return {"ok": True, "pk": pk, "status": "preparing", "next": "review"}

    @app.post("/actions/reopen/{pk:path}")
    def reopen(pk: str, background: BackgroundTasks):
        """Only safely closed, unsent, inactive rows can be reopened.

        A suspended reader or an in-flight submit must not be resurrected by a
        stale status-only reset. Reopen preserves the reader budget and intent.
        """
        from agent.run import (_active_apply_lease, _claimed, _reader_check,
                               _reader_patch)

        stores = make_stores(settings)
        try:
            row = stores.tracking.get(pk)
        except Exception:
            return {"ok": False, "error": "Tracking storage unavailable; restore it before Reopen."}
        if not row:
            return {"ok": False, "error": "unknown job"}
        status = str(row.get("status") or "")
        allowed = {"skipped", "failed", "error", "job_gone", "capped"}
        if status not in allowed:
            return {"ok": False, "error": "refused",
                    "note": "Only inactive, closed, unsent jobs can be reopened; "
                            "do not re-open a live or submitted application."}
        # Skip changes status but intentionally preserves exhausted reader
        # evidence. Reopen must not produce an ineligible FOUND row from it.
        if (int(row.get("jd_read_attempts") or 0) >= 3
                or row.get("fail_kind") in (
                    "jd_reader_exhausted", "jd_tracking_storage_error",
                    "jd_retry_session_error")):
            return {"ok": False, "error": "jd_reader_attention",
                    "note": "Reader attention requires Retry, not Reopen."}
        try:
            check = _reader_check(pk, row, stores)
            if check:
                return {"ok": False, "error": check["reason"],
                        "note": "Reconcile the submission or refresh this row before Reopen."}
            if _claimed(pk, stores):
                return {"ok": False, "error": "evaluation_active",
                        "note": "Wait for the active evaluation claim to finish."}
            active = _active_apply_lease(pk, stores)
            if active is None:
                return {"ok": False, "error": "apply_lease_unknown",
                        "note": "Restore application-queue lease inspection before Reopen."}
            if active:
                return {"ok": False, "error": "apply_in_flight",
                        "note": "Reconcile the active application before Reopen."}
            check = _reader_check(pk, row, stores)
            if check:
                return {"ok": False, "error": check["reason"],
                        "note": "Protection or tracking state changed; refresh before Reopen."}
            committed, _ = _reader_patch(pk, row, stores, {
                "status": Status.FOUND, "skip_reason": "", "fail_reason": "",
                "fail_kind": "", "error": "", "match_score": None,
                "gate_reason": "", "gate_pending": None, "gate_call_id": None,
                "rescore_requested": True,
            })
        except Exception:
            return {"ok": False, "error": "Tracking/claim/lease inspection failed; "
                    "restore storage and check the job before Reopen."}
        if not committed:
            return {"ok": False, "error": "conflict",
                    "note": "The job changed during Reopen; refresh before retrying."}
        from core.events import emit

        emit("running", pk=pk, agent="workflow", url=row.get("jd_url"),
             detail=f"reopened · {row.get('title','')} @ {row.get('company','')} "
                    f"(was {status}) — scoring again")

        # Reopen shares discovery's bounded evaluate lanes. The admitted worker
        # resets its session under its existing claim before scoring again.
        stores.queue.enqueue(stores.tailor_queue, {"pk": pk})
        return {"ok": True, "pk": pk, "was": status, "status": "queued"}

    @app.post("/actions/tailor-text")
    def tailor_text(body: dict, background: BackgroundTasks):
        """Tailor a pasted JD only; its text identity never becomes an ATS job."""
        import hashlib
        from core.models import JobRecord, Status
        from core.storage.base import REPAIR_READER_FIELDS
        from tools import posting_guard as pg

        text = ((body or {}).get("text") or "").strip()
        if len(text) < 80:
            return {"ok": False, "error": "paste the role description — a line or two is not enough to tailor against"}
        company = ((body or {}).get("company") or "").strip().rstrip("#").strip() or "Reach out"
        title = ((body or {}).get("title") or "").strip() or "Role from a message"
        digest = hashlib.sha1(text.encode()).hexdigest()
        job_id = "text-" + digest
        stores = make_stores(settings)

        def _run() -> None:
            import logging
            from agent.run import run_job
            from core.events import emit

            _RUNNING["process"] = True
            try:
                emit("running", agent="workflow", company=company, pk=pk,
                     detail=f"▶ tailoring a résumé for {title} @ {company}…")
                run_job(pk, stores)
                emit("applied", agent="workflow", company=company, pk=pk,
                     detail=f"{company}: résumé tailored — download it from the card")
            except Exception:
                logging.getLogger("server").exception("tailor-text failed")
            finally:
                _RUNNING["process"] = False

        # Do not use the generic JD hash index: ATS postings may share this
        # text and must never become text-only rows.
        existing = [row for row in stores.tracking.all()
                    if row.get("ats") == "custom" and not row.get("jd_url")
                    and row.get("job_id") in {job_id, "text-" + digest[:12]}]
        if len(existing) > 1:
            return {"ok": False, "error": "ambiguous_text_identity"}
        row = existing[0] if existing else None
        if row:
            pk = row["pk"]
            if _review_row_conflict(row):
                return {"ok": False, "error": "Submit review is held or closed; start a separate fresh application run."}
            if submit_hold.blocked(pk, row, tracking=stores.tracking):
                return {"ok": False, "error": "possible submission; check the employer portal before retrying"}
            if row.get("status") not in {"found", "skipped", "error", "failed", "tailored"}:
                return {"ok": False, "error": "protected_tracking_state"}
            if not _commit_manual_posting(pk, stores, pg.analyze(text)):
                return {"ok": False, "error": "posting_guard_conflict"}
            if not _manual_update_display(stores, pk, {
                    "title": title, "display_company": company}):
                return {"ok": False, "error": "tracking_conflict"}
            # Only a status-conditional patch can restage a prior tailored or
            # skipped text card; set_status could overwrite in-flight work.
            current = stores.tracking.get_current(pk)
            if (not current or _review_row_conflict(current)
                    or submit_hold.blocked(pk, current, tracking=stores.tracking)
                    or current.get("status") not in {
                        "found", "skipped", "error", "failed", "tailored"
                    }):
                return {"ok": False, "error": "tracking_conflict"}
            # Repeating a safe text-only role requests actual re-tailoring:
            # restore FOUND only alongside a new background dispatch.
            reader = {key: current[key] for key in REPAIR_READER_FIELDS
                      if key in current}
            if not stores.tracking.update_if_status(
                    pk, current["status"],
                    {"status": Status.FOUND, "skip_reason": "",
                     "fail_kind": "", "fail_reason": ""},
                    expected_reader=reader):
                return {"ok": False, "error": "tracking_conflict"}
            background.add_task(_run)
            return {"ok": True, "status": "tailoring", "pk": pk,
                    "company": company, "title": title}

        inspected = pg.analyze(text)
        job = JobRecord(company=company, job_id=job_id, title=title,
                        jd_url="", jd_text=inspected["text"],
                        posting_guard=inspected["posting_guard"], ats="custom")
        pk = job.pk
        if not stores.tracking.put_new(job):
            # A competing request created this identity; never reset it by
            # unconditional set_status or schedule another background action.
            return {"ok": False, "error": "tracking_conflict"}
        if not _commit_manual_posting(pk, stores, inspected):
            return {"ok": False, "error": "posting_guard_conflict"}

        background.add_task(_run)
        return {"ok": True, "status": "tailoring", "pk": pk,
                "company": company, "title": title}

    @app.post("/actions/apply-role")
    def apply_role(body: dict, background: BackgroundTasks):
        """Resolve a posting's employer and durable identity before any row."""
        from core.models import JobRecord
        from tools import jd, posting_guard as pg

        funded_tag = (body or {}).get("discovery_source") == "funded_no_opening"
        if (body or {}).get("discovery_source") not in (None, "", "funded_no_opening"):
            return {"ok": False, "error": "unsupported_discovery_source"}
        url = ((body or {}).get("url") or "").strip()
        if url and not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        if not url:
            return {"ok": False, "error": "a job URL is required"}
        identity = _manual_role_identity(url)
        if not identity:
            return {"ok": False, "error": "posting_identity_unresolved"}
        # The vendor tenant/domain label is a URL identity, not necessarily
        # the verified employer's displayed name. Use #386's exact identity
        # for tracking, but keep the parsed tenant as independent evidence for
        # the later source-verifying gate.
        parsed_identity = identity
        funded_lead = None
        if funded_tag:
            from core.outreach_sources import SourceHold, read_no_opening
            try:
                funded_lead = read_no_opening(
                    settings, (body or {}).get("funded_company_id"),
                    require_fresh=False)
            except SourceHold as exc:
                return {"ok": False, "error": str(exc)}
            identity = {**identity, "company": funded_lead.name}
        stores = make_stores(settings)
        row, ambiguous = _manual_existing_role(stores.tracking, identity, url)
        if ambiguous:
            return {"ok": False, "error": "ambiguous_posting_identity"}
        if not row and identity["ats"] == "linkedin":
            # A standalone LinkedIn ID proves a role, not its employer.
            return {"ok": False, "error": "employer_identity_unverified"}
        if row:
            pk = row["pk"]
            if _review_row_conflict(row):
                return {"ok": False, "error": "Submit review is held or closed; start a separate fresh application run."}
            if submit_hold.blocked(pk, row, tracking=stores.tracking):
                return {"ok": False, "error": "possible submission; check the employer portal before retrying"}
            if row.get("status") not in {"found", "skipped", "error", "failed", "tailored"}:
                return {"ok": False, "error": "protected_tracking_state"}
            # Keep the original source for the exact matched_source_witness:
            # an otherwise equivalent URL with a new query is not a witness.
            read_url = row.get("jd_url") or ""
            company = row["company"]
        else:
            read_url = url
            company = identity["company"]
        if not read_url:
            return {"ok": False, "error": "posting_source_unresolved"}

        # Exactly one existing reader call, before insert or success. A failed
        # read must not leave a placeholder pk or a source-unverified JD.
        try:
            meta = jd.fetch_jd_meta(read_url)
        except Exception:
            return {"ok": False, "error": "posting_reader_unavailable"}
        if (not isinstance(meta, dict)
                or not pg.valid(meta.get("text"), meta.get("posting_guard"))
                or not jd.matched_source_witness({"jd_url": read_url}, meta)):
            return {"ok": False, "error": "posting_source_unverified"}
        title = meta.get("title") or (row or {}).get("title") or ""
        if not isinstance(title, str) or not title.strip():
            return {"ok": False, "error": "posting_title_invalid"}
        page_company = jd._employer_name(meta.get("company"))
        if (row and not funded_tag and page_company
                and page_company.casefold() != row["company"].casefold()):
            # New text cannot silently re-key or overwrite a protected identity.
            return {"ok": False, "error": "employer_identity_conflict"}
        if not row and not funded_tag:
            company = page_company or identity["company"]
            if not company:
                return {"ok": False, "error": "employer_identity_unverified"}
        if funded_tag:
            from core.outreach_sources import SourceHold
            from discovery.career_ops import funded_apply_role_handoff
            try:
                funded_apply_role_handoff(
                    settings, (body or {}).get("funded_company_id"),
                    parsed_identity, read_url, meta)
            except SourceHold as exc:
                return {"ok": False, "error": str(exc)}
            if row and company.casefold() not in {
                    funded_lead.name.casefold(),
                    *(alias.casefold() for alias in funded_lead.aliases)}:
                return {"ok": False, "error": "funded_existing_employer_mismatch"}

        if row:
            if not _commit_manual_posting(pk, stores, meta):
                return {"ok": False, "error": "posting_guard_conflict"}
            patch = {"title": title}
            # #369 does not promise posted_at; never invent a timestamp.
            if isinstance(meta.get("posted_at"), str) and meta["posted_at"]:
                patch["posted_at"] = meta["posted_at"]
            if not _manual_update_display(stores, pk, patch):
                return {"ok": False, "error": "tracking_conflict"}
            return {"ok": True, "status": "existing", "pk": pk,
                    "company": company, "title": title}

        # A new row is not source-verified until the guarded compare commits.
        # On a failed compare it must retain the untrusted placeholder JD.
        pending = pg.analyze("", source_required=True)
        job = JobRecord(company=company, job_id=identity["job_id"], title=title,
                        jd_url=read_url, jd_text=pending["text"],
                        posting_guard=pending["posting_guard"], ats=identity["ats"],
                        posted_at=meta.get("posted_at") or "",
                        discovery_source="funded_no_opening" if funded_tag else "")
        pk = job.pk
        if not stores.tracking.put_new(job):
            return {"ok": False, "error": "tracking_conflict"}
        if not _commit_manual_posting(pk, stores, meta):
            # Historical apply-role callers expect a persisted new card and a
            # background task. Never score it after a failed guarded write.
            def _guard_conflict() -> None:
                import logging
                logging.getLogger("server").warning(
                    "apply-role: posting guard conflict for %s; scorer not started", pk)

            background.add_task(_guard_conflict)
            return {"ok": True, "status": "found", "pk": pk,
                    "company": company, "title": title,
                    "warning": "posting_guard_conflict"}

        def _run() -> None:
            import logging
            from agent.run import run_job
            from core.events import emit

            _RUNNING["process"] = True
            try:
                emit("running", agent="workflow", company=company, pk=pk,
                     detail=f"▶ single-role: scoring + tailoring {title} @ {company}…")
                run_job(pk, stores)
                emit("applied", agent="workflow", company=company, pk=pk,
                     detail=f"{company}: résumé tailored — approve on the board to apply")
            except Exception:
                logging.getLogger("server").exception("apply-role failed")
            finally:
                _RUNNING["process"] = False

        background.add_task(_run)
        return {"ok": True, "status": "running", "pk": pk,
                "company": company, "title": title}

    @app.post("/actions/watchlist")
    def watchlist_add(body: dict):
        """Add a company to the watchlist (name + optional careers URL). The
        finder auto-resolves its ATS/board on the first discovery run."""
        from discovery.handler import add_watchlist_company
        return add_watchlist_company((body or {}).get("name", ""),
                                     (body or {}).get("careers_url", ""))

    @app.post("/actions/discover")
    def discover(background: BackgroundTasks, body: dict | None = None):
        """Run ONE discovery cycle now — find + enqueue new jobs as `found`.
        Body may carry `{"companies": ["Ramp", ...]}` to scope the run to the
        picked companies; omit/empty = the whole watchlist. Discover-only: it does
        not score, tailor, or apply (that's /actions/process)."""
        only = [c for c in ((body or {}).get("companies") or []) if isinstance(c, str)]
        # Refuse only when there is genuinely nothing to do: every company asked
        # for is already being scanned. A scan of some OTHER company is not a
        # reason to refuse this one, and an un-scoped run is never refused here —
        # run_discovery claims whatever is free and gets on with it.
        from discovery import handler as _h

        if only and {c.strip().lower() for c in only} <= _h.scanning():
            return {"ok": False, "status": "already_running", "blocked_by": "discover",
                    "companies": only}
        # Everything this run finds is stamped with the chosen identity, so a whole
        # batch applies from one address without touching each job afterwards.
        profile_id = str((body or {}).get("profile_id") or "")
        # Run-scoped freshness window: bound THIS scan to roles the employer
        # published within `hours`; 0/absent = no age limit. It never edits any
        # company's saved preferences — the next un-windowed run is back to normal.
        try:
            hours = float((body or {}).get("hours") or 0)
        except (TypeError, ValueError):
            hours = 0.0

        def _run() -> None:
            from daemon import run_discovery_once
            _RUNNING["discover"] = True
            try:
                run_discovery_once(only=only or None, profile_id=profile_id,
                                   max_age_hours=hours)
            except Exception:  # noqa: BLE001
                import logging
                logging.getLogger("server").exception("manual discovery failed")
            finally:
                _RUNNING["discover"] = False

        background.add_task(_run)
        return {"ok": True, "status": "discovering",
                "companies": only or "all"}

    @app.post("/actions/process")
    def process(background: BackgroundTasks, body: dict | None = None):
        """Process the discovered backlog now — score + tailor every `found` job,
        then apply the ones that qualify (up to the daily cap). One full pass.
        Body may carry {companies:[names]} to run the pass on JUST those
        companies' jobs — the rest of the backlog stays untouched for later."""
        if _RUNNING["process"]:
            return {"ok": False, "status": "already_running", "blocked_by": "process"}
        companies = [c for c in ((body or {}).get("companies") or [])
                     if isinstance(c, str) and c.strip()]

        def _run() -> None:
            from daemon import process_backlog_once
            _RUNNING["process"] = True
            try:
                process_backlog_once(
                    companies=companies, manual=True, prepare_only=True)
            except Exception:  # noqa: BLE001
                import logging
                logging.getLogger("server").exception("manual process failed")
            finally:
                _RUNNING["process"] = False

        _RUNNING["process"] = True
        background.add_task(_run)
        return {"ok": True, "status": "processing", "companies": companies}

    @app.post("/actions/resume/{pk}")
    def resume(pk: str, body: dict, background: BackgroundTasks):
        # Continue the gated run in the background so the click returns instantly;
        # the pipeline's next steps stream to the Logs view as they happen.
        from agent.run import resume_job
        stores = make_stores(settings)
        if _review_row_conflict(stores.tracking.get(pk) or {}):
            return {"ok": False, "error": "Submit review is held or closed; start a separate fresh application run."}
        if submit_hold.blocked(pk, stores.tracking.get(pk) or {}, tracking=stores.tracking):
            return {"ok": False, "error": "possible submission; check the employer portal before retrying"}
        disposition = body.get("disposition")
        generation = body.get("expected_gate_generation")
        identity = body.get("expected_question_identity")
        answer = body.get("answer", "")
        # Syntactic refusal before scheduling: the exact-question match itself
        # runs in resume_job against the current row, before any effect.
        if (not isinstance(answer, str)
                or disposition not in (None, "answer", "skip")
                or (disposition is not None and (
                    not isinstance(generation, str) or not 0 < len(generation) <= 128
                    or not isinstance(identity, str) or not 0 < len(identity) <= 128))
                or (disposition == "answer" and not answer.strip())):
            return {"ok": False, "error": "invalid_answer"}
        def _run_and_report():
            # A transport acknowledgement is not an answer decision. Report only
            # the original pre-effect refusal; never invoke resume_job again.
            result = resume_job(pk, answer, disposition=disposition,
                                expected_gate_generation=generation,
                                expected_question_identity=identity)
            if (disposition in ("answer", "skip") and isinstance(result, dict)
                    and result.get("result") == "gated"
                    and result.get("reason") == "invalid_answer"):
                same_gate = False
                try:
                    # Report-time relevance is observational, never an authority
                    # to accept a reply or change the stored gate.
                    current = stores.tracking.get(pk) or {}
                    tokens = _owner_gate_tokens(current)
                    same_gate = (
                        current.get("status") == Status.NEEDS_HUMAN.value
                        and current.get("gate_reason") == "owner_question"
                        and tokens["gate_generation"] == generation
                        and tokens["gate_question_identity"] == identity
                        and _project_submit_hold(pk, current, stores.tracking) == "clear")
                except Exception:
                    pass  # An unreadable gate is historical-only in the UI.
                try:
                    from core.events import emit
                    emit("response", pk=pk, detail="Owner reply not accepted.",
                         resume_answer_refused=True, refusal_reason="invalid_answer",
                         same_pending_gate_at_emit=bool(same_gate))
                except Exception:
                    pass  # Telemetry must not change the background result.
            return result

        background.add_task(_run_and_report)
        # Accepted for processing only; the dashboard reads the eventual row.
        return {"ok": True, "status": "resuming"}

    def stopped_application_attempt(pk: str, tracking) -> str:
        """Refuse the target attempt, not an unrelated company's browser session.

        The caller already owns this job's company lease. An orphaned
        SUBMITTING row is still ambiguous even without a queue pk witness.
        """
        from core.apply_queue import ApplyQueue
        try:
            row = tracking.get(pk) or {}
            if row.get("status") == Status.SUBMITTING.value:
                return "Cannot prove that the attempt has stopped."
            if pk in ApplyQueue(tracking.r).in_flight():
                return "An application attempt is still in flight."
        except Exception:
            return "Cannot prove that the attempt has stopped."
        return ""

    def claim_human_outcome(pk: str, tracking):
        """Acquire the worker's company lease before checking or changing outcome."""
        from core.apply_queue import ApplyQueue
        try:
            row = tracking.get(pk) or {}
            if not row:
                return None, "", "unknown job"
            company = row.get('company')
            if not isinstance(company, str) or not company.strip():
                return None, "", "Cannot prove the company lease for this job."
            queue = ApplyQueue(tracking.r)
            if not queue.claim_human_outcome(company):
                return None, "", "An application attempt is still in flight."
            return queue, company, ""
        except Exception:
            return None, "", "Cannot prove that the attempt has stopped."

    @app.post("/actions/mark-applied/{pk}")
    def mark_applied(pk: str, body: dict | None = None):
        """Human-confirmed submission, serialized with the application worker."""
        note = ((body or {}).get("note") or "").strip() or "Confirmed by you (email / on-screen ACK)."
        stores = make_stores(settings)
        lease, company, error = claim_human_outcome(pk, stores.tracking)
        if error:
            return {"ok": False, "error": error}
        try:
            row = stores.tracking.get(pk) or {}
            if _review_row_conflict(row):
                return {"ok": False, "error": "Submit review is held or closed; start a separate fresh application run."}
            if row.get("company") != company:
                return {"ok": False, "error": "The job company changed; outcome was not recorded."}
            if failure := stopped_application_attempt(pk, stores.tracking):
                return {"ok": False, "error": failure}
            stores.tracking.set_status(
                pk, Status.APPLIED_MANUAL, confirmation_id=note, gate_reason="", gate_pending=None)
            submit_hold.clear(pk, tracking=stores.tracking)
            from core.events import emit
            emit("applied", pk=pk, agent="applier", detail="Marked applied by you — no resubmit.")
            return {"ok": True}
        finally:
            lease.release_human_outcome(company)

    @app.post("/actions/resolve-uncertain/{pk:path}")
    def resolve_uncertain(pk: str, body: dict):
        """Portal-verified human outcome, serialized with the application worker."""
        if body.get("portal_checked") is not True:
            return {"ok": False, "error": "Check the employer portal before resolving."}
        outcome = body.get("outcome")
        if outcome not in {"submitted", "not_submitted"}:
            return {"ok": False, "error": "Explicit submitted/not_submitted outcome required."}
        stores = make_stores(settings)
        lease, company, error = claim_human_outcome(pk, stores.tracking)
        if error:
            return {"ok": False, "error": error}
        try:
            row = stores.tracking.get(pk) or {}
            if _review_row_conflict(row):
                return {"ok": False, "error": "Submit review is held or closed; start a separate fresh application run."}
            if row.get("company") != company:
                return {"ok": False, "error": "The job company changed; outcome was not recorded."}
            if failure := stopped_application_attempt(pk, stores.tracking):
                return {"ok": False, "error": failure}
            if not submit_hold.blocked(pk, row, tracking=stores.tracking):
                return {"ok": False, "error": "This row is not a held uncertain submission."}
            if outcome == "submitted":
                evidence = str(body.get("confirmation") or "").strip()[:200]
                if not evidence:
                    return {"ok": False, "error": "Provide the portal confirmation."}
                stores.tracking.set_status(
                    pk, Status.APPLIED_MANUAL, confirmation_id=evidence,
                    gate_reason="", gate_pending=None, gate_call_id=None,
                    possible_submission=False, fail_kind="", fail_reason="")
                submit_hold.clear(pk, tracking=stores.tracking)
                return {"ok": True, "status": "applied_manual"}
            if body.get("new_apply_decision") is not True:
                return {"ok": False, "error": (
                    "A fresh explicit owner application decision is required to clear the hold.")}
            # No application starts here: the row returns to explicit approval.
            stores.tracking.set_status(
                pk, Status.TAILORED, possible_submission=False, fail_kind="",
                fail_reason="", gate_reason="approval", gate_pending=None,
                gate_call_id=None, last_button="", last_url="")
            submit_hold.clear(pk, tracking=stores.tracking)
            return {"ok": True, "status": "tailored", "queued": False}
        finally:
            lease.release_human_outcome(company)

    @app.post("/actions/stop-run")
    def stop_run(body: dict | None = None):
        """Stop the DISCOVERY run, the PROCESS run, or both.

        `what` is "discover", "process" or "all" (default "discover", because the
        stop control the dashboard shows sits next to Discover).

        Scoped on purpose. Stopping discovery must not touch an application that
        happens to be in flight: a crawl is disposable, while an apply is a form
        already half filled under someone's real name and abandoning it midway can
        leave a submission nobody can account for. Browser sessions are killed by
        TAG for the same reason, so "stop this discovery" ends crawl and posting
        reads only.

        Two guards have to be released, not one: this module's `_RUNNING` flags
        and the single-flight lock inside `discovery.handler`. Clearing only the
        flags would report success and then have the next run refused by the lock,
        which reads as the stop having done nothing.
        """
        import logging

        from discovery import handler as _handler
        from tools.browser_runtime import kill_live_sessions

        log = logging.getLogger("server")
        stores = make_stores(settings)

        what = ((body or {}).get("what") or "discover").lower()
        if what not in ("discover", "process", "apply", "all"):
            return {"ok": False,
                    "error": "what must be discover, process, apply or all"}

        was = {"discover": _RUNNING["discover"], "process": _RUNNING["process"]}
        # The signal that actually reaches a run already going. The per-run loops
        # compare this against the epoch they started at, so a Stop pressed now
        # lands mid sweep while a board that was merely left paused does not veto
        # the next press.
        from core import flags as _flags

        _flags.bump_stop_epoch()
        killed = 0
        if what in ("discover", "all"):
            # The crawl and the posting reads it spawns. Never "apply".
            killed += kill_live_sessions("crawl") + kill_live_sessions("jd")
            _RUNNING["discover"] = False
            # Clear every company claim. The runs themselves release in a finally,
            # so this only matters when one died without unwinding — but a claim
            # left behind would refuse that company for good.
            for company in _handler.scanning():
                _handler.stop_company(company)
        if what in ("process", "all"):
            _RUNNING["process"] = False
        if what in ("apply", "all"):
            # Ending an application mid form is the one stop with a real cost: the
            # page may be half filled, and if it had already been submitted the
            # confirmation is now unread. It is still the owner's call to make, so
            # the button exists — but nothing else in the product reaches here.
            killed += kill_live_sessions("apply")
            # Cancellation is scheduled, not completed. Resetting the queue's
            # lease here would let a human clear an uncertain hold while a
            # submission-capable browser click can still finish. The existing
            # run_queued finally/q.done releases the lease after worker unwind.
            log.info("apply cancellation requested for %d session(s); "
                     "worker leases remain until teardown", killed)

        from core.events import emit
        emit("running", agent="daemon",
             detail=f"{what} stop requested — cancellation requested for "
                    f"{killed} browser session(s)")
        return {"ok": True, "what": what, "stopped": was, "sessions_killed": killed}

    @app.get("/scan-log")
    def scan_log_view():
        """What each company's scan produced, as the sweep works through them.

        A whole watchlist sweep is hours of sequential work. Without this the
        board could say which company is being read but nothing about the forty
        that already finished, so a long run was a spinner rather than a list you
        can watch fill up.
        """
        from discovery import scan_log

        return scan_log.results(make_stores(settings).tracking.r)

    @app.get("/passed-over")
    def passed_over_view(company: str = "", limit: int = 200):
        """Roles the last scans decided NOT to keep, and the reason for each.

        A dropped job never becomes a row, so the board could not answer the
        question the owner actually asks: why is this role, which I can see on the
        careers page, not here. The three answers need three different actions, so
        they are kept apart: too old means widen the window, not relevant means
        change the preferences, and absent from this list entirely means the scan
        never saw it, which is a reading problem.
        """
        from discovery import passed_over
        from discovery.freshness import describe

        r = make_stores(settings).tracking.r
        if company:
            rows = passed_over.for_company(r, company, limit)
            for row in rows:
                row["posted_label"] = describe(row.get("posted_at") or "")
                row["company"] = company
            return {"companies": [{"company": company, "total": len(rows),
                                   "jobs": rows, "by_reason": {}}],
                    "count": len(rows)}

        groups = passed_over.by_company(r)
        total, by_reason = 0, {}
        for g in groups:
            total += g["total"]
            for k, n in g["by_reason"].items():
                by_reason[k] = by_reason.get(k, 0) + n
            for row in g["jobs"]:
                row["posted_label"] = describe(row.get("posted_at") or "")
        return {"companies": groups, "count": total, "by_reason": by_reason,
                "shown_per_company": 25}

    @app.get("/fresh")
    def fresh(hours: int = 48):
        """Roles the EMPLOYER published within `hours`, across every company.

        The question this answers is "what appeared while I was not looking", which
        no existing view could: the board groups by company and sorts by score, so
        a role posted an hour ago sits below a stronger one from three weeks back.
        Being early matters more than being thorough on a job that went up today.

        Undated postings are reported separately rather than mixed in. Many boards
        publish no date, and silently listing those as fresh would turn this into
        "roles from boards that timestamp", which is a different claim.
        """
        from discovery.freshness import age_hours, describe

        rows, undated = [], 0
        for r in make_stores(settings).tracking.all():
            if is_internal_pk(r.get("pk", "")):
                continue
            posted = r.get("posted_at") or ""
            if not posted:
                undated += 1
                continue
            age = age_hours(posted)
            if age is None or age > max(1, hours):
                continue
            rows.append({
                "pk": r["pk"], "company": r.get("company") or "",
                "title": r.get("title") or "", "location": r.get("location") or "",
                "jd_url": r.get("jd_url") or "", "status": r.get("status") or "",
                "match_score": r.get("match_score"),
                "posted_at": posted, "age_hours": round(age, 1),
                "posted_label": describe(posted),
            })
        rows.sort(key=lambda d: d["age_hours"])
        return {"hours": hours, "jobs": rows, "count": len(rows),
                "undated": undated,
                "companies": len({r["company"] for r in rows})}

    @app.get("/activity")
    def activity(days: int = 371):
        """Jobs found and applications sent, per day.

        Two different dates on purpose. A job found in a sweep and applied to a
        week later belongs to both weeks, in different columns: `discovered_at`
        answers "was discovery working", `applied_at` answers "did anything go
        out". Counting applications by their discovery date would credit the
        sweep's day and make an idle week look busy.

        Dates are bucketed in LOCAL time, because the question is about the
        owner's days, not UTC's.
        """
        from collections import Counter
        from datetime import datetime, timedelta

        def day(iso: str) -> str:
            try:
                d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return ""
            return (d.astimezone() if d.tzinfo else d).strftime("%Y-%m-%d")

        found: Counter = Counter()
        applied: Counter = Counter()
        for r in make_stores(settings).tracking.all():
            if is_internal_pk(r.get("pk", "")):
                continue
            if (k := day(r.get("discovered_at"))):
                found[k] += 1
            if r.get("status") in ("applied", "applied_manual") and (
                    k := day(r.get("applied_at"))):
                applied[k] += 1

        first = (datetime.now().astimezone() - timedelta(days=max(1, days) - 1)).date()
        out = []
        for i in range((datetime.now().astimezone().date() - first).days + 1):
            k = (first + timedelta(days=i)).strftime("%Y-%m-%d")
            out.append({"date": k, "found": found.get(k, 0), "applied": applied.get(k, 0)})
        return {"days": out,
                "totals": {"found": sum(found.values()), "applied": sum(applied.values())}}

    @app.get("/apply-queue")
    def apply_queue_state():
        """The application queue: what is waiting, per company, and what is
        running right now. `running` is the answer to "why has mine not started":
        either the limit is reached, or that company already has one going."""
        from core import flags
        from core.apply_queue import ApplyQueue, MAX_ATTEMPTS

        q = ApplyQueue(make_stores(settings).tracking.r)
        return {**q.depth(), "pending": q.pending(actionable=True), "in_flight": sorted(q.in_flight()),
                "concurrency": flags.apply_concurrency(),
                "max_attempts": MAX_ATTEMPTS}

    @app.post("/actions/apply-concurrency")
    def set_apply_concurrency(body: dict):
        """How many applications may run at once, across DIFFERENT companies.
        Takes effect on the next application rather than needing a restart."""
        from core import flags

        try:
            n = int((body or {}).get("value"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "value must be a number"}
        flags.set_flag("apply_concurrency", str(max(1, min(6, n))))
        return {"ok": True, "concurrency": flags.apply_concurrency()}

    def _tailor_and_queue(company: str, pks: list, stores, q) -> int:  # noqa: ANN001
        """Tailor the jobs rotation reached before they had a résumé, and queue
        each as it finishes.

        "Approve everything" has to include making the thing being sent. The
        profile is already stamped, so the résumé is written under the rotating
        address from the start — no re-render needed afterwards.
        """
        import logging

        from agent.run import run_job
        from core.events import emit

        done = 0
        for pk in pks:
            try:
                run_job(pk, stores)
                row = stores.tracking.get(pk) or {}
                if not submit_hold.blocked(pk, row, tracking=stores.tracking) and row.get("status") in ("tailored", "needs_human") and q.put(
                        pk, row.get("company") or company):
                    done += 1
                    emit("running", pk=pk, agent="workflow",
                         detail=f"rotate & queue → in line at {company}")
            except Exception:  # noqa: BLE001 — one bad posting must not stop the rest
                logging.getLogger("server").exception(
                    "rotate-and-queue: tailoring %s failed", pk)
        return done

    def _flush_loop(company: str, q, already_claimed: bool = False,
                    pks: set[str] | None = None) -> int:  # noqa: ANN001
        """Work through ONE company's queue, back to back, and stop there.

        One at a time, because they go to the same employer — the same lease the
        apply worker obeys, so this cannot open a second session against a company
        that already has one. The loop re-checks between applications whether the
        flush is still wanted, since four of them is over half an hour of browser
        time and Stop applying has to be able to end it.

        Shared by "Process" and by "Rotate & apply", which needs the same loop but
        only after its own tailoring has finished queueing.
        """
        import logging

        from agent.run import run_queued

        log = logging.getLogger("server")
        co = company.strip().lower()
        if not already_claimed and not q.start_flush(company):
            log.info("flush of %s skipped — already being worked through", company)
            return 0
        done = 0
        try:
            remaining = set(pks) if pks is not None else None
            while co in q.flushing():
                item = (q.next(only=company, pks=remaining) if remaining is not None
                        else q.next(only=company))
                if item is None:
                    return done                # drained, or the rest is backing off
                run_queued(item, q)            # releases the lease in its own finally
                if remaining is not None:
                    remaining.discard(item["pk"])
                done += 1
                if co not in q.flushing():
                    log.info("flush of %s stopped after %d", company, done)
                    return done
            return done
        finally:
            q.stop_flush(company)
            log.info("flush of %s finished: %d application(s)", company, done)

    def review_selection(body: dict) -> tuple[str, list[str]]:
        company, pks = body.get("company"), body.get("pks")
        if not isinstance(company, str) or not company.strip() or company.strip() == "__all__":
            raise ValueError("Choose one company for this review action.")
        if (
            not isinstance(pks, list)
            or not pks
            or len(pks) > 1000
            or any(not isinstance(pk, str) or not pk.strip() for pk in pks)
        ):
            raise ValueError("Select between 1 and 1,000 job IDs.")
        return company.strip(), list(dict.fromkeys(pks))

    def review_eligible(row: dict) -> bool:
        return not _review_row_conflict(row) and not submit_hold.blocked(row.get('pk', ''), row, tracking=make_stores(settings).tracking) and (row.get("status") == "tailored" or (
            row.get("status") == "needs_human"
            and (
                row.get("gate_reason") == "approval"
                or (row.get("gate_pending") or {}).get("question", "").startswith("Ready to apply")
            )
        ))

    def _posting_generation_visible(tracking, pk: str, displayed: object) -> bool:
        """Preflight every selected generation before granting any role.

        A stale second role must not leave the first role durably approved when
        the batch is refused. Each actual grant still uses the existing CAS.
        """
        from tools import posting_guard as pg

        try:
            current = tracking.get_current(pk)
            if not current or current.get("pk") != pk:
                return False
            if not pg.requires_review(current):
                return True
            guard = current.get("posting_guard")
            return (isinstance(displayed, str) and bool(displayed)
                    and pg.valid(current.get("jd_text") or "", guard)
                    and displayed == guard["generation"])
        except Exception:
            return False

    def _review_posting_generation(tracking, pk: str, displayed: object) -> bool:
        """Only a version actually displayed on a job-specific action can grant."""
        from tools import posting_guard as pg

        try:
            current = tracking.get_current(pk)
            if not current or current.get("pk") != pk:
                return False
            if pg.requires_review(current):
                guard = current.get("posting_guard")
                if (not isinstance(displayed, str) or not displayed
                        or not pg.valid(current.get("jd_text") or "", guard)
                        or displayed != guard["generation"]):
                    return False
                return tracking.update_posting_guard(
                    pk, current, reviewed_generation=displayed)
            return True
        except Exception:
            return False

    @app.post("/actions/apply-selection")
    def apply_selection(body: dict, background: BackgroundTasks):
        """Authorize and run exactly the selected roles, never the older queue."""
        from core.apply_queue import ApplyQueue

        try:
            company, pks = review_selection(body)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        stores = make_stores(settings)
        q = ApplyQueue(stores.tracking.r)
        for pk in pks:
            row = stores.tracking.get(pk) or {}
            if (
                (row.get("company") or "").strip().lower() != company.lower()
                or not review_eligible(row)
                or row.get("confirmation_id")
            ):
                return {
                    "ok": False,
                    "error": "A selected role is no longer available for review. Refresh and select again.",
                }
        versions = body.get("guard_generations") or {}
        if not isinstance(versions, dict) or any(
                not _posting_generation_visible(stores.tracking, pk, versions.get(pk))
                for pk in pks):
            return {"ok": False, "error": "Posting guard changed. Refresh and review the selected job generation."}
        if company.lower() in q.depth()["running"] or not q.start_flush(company):
            return {
                "ok": False,
                "error": f"{company} already has an application run. Wait for it to finish, then apply this selection.",
            }
        try:
            # Acquire the company run before durable review grants. A busy run
            # cannot leave otherwise valid roles approved without dispatch.
            if any(not _review_posting_generation(
                    stores.tracking, pk, versions.get(pk)) for pk in pks):
                q.stop_flush(company)
                return {"ok": False, "error": "Posting guard changed. Refresh and review the selected job generation."}
            for pk in pks:
                q.put(pk, company)
            background.add_task(_flush_loop, company, q, True, set(pks))
        except Exception:
            q.stop_flush(company)
            raise
        return {"ok": True, "company": company, "pks": pks, "queued": len(pks)}

    @app.post("/actions/review-decision")
    def review_decision(body: dict):
        """Persist decisions per role and report partial results for safe retries."""
        from core.apply_queue import ApplyQueue

        try:
            company, pks = review_selection(body)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        action = body.get("action")
        if action not in {"skip", "restore", "reject", "undo"}:
            return {"ok": False, "error": "Choose skip, restore, reject or undo."}
        stores = make_stores(settings)
        q = ApplyQueue(stores.tracking.r)
        pending = {it["pk"] for it in q.pending()}
        results = []
        for pk in pks:
            try:
                row = stores.tracking.get(pk) or {}
                if (row.get("company") or "").strip().lower() != company.lower():
                    raise ValueError("This role does not belong to the selected company.")
                if _review_row_conflict(row):
                    raise ValueError("Submit review is not a bulk review action.")
                if pk in q.in_flight() or submit_hold.blocked(pk, row, tracking=stores.tracking) or row.get("confirmation_id") or row.get("status") == "submitting":
                    raise ValueError(
                        "This role is applying or submitted; refresh its current state."
                    )
                if pk in pending:
                    if action not in {"skip", "reject"} or not review_eligible(row):
                        raise ValueError("Only Skip or Reject can remove a waiting application.")
                    # Removal competes atomically with dispatch. Losing means the
                    # browser may be running; never overwrite its tracking state.
                    if not q.remove(pk) or pk in q.in_flight():
                        raise ValueError("This role has started or changed; refresh its current state.")
                if action == "undo":
                    if (
                        row.get("status") != "skipped"
                        or row.get("skip_reason") != "review_rejected"
                    ):
                        raise ValueError("Only a role rejected from review can be restored.")
                    previous = row.get("review_rejected_from", "tailored")
                    if previous not in {"tailored", "needs_human"}:
                        raise ValueError("The previous review state is unavailable.")
                    stores.tracking.set_status(
                        pk, Status(previous), skip_reason="", review_skipped=False
                    )
                else:
                    if not review_eligible(row):
                        raise ValueError("This role is no longer waiting for review.")
                    if action == "reject":
                        stores.tracking.set_status(
                            pk,
                            Status.SKIPPED,
                            skip_reason="review_rejected",
                            review_rejected_from=row["status"],
                            review_skipped=False,
                        )
                    else:
                        stores.tracking.set_status(
                            pk, Status(row["status"]), review_skipped=action == "skip"
                        )
                results.append({"pk": pk, "ok": True})
            except ValueError as exc:
                results.append({"pk": pk, "ok": False, "error": str(exc)})
            except Exception:
                results.append(
                    {"pk": pk, "ok": False, "error": "Could not save this decision. Try again."}
                )
        return {"ok": all(r["ok"] for r in results), "results": results}

    @app.post("/actions/drain-company")
    def drain_company(body: dict, background: BackgroundTasks):
        """Work through ONE company's queue now, back to back, and stop there.

        The manual counterpart to the apply worker, for when automatic applying is
        paused: pause means "do not go off on your own", not "do nothing I ask", so
        this deliberately runs while paused. Nobody else's queue is touched.

        Jobs run ONE AT A TIME because they go to the same employer, which is the
        same lease the worker obeys — this cannot open a second session against a
        company that already has one, by hand or otherwise. Four applications back
        to back is over half an hour of browser sessions, so the loop checks
        between each whether the flush is still wanted; Stop applying clears it.

        NOTHING IS LEASED HERE. An earlier version took the first job in the
        handler, and when the handler then failed the job was already out of the
        queue with its company marked busy: it ran nowhere, and that employer
        looked permanently in flight. Leasing belongs inside the loop that also has
        the `finally` to release it.
        """
        import logging

        from agent.run import run_queued
        from core.apply_queue import ApplyQueue

        log = logging.getLogger("server")
        company = (body or {}).get("company", "").strip()
        if not company:
            return {"ok": False, "error": "name a company"}
        stores = make_stores(settings)
        q = ApplyQueue(stores.tracking.r)
        co = company.strip().lower()

        # Read-only checks, so a refusal changes nothing.
        depth = q.depth()
        waiting = depth["queued"].get(co, 0)
        if co in depth["running"]:
            return {"ok": False, "error": f"{company} already has an application running"}
        if not waiting:
            return {"ok": False, "error": f"nothing is waiting for {company}"}
        if not q.start_flush(company):      # atomic claim
            return {"ok": False, "error": f"{company} is already being worked through"}

        background.add_task(_flush_loop, company, q, True)  # claimed just above
        log.info("flush: working through %s (%d queued)", company, waiting)
        return {"ok": True, "company": company, "queued": waiting}

    @app.get("/apply-queue/dead")
    def apply_dead_letters():
        """Applications the queue gave up on, with every attempt recorded."""
        from core.apply_queue import ApplyQueue

        rows = ApplyQueue(make_stores(settings).tracking.r).dead_letters()
        stores = make_stores(settings)
        for r in rows:                       # a pk alone is not readable on a card
            row = stores.tracking.get(r.get("pk", "")) or {}
            r["title"] = row.get("title", "")
            r["status"] = row.get("status", "")
        return {"dead": rows}

    @app.post("/actions/apply-revive")
    def apply_revive(body: dict | None = None):
        """Put dead lettered applications back. `pk` for one, omitted for all.
        Attempts reset: asking again is a new decision, not a continuation."""
        from core.apply_queue import ApplyQueue

        pk = ((body or {}).get("pk") or "").strip()
        stores = make_stores(settings)
        if pk and submit_hold.blocked(pk, stores.tracking.get(pk) or {}, tracking=stores.tracking):
            return {"ok": False, "error": "possible submission; check the employer portal before retrying"}
        if (pk and _review_row_conflict(stores.tracking.get(pk))
                or not pk and any(_review_row_conflict(row)
                                  for row in stores.tracking.all())):
            return {"ok": False, "error": "Submit review is held or closed; start a separate fresh application run."}
        n = ApplyQueue(stores.tracking.r).revive(pk)
        return {"ok": True, "revived": n}

    @app.post("/actions/queue-apply/{pk}")
    def queue_apply(pk: str):
        """Put a job on the APPLY QUEUE instead of applying it right now.

        ▶ Apply runs the browser immediately, so approving three jobs at once has
        them racing over the one real Chrome. Queued jobs drain a lane at a time,
        in order. This is also the recovery path for a row that died mid-apply:
        it clears the failure and hands the job back to the queue, which
        previously meant editing the store by hand.
        """
        stores = make_stores(settings)
        row = stores.tracking.get(pk) or {}
        if not row:
            return {"ok": False, "error": f"no job {pk!r}"}
        # Never re-queue something already submitted — a duplicate under a real
        # name is worse than a missed application.
        if _review_row_conflict(row):
            return {"ok": False, "error": "Submit review is held or closed; start a separate fresh application run."}
        if submit_hold.blocked(pk, row, tracking=stores.tracking):
            return {"ok": False, "error": "possible submission; check the employer portal before retrying"}
        if row.get("status") in ("applied", "applied_manual"):
            return {"ok": False, "error": f"already {row.get('status')} — refusing to re-apply"}
        stores.tracking.set_status(pk, Status.TAILORED, fail_reason="", skip_reason="",
                                   gate_reason="approval")
        from core.apply_queue import ApplyQueue
        ApplyQueue(stores.tracking.r).put(pk, row.get("company", ""))
        from core.events import emit
        emit("running", pk=pk, agent="applier", detail="queued for apply…",
             url=row.get("jd_url", ""))
        return {"ok": True, "status": "queued"}

    # --- verification codes -------------------------------------------------
    # A browser session cannot read a file: its tools are the browser plus Write,
    # so an unattended run cannot reach anything on disk. It CAN read a page, so
    # the code is handed over as one. The session opens this and reloads until the
    # code shows up; the owner types it into the dashboard meanwhile.
    @app.get("/verify/{pk}", include_in_schema=False)
    def verify_page(pk: str, company: str = ""):
        """The page a waiting session reads. It HOLDS rather than answering at once.

        The first version answered immediately and asked the session to reload every
        twenty seconds. It did not work, and the log said why: the session exited
        cleanly (`exit=0`, `stop_reason: end_turn`) after seven reads and about two
        minutes. A model will not sit in a twenty-seven iteration poll loop because
        a prompt told it to — it decides it is finished.

        So one read costs time instead. The request is held open until the code
        arrives or HOLD_S passes, which turns nine minutes of waiting into a handful
        of reads rather than dozens. The page also refreshes itself, so the tab
        keeps advancing even between the session's own looks at it.
        """
        from fastapi.responses import HTMLResponse

        from core.verification import Verification

        # Answers IMMEDIATELY. It used to hold the request open for 25 seconds to
        # buy the session more patience per read. That backfired twice over: it
        # bought about a minute against a nine minute problem, and a session
        # reported the slow navigation as a FAILURE — the tab sat on newtab and the
        # read tools errored with "unparseable URL" — so the page the whole
        # mechanism depends on looked broken to the one thing that had to read it.
        # A page that answers at once and refreshes itself is worth more than a
        # page that answers slowly and looks dead.
        v = Verification(make_stores(settings).tracking.r)
        code = v.code_for(pk)
        if not code:
            # Opening the page IS the announcement. Asking the session to report
            # its wait separately would mean a run that forgot to could sit there
            # with nobody knowing it was blocked.
            v.start_waiting(pk, company)
        state = "READY" if code else ("EXPIRED" if v.waited_too_long(pk) else "WAITING")
        # Self refresh, so the tab keeps advancing between the session's own looks.
        # The page keeps itself current, so a later read is fresh without the
        # request having had to wait.
        refresh = "" if code else '<meta http-equiv="refresh" content="5">'
        # Deliberately plain: the session reads this with a page read, so the words
        # it must match are the only thing on it.
        return HTMLResponse(
            f"<!doctype html><meta charset=utf-8>{refresh}"
            f"<title>Verification {state}</title>"
            f"<body style='font:16px system-ui;padding:2rem'>"
            f"<h1>VERIFICATION {state}</h1>"
            f"<p>job: {pk}</p>"
            + (f"<p>CODE: <b style='font-size:2rem'>{code}</b></p>"
               if code else "<p>No code yet. Reload this page.</p>")
            + "</body>")

    @app.post("/actions/verify-code")
    def verify_code(body: dict):
        """Hand a verification code to the session waiting on that job."""
        from core.verification import Verification

        pk = str((body or {}).get("pk") or "").strip()
        code = str((body or {}).get("code") or "").strip()
        if not pk or not code:
            return {"ok": False, "error": "need both a job and a code"}
        v = Verification(make_stores(settings).tracking.r)
        v.give(pk, code)
        return {"ok": True, "pk": pk}

    @app.get("/verify-pending")
    def verify_pending():
        """Jobs whose browser session is sitting waiting for a code."""
        from core.verification import Verification

        return {"waiting": Verification(make_stores(settings).tracking.r).pending()}

    @app.post("/actions/skip/{pk:path}")
    @app.post("/actions/skip/{pk}")
    def skip(pk: str):
        """Decline a job: mark it skipped AND take it out of the apply queue.

        Skipping used to only set the status, leaving the pk queued — so a job the
        owner had explicitly declined was still dispatched later. The applier
        refuses it now, but a job that cannot run should not hold a place in the
        queue either: it makes the depth lie and spends that company's turn.
        """
        from core.apply_queue import ApplyQueue

        stores = make_stores(settings)
        row = stores.tracking.get(pk)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown job")
        status = str(row.get("status") or "")

        # An application that WENT OUT is a fact about the world, and skip is the
        # one action that would overwrite it with "you did not want this". Retry
        # has refused an applied row for a long time; skip never did, so a stray
        # click — or any bulk action reaching this endpoint — could erase the
        # record of something an employer had already received. The row is worth
        # more than the click.
        if _review_row_conflict(row):
            return {"ok": False, "error": "Submit review is held or closed; start a separate fresh application run."}
        if submit_hold.blocked(pk, row, tracking=stores.tracking):
            return {"ok": False, "error": "possible submission; check the employer portal before retrying"}
        if status in ("applied", "applied_manual") or row.get("confirmation_id"):
            return {"ok": False, "error": "refused",
                    "note": f"This one is already '{status or 'applied'}' — it went "
                            "out and cannot be un-sent, so it will not be marked "
                            "skipped. Use the drawer if the record itself is wrong."}
        q = ApplyQueue(stores.tracking.r)
        # Mid-submission is the other case where the status is not ours to set:
        # the form may go through a second after this call, and a skipped row that
        # then submits reads as an application nobody made.
        if status == "submitting" or pk in q.in_flight():
            return {"ok": False, "error": "in_flight",
                    "note": "It is being applied to right now, so it cannot be "
                            "skipped from here — the submit may land a moment from "
                            "now. Use Stop applying, then skip it."}

        stores.tracking.set_status(pk, Status.SKIPPED, skip_reason="user_skipped")
        removed = q.remove(pk)
        q.drop_dead_letter(pk)
        return {"ok": True, "dequeued": removed}

    RETAILOR_DEADLINE_S = 15 * 60

    @app.post("/actions/apply-now/{pk}")
    def apply_now(pk: str, background: BackgroundTasks, body: dict | None = None):
        """Run ONE queued job immediately, ahead of its turn.

        Every tailored job is queued now, so a card's button no longer means "add
        this" — it means "do this one first". Still through the same lease, so it
        cannot open a second session against a company that already has one.
        """
        import logging

        from agent.run import run_queued
        from core.apply_queue import ApplyQueue

        log = logging.getLogger("server")
        stores = make_stores(settings)
        q = ApplyQueue(stores.tracking.r)
        row = stores.tracking.get(pk)
        if not row:
            # No such job. Without this the endpoint happily leased a company and
            # dispatched a run for a pk that does not exist, which then sat in the
            # in-flight set holding that company's turn against nothing.
            return {"ok": False, "error": "No such job."}
        if _review_row_conflict(row):
            return {"ok": False, "error": "Submit review is held or closed; start a separate fresh application run."}
        if submit_hold.blocked(pk, row, tracking=stores.tracking):
            return {"ok": False, "error": "possible submission; check the employer portal before retrying"}
        if row.get("status") in ("applied", "applied_manual"):
            return {"ok": False, "error": f"Already {row['status']} — not reapplying."}
        version = (body or {}).get("posting_guard_generation")
        if not _review_posting_generation(stores.tracking, pk, version):
            return {"ok": False, "error": "Refresh this card and review the current posting safety generation."}
        company = row.get("company") or ""
        if pk in q.in_flight():
            return {"ok": False, "error": "This one is already being filled."}
        if (company or "").strip().lower() in q.depth()["running"]:
            return {"ok": False,
                    "error": f"{company} already has an application running."}
        if not q.remove(pk):
            # Not waiting — queue it, so a job that never made it in still works.
            q.put(pk, company)
        item = {"pk": pk, "company": company, "attempts": 0, "history": []}
        q.r.sadd("applyq:inflight", (company or "").strip().lower() or "unknown")
        q.r.sadd("applyq:inflight:pks", pk)

        # A press is an explicit instruction, so a stale résumé is re-tailored
        # HERE and the apply follows in the same task. The stale guard inside
        # _apply_direct hands the row to the tailor queue instead — right for the
        # worker, wrong for a button: with the board paused that queue is asleep,
        # so the row went back to `found` and the press looked like nothing at all.
        from agent.run import run_job, seed_fingerprint
        from core.events import emit
        from core.models import Status

        stale = bool(row.get("resume_tex_key")) and row.get("resume_seed") != seed_fingerprint()

        def _go() -> None:
            try:
                if stale:
                    stores.tracking.set_status(pk, Status.FOUND, gate_reason="", fail_reason="")
                    emit("running", pk=pk, agent="tailor", url=row.get("jd_url"),
                         detail="base résumé changed — re-tailoring first, then applying")
                    # With a deadline. A model call stuck behind a rate limit once
                    # held Netflix's lease for three and a half hours from here.
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        try:
                            pool.submit(run_job, pk, stores).result(timeout=RETAILOR_DEADLINE_S)
                        except concurrent.futures.TimeoutError:
                            stores.tracking.set_status(pk, Status.FOUND, gate_reason="", fail_reason="")
                            emit("error", pk=pk, agent="tailor", url=row.get("jd_url"),
                                 detail=f"re-tailoring did not finish in {RETAILOR_DEADLINE_S // 60} min "
                                        "(the model API may be rate limiting) — not applied, press Apply again later")
                            log.warning("apply-now: re-tailor of %s hit the %ds deadline", pk, RETAILOR_DEADLINE_S)
                            q.done(item)
                            return
                    q.remove(pk)     # run_job queued it again; this press owns the turn
                    after = stores.tracking.get(pk) or {}
                    if after.get("status") not in ("tailored", "needs_human"):
                        log.info("apply-now: %s did not come back tailored (%s) — not applying",
                                 pk, after.get("status"))
                        q.done(item)
                        return
                run_queued(item, q)
            except Exception:
                log.exception("apply-now failed for %s", pk)
                q.done(item)

        background.add_task(_go)
        log.info("apply-now: %s%s", pk, " (re-tailoring first)" if stale else "")
        return {"ok": True, "started": pk, "retailoring": stale}

    @app.post("/actions/queue-remove/{pk}")
    def queue_remove(pk: str):
        """Take a job OUT of the apply queue without judging it.

        Different from skip: the row keeps its status, so it stays on the board and
        can be queued again later. This is "not now", where skip is "not at all".
        """
        from core.apply_queue import ApplyQueue

        stores = make_stores(settings)
        if _review_row_conflict(stores.tracking.get(pk)):
            return {"ok": False, "error": "Submit review is held or closed; start a separate fresh application run."}
        if not stores.tracking.get(pk):
            # set_status writes `self.get(pk) or {"pk": pk}`, so calling it for an
            # unknown pk INVENTS a row. A typo'd id would quietly add a job to the
            # board that no posting corresponds to.
            return {"ok": False, "error": "No such job."}
        q = ApplyQueue(stores.tracking.r)
        if pk in q.in_flight():
            return {"ok": False,
                    "error": "This one is being filled in the browser right now. "
                             "Use Stop applying to end it."}
        if not q.remove(pk):
            return {"ok": False, "error": "It is not in the queue."}
        # Back to needing your go-ahead, so it reappears under Ready to apply
        # instead of vanishing from the board entirely.
        stores.tracking.set_status(pk, Status.TAILORED, gate_reason="approval")
        return {"ok": True, "removed": pk}

    @app.post("/actions/retailor/{pk:path}")
    def retailor_job(pk: str, body: dict, background: BackgroundTasks):
        """Re-tailor ONE résumé with a note from the owner. Applies nothing.

        The owner reads a tailored résumé, sees it under-plays what the posting is
        really about, and had no way to say so: Retry re-runs the whole pipeline,
        applier included, so adjusting a résumé with it submits an application.

        The note is stored on the row, so the automatic re-tailor that the
        stale-résumé guard triggers — the run that actually reaches the employer —
        uses it too. Sending an empty note clears it, because the box shows the
        note in force and blanking it is how a person removes one.

        Refusals live in `retailor`, not here, so the CLI and the button cannot
        drift on which rows may be rewritten.
        """
        from agent.run import retailor

        note = str((body or {}).get("note") or "")
        stores = make_stores(settings)
        row = stores.tracking.get(pk) or {}
        if not row:
            return {"ok": False, "error": "no such job"}
        if _review_row_conflict(row):
            return {"ok": False, "error": "Submit review is held or closed; start a separate fresh application run."}
        if submit_hold.blocked(pk, row, tracking=stores.tracking):
            return {"ok": False, "error": "possible submission; check the employer portal before retrying"}
        if (st := row.get("status")) in ("applied", "applied_manual"):
            return {"ok": False,
                    "error": "This application has already gone out. A fresh "
                             "résumé here would look like the one they received."}
        if st == "submitting":
            return {"ok": False,
                    "error": "A browser is filling this form right now — try "
                             "again once it finishes."}

        def _go() -> None:
            import logging

            from core.events import emit

            try:
                emit("running", pk=pk, agent="tailor", url=row.get("jd_url"),
                     detail="re-tailoring with your note…" if note.strip()
                            else "re-tailoring…")
                out = retailor(pk, note, stores)
                emit("response", pk=pk, agent="tailor", url=row.get("jd_url"),
                     detail=("✅ résumé re-tailored" if out.get("result") == "ok"
                             else f"re-tailor did not finish: {out.get('result')}"))
            except Exception:
                logging.getLogger("server").exception("re-tailor failed for %s", pk)
                emit("error", pk=pk, agent="tailor", url=row.get("jd_url"),
                     detail="re-tailoring failed — the previous résumé is kept")

        background.add_task(_go)
        return {"ok": True, "status": "retailoring", "pk": pk, "note": note.strip()}

    @app.post("/actions/retry/{pk}")
    def retry(pk: str, background: BackgroundTasks):
        """Explicit fresh preparation for closed reviews; other Retry stays unchanged."""
        from agent.run import retry_job, retry_precheck
        stores = make_stores(settings)
        row = stores.tracking.get(pk) or {}
        if submit_hold.blocked(pk, row, tracking=stores.tracking):
            return {"ok": False, "error": "possible submission; check the employer portal before retrying"}
        if _review_row_conflict(row):
            # Never borrow generic queue/resume/revive authority. The worker
            # repeats this check before atomically consuming the closed token.
            if not submit_review.manual_restart_allowed(
                    pk, row, tracking=stores.tracking):
                return {"ok": False, "error": "Only an unsent, closed review with no live holder can start a fresh run."}
            background.add_task(retry_job, pk, explicit_review_restart=True)
            return {"ok": True, "status": "retrying", "fresh_preparation": True}
        # The same read-only admission predicate the worker applies; the worker
        # still rechecks any later race. "retrying" only for a scheduled retry.
        refusal = retry_precheck(pk, stores)
        if refusal is not None:
            return {"ok": False, "status": refusal.get("result"),
                    "reason": refusal.get("reason"),
                    "error": refusal.get("detail") or _retry_refusal_text(refusal)}
        background.add_task(retry_job, pk)
        return {"ok": True, "status": "retrying"}

    @app.post("/actions/submit-review-mode")
    def set_submit_review_mode(body: dict, request: Request):
        _require_submit_review_first_party(request)
        from core import flags
        from tools import browser_runtime, browser_skill
        on = (body or {}).get("enabled")
        if type(on) is not bool:
            raise HTTPException(422, "enabled must be boolean",
                                headers={"Cache-Control": "no-store"})
        if on:
            try:
                supported = browser_runtime.backend() is browser_skill
            except Exception:
                supported = False
            if not supported:
                raise HTTPException(409, "submit_review requires the browser_skill engine",
                                    headers={"Cache-Control": "no-store"})
        if not flags.set_submit_review_enabled(on):
            raise HTTPException(503, "Submit review flag storage is unavailable.",
                                headers={"Cache-Control": "no-store"})
        if not on:
            submit_review.invalidate_all()
        return JSONResponse({"ok": True, "submit_review": flags.submit_review_enabled()},
                            headers={"Cache-Control": "no-store"})

    @app.post("/actions/submit-review/{pk:path}")
    async def decide_submit_review(pk: str, body: dict, request: Request):
        _require_submit_review_first_party(request)
        import asyncio
        try:
            decision = body.get("decision")
            generation = body.get("expected_gate_generation")
            reason = body.get("reason", "")
            future = submit_review.claim(pk, generation, decision, reason)
        except (submit_review.Refused, TypeError, AttributeError):
            raise HTTPException(409, "Review generation is invalid, stale or consumed.",
                                headers={"Cache-Control": "no-store"}) from None
        # The live application worker, not this HTTP handler, performs the
        # private readback, fresh safety checks and any raw Submit effect.
        try:
            outcome = await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(future)), timeout=90)
        except asyncio.TimeoutError:
            outcome = {"ok": True, "review_state": "claimed",
                       "note": "Decision consumed; inspect the updated job state."}
        status = 200 if outcome.get("ok") else 409
        return JSONResponse(outcome, status_code=status,
                            headers={"Cache-Control": "no-store"})

    @app.post("/actions/mode")
    def set_mode(body: dict):
        """Flip apply mode: 'gated' (approve each) / 'auto' (apply while asleep)."""
        from core import flags
        mode = (body.get("mode") or "").lower()
        if mode not in ("gated", "auto", "assisted"):
            return {"ok": False, "note": "mode must be 'gated', 'auto' or 'assisted'"}
        flags.set_flag("apply_mode", mode)
        return {"ok": True, "apply_mode": mode}

    @app.post("/actions/browser-mode")
    def set_browser_mode(body: dict):
        """Toggle the apply browser between visible windows and HEADLESS (no GUI).
        Headless is faster/less intrusive, but a CAPTCHA or human handoff can't
        open a window — it surfaces on the board with a screenshot instead."""
        from core import flags
        flags.set_flag("headless", "yes" if (body or {}).get("headless") else "no")
        return {"ok": True, "headless": flags.browser_headless()}

    @app.post("/actions/approve-all")
    def approve_all(body: dict):
        """Approve every job waiting only for your go-ahead (gate 'approval'),
        optionally scoped to one company, and hand them to the apply queue.

        This ENQUEUES; it does not apply. It used to run the applies itself, five
        at a time on their own Chrome profiles, which meant approving one company
        opened five sessions against that same employer at once — the exact thing
        the per company queue exists to prevent, bypassed by the button most likely
        to hit a single company. Dispatch belongs in one place, so this puts the
        work in the queue and the daemon's apply loop drains it under the per
        company lease and the live concurrency flag.

        Nothing starts here, so if the daemon is paused the jobs sit in the queue
        and the Apply queue panel says so, rather than quietly running anyway.
        """
        import logging

        from core.apply_queue import ApplyQueue

        raw_company = body.get("company")
        if not isinstance(raw_company, str) or not raw_company.strip():
            return {"ok": False, "error": "Choose a company, or explicitly choose all companies."}
        company = raw_company.strip().lower()
        # Explicit IDs bind approval to the jobs the owner actually saw. An empty
        # list means none, and a new matching job arriving mid-request stays out.
        pks = body.get("pks")
        if "pks" in body and (not isinstance(pks, list)
                              or any(not isinstance(pk, str) or not pk.strip() for pk in pks)):
            return {"ok": False, "error": "pks must be a list of job IDs."}
        selected = set(pks) if pks is not None else None
        stores = make_stores(settings)
        picked: list[tuple[str, str]] = []
        # Approval gates live on TAILORED rows (tailoring done, awaiting the
        # go-ahead); stragglers from the old flow may still sit in needs_human.
        for r in [*stores.tracking.query_status(Status.TAILORED),
                  *stores.tracking.query_status(Status.NEEDS_HUMAN)]:
            if is_internal_pk(r.get("pk", "")):
                continue
            if selected is not None and r["pk"] not in selected:
                continue
            if company != "__all__" and (r.get("company") or "").strip().lower() != company:
                continue
            q_ = (r.get("gate_pending") or {}).get("question", "")
            if (not _review_row_conflict(r)
                    and not submit_hold.blocked(r['pk'], r, tracking=stores.tracking) and (r.get("status") == "tailored"
                    or r.get("gate_reason") == "approval"
                    or q_.startswith("Ready to apply"))):
                picked.append((r["pk"], r.get("company") or ""))

        q = ApplyQueue(stores.tracking.r)
        queued = sum(1 for pk, co in picked if q.put(pk, co))
        # Bound to a local logger like every other handler here. `log` is not a
        # module global in this file, so this line raised NameError AFTER the
        # rows were queued: the work happened, the caller got a 500, and the
        # dashboard — which ignores the response — still said it had worked.
        logging.getLogger("server").info(
            "approve-all queued %d of %d eligible job(s)%s",
            queued, len(picked), f" for {company}" if company else "")
        return {"ok": True, "approving": queued, "queued": queued,
                "already_queued": len(picked) - queued}

    @app.post("/actions/pause")
    def set_pause(body: dict):
        """Freeze/unfreeze the worker + discovery without stopping the daemon."""
        from core import flags
        on = bool(body.get("paused"))
        flags.set_manual_pause(on)
        # Pausing also means "and stop what is going right now". That is the half
        # the pause flag cannot express on its own, because a run already inside
        # a loop needs to know the Stop came AFTER it started — otherwise a board
        # left paused refuses every scan the owner later presses by hand.
        if on:
            flags.bump_stop_epoch()
        return {"ok": True, "paused": flags.paused()}

    @app.post("/actions/reset")
    def reset():
        """Clear the pipeline — tracking rows, queue, live events, and stored
        résumés/screenshots. Keeps your facts + saved logins. Local mode only."""
        import shutil

        if settings.mode != "local":
            return {"ok": False, "note": "reset is local-mode only"}
        import redis

        # Work already in flight does not stop just because the store was
        # emptied. A job the evaluate worker was midway through finishes, writes
        # its result back, and reappears on a board the owner just cleared — a
        # zombie they did not ask for and cannot explain. So the reset is marked
        # first, and anything that started before it is discarded on write.
        from core import flags
        from tools import seen
        from tools.browser_runtime import kill_live_sessions

        # Refuse destructive reset if the durable hold or an in-flight attempt
        # might be the only evidence of a possibly committed submission.
        from core.apply_queue import ApplyQueue
        from tools.browser_skill import applies_running
        try:
            stores = make_stores(settings)
            queued = ApplyQueue(stores.tracking.r)
            rows = stores.tracking.all()
            if (submit_hold.any_held(tracking=stores.tracking)
                    or any(submit_hold.blocked(r['pk'], r, tracking=stores.tracking) or r.get("status") == "submitting"
                    for r in rows)
                    or queued.in_flight() or applies_running()):
                return {"ok": False, "note": (
                    "possible submission or active browser attempt; "
                    "check the employer portal before reset")}
        except Exception:
            return {"ok": False, "note": "Could not verify safe reset conditions."}
        # End the browsers first. Clearing the store while a session is still
        # filling a form leaves the owner watching it work on a job that no
        # longer exists.
        stopped = kill_live_sessions()
        flags.mark_reset()
        redis.Redis.from_url(settings.redis_url).flushdb()  # tracking + queue + events
        flags.mark_reset()  # flushdb cleared the marker — set it again
        art = Path(settings.local_dir) / "artifacts"
        if art.exists():
            shutil.rmtree(art, ignore_errors=True)
        seen.clear()  # so a fresh start re-discovers everything
        return {"ok": True, "status": "reset", "sessions_stopped": stopped}

    @app.get("/actions/diff/{pk}")
    def diff(pk: str):
        """What the tailor changed on this résumé (base vs tailored bullets)."""
        from agent.run import _base_latex
        from tools.diffing import resume_diff

        stores = make_stores(settings)
        row = stores.tracking.get(pk) or {}
        tex_key = row.get("resume_tex_key")
        if not tex_key:
            return {"changes": [], "note": "no tailored résumé yet"}
        try:
            tailored = stores.artifacts.get(tex_key).decode()
        except Exception:
            return {"changes": [], "note": "tailored résumé unavailable"}
        return {"changes": resume_diff(_base_latex(), tailored)}

    @app.get("/artifact/{key:path}")
    def artifact(key: str):
        """Serve a stored artifact (résumé PDF, screenshot) to the dashboard.

        In local mode the artifact is a real file on disk — serve it with
        FileResponse so the browser gets Range support (HTTP 206), a Content-Length
        and an `inline` disposition. Chrome's embedded PDF viewer (PDFium) probes
        with a Range request and renders BLANK in an <iframe> when the server
        ignores it and returns a plain 200 — which is exactly why the tailored
        résumé wouldn't show. FileResponse fixes that; cloud falls back to bytes."""
        import mimetypes

        ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
        disp = "inline" if ctype in ("application/pdf",) or ctype.startswith("image/") \
            else "attachment"
        # An artifact key is STABLE across re-runs (resumes/<pk>.pdf), but its bytes
        # are not: re-tailoring rewrites the same key. Without this the browser
        # keeps showing the résumé from the previous tailoring and the edit looks
        # like it never happened — must-revalidate, so a re-tailor is always visible.
        no_cache = {"Cache-Control": "no-cache, must-revalidate", "Pragma": "no-cache"}
        if settings.mode == "local":
            from fastapi.responses import FileResponse
            base = (Path(settings.local_dir) / "artifacts").resolve()
            p = (base / key).resolve()
            if p.is_file() and str(p).startswith(str(base) + "/"):  # no path traversal
                return FileResponse(str(p), media_type=ctype, headers={
                    "Content-Disposition": f'{disp}; filename="{p.name}"', **no_cache})
        try:
            data = make_stores(settings).artifacts.get(key)
        except Exception:
            return Response(status_code=404)
        return Response(data, media_type=ctype,
                        headers={"Content-Disposition": disp, **no_cache})

    @app.get("/events")
    async def events():
        """Live activity stream (SSE): every agent step's input/response, gates,
        discoveries — pushed the instant they happen."""
        import redis.asyncio as aredis

        from core.events import CHANNEL, recent

        async def gen():
            for ev in reversed(recent(400)):  # deep history first — keep runs visible
                yield f"data: {json.dumps(ev)}\n\n"
            r = aredis.from_url(settings.redis_url, decode_responses=True)
            ps = r.pubsub()
            await ps.subscribe(CHANNEL)
            try:
                async for m in ps.listen():
                    if m and m.get("type") == "message":
                        yield f"data: {m['data']}\n\n"
            finally:
                await ps.aclose()
                await r.aclose()

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # StaticFiles sends an ETag and Last-Modified but no Cache-Control, so a
    # browser is free to apply its own heuristic and serve app.js from cache
    # WITHOUT revalidating. That turns "the fix is deployed" into "the fix is
    # deployed and the page still runs yesterday's code", which reads as the fix
    # not working and costs an hour of chasing a bug that is no longer there.
    # no-cache does not mean "do not store": the browser still caches, it just
    # asks first, and the ETag above makes that a 304 with no body.
    @app.middleware("http")
    async def _revalidate_assets(request, call_next):  # noqa: ANN001, ANN202
        response = await call_next(request)
        if request.url.path.endswith((".js", ".css", ".html")) or request.url.path == "/":
            response.headers["Cache-Control"] = "no-cache"
        return response

    # The dashboard lives in dashboard.html because index.html is the public
    # landing page (Vercel serves whatever is called index.html at the root).
    # Locally, "/" should still open the dashboard.
    @app.get("/", include_in_schema=False)
    def _dashboard():
        from fastapi.responses import FileResponse

        return FileResponse(_WEB / "dashboard.html")

    # Outermost middleware, after every API route exists.
    _install_request_timing(app)

    # Static assets (app.js, styles.css, …) at the root, last so
    # the explicit API routes above win.
    app.mount("/", StaticFiles(directory=str(_WEB), html=True), name="web")
    return app


def _recover_stuck(settings) -> None:  # noqa: ANN001
    """Self-heal orphaned applies on startup. A job left in `submitting` means an
    apply was killed mid-flight (a prior run, a crash, a kill) and never reached a
    terminal state — so it hangs forever in the 'Submitting' lane. Reset it to
    `tailored` so it's un-stuck and re-appliable. Reset only (no auto re-queue) so
    we never silently re-submit something that might already have gone through. The
    daemon does this too (via _recover_orphans); the plain server must as well."""
    import logging

    from core.models import Status

    try:
        stores = make_stores(settings)
        rows = [r for r in stores.tracking.all()
                if not is_internal_pk(r.get("pk", ""))]
        from agent.run import release_claim

        stuck = [r for r in rows if r.get("status") == "submitting"]
        for r in stuck:
            if submit_hold.blocked(r['pk'], r, tracking=stores.tracking):
                stores.tracking.set_status(
                    r["pk"], Status.NEEDS_HUMAN, gate_reason="submit_uncertain",
                    fail_kind="uncertain", fail_reason="possible submission; check the employer portal before retrying",
                    gate_pending={"question": "possible submission; check the employer portal before retrying"})
                release_claim(r["pk"], stores)
                continue
            stores.tracking.set_status(r["pk"], Status.TAILORED)
            release_claim(r["pk"], stores)
        # A killed score/tailor leaves the row in 'tailoring' — reset to found.
        # Free its claim too, or every retry is refused until the TTL expires.
        mid = [r for r in rows if r.get("status") == "tailoring"]
        for r in mid:
            stores.tracking.set_status(r["pk"], Status.FOUND)
            release_claim(r["pk"], stores)
        if stuck or mid:
            logging.getLogger("server").info(
                "recovered %d 'submitting' -> tailored, %d 'tailoring' -> found",
                len(stuck), len(mid))
    except Exception:  # noqa: BLE001 - never block startup on recovery
        logging.getLogger("server").exception("orphan recovery failed")


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    import uvicorn

    _recover_stuck(get_settings())
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    serve()
