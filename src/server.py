"""Local web server — serves the dashboard UI + a data API over the store.

In local mode the daemon starts this: it serves the static `web/` dashboard on
localhost and exposes the same endpoints the dashboard calls (`/applications`,
`/stats`, `/actions/*`), reading the Redis-backed tracking store. In cloud the
dashboard is on Vercel and hits an API Gateway with the same routes over
DynamoDB — same contract, different host.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from core.config import get_settings
from core.models import Status
from core.stores import make_stores

_WEB = Path(__file__).resolve().parents[1] / "web"

# In-process flags so the dashboard can show whether a manual run is active and
# grey out its button. Background tasks run in this same process, so a plain dict
# is enough — no need to round-trip through Redis for a transient UI hint.
_RUNNING = {"discover": False, "process": False}


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
            and not str(r.get("pk", "")).startswith("meta#")]
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
    return not str(row.get("pk") or "").startswith("meta#")


def _to_ui(row: dict, artifacts) -> dict:
    """Map a tracking row to the shape the dashboard expects."""
    def link(key):
        # Serve artifacts same-origin (/artifact/<key>) so the browser can load
        # them — a local file:// presign can't be shown from an http page. Keys
        # contain '#' (pk = company#job_id), so URL-encode it (# starts a
        # fragment) while keeping the path slashes.
        from urllib.parse import quote

        k = row.get(key)
        return f"/artifact/{quote(k, safe='/')}" if k else None

    events = row.get("events") or []
    return {
        "pk": row.get("pk"),
        "company": row.get("company", ""),
        "title": row.get("title", ""),
        "status": row.get("status", ""),
        "match_score": row.get("match_score"),
        "gate_reason": row.get("gate_reason"),
        "fail_kind": row.get("fail_kind") or "",
        "fail_reason": row.get("fail_reason") or "",
        "tailored_at": row.get("tailored_at") or "",
        "gate_question": (row.get("gate_pending") or {}).get("question"),
        "skip_reason": row.get("skip_reason"),
        "closed_reason": _closed_reason(row),
        "resume_version": row.get("resume_version"),
        "resume_url": link("resume_s3_key"),
        "has_diff": bool(row.get("resume_tex_key")),
        "jd_url": row.get("jd_url"),
        # The posting itself, so the owner can read what the résumé was tailored
        # against without leaving the board and losing their place.
        "jd_text": (row.get("jd_text") or "")[:20000],
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


def create_app() -> FastAPI:
    app = FastAPI(title="AppliedIn")
    settings = get_settings()

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

    @app.get("/applications")
    def applications():
        stores = make_stores(settings)
        rows = [r for r in stores.tracking.all() if _is_job_row(r)]
        return {"items": [_to_ui(r, stores.artifacts) for r in rows]}

    @app.get("/stats")
    def stats():
        stores = make_stores(settings)
        counts: dict[str, int] = {}
        for r in stores.tracking.all():
            if not _is_job_row(r):
                continue
            counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
        applied = counts.get("applied", 0) + counts.get("applied_manual", 0)
        from core import flags
        return {"today_submitted": applied,
                "llm_error": flags.llm_error(),
                "queue_age_seconds": None, "paused": flags.paused(),
                "apply_mode": flags.apply_mode(),
                "headless": flags.browser_headless(),
                "auto_min_score": settings.auto_min_score,
                "counts_by_status": counts,
                "discovering": _discovery_running(), "processing": _RUNNING["process"],
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
        from tools.browser_apply import (
            _SAFE_OPTION_RX,
            _SANCTIONS_RX,
            _map_fields,
            _safe_sanctions_answer,
        )
        from tools.narrative import draft_answer

        url = (body or {}).get("url", "")
        company = (body or {}).get("company", "")
        fields = (body or {}).get("fields") or []
        jd_text = (body or {}).get("jd_text", "")
        stores = make_stores(settings)
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
        stores.tracking.set_status(
            pk, Status.APPLIED,
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
        return {"default": default,
                "profiles": [{"id": x.id, "label": x.label, "email": x.email,
                              "phone": x.phone} for x in items]}

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
                              "phone": x.phone} for x in items]}

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
            if str(pk).startswith("meta#") or not row.get("resume_tex_key"):
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
    def company_prefs(body: dict):
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
        flags.set_company_pref(name, over)

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
            try:
                emit("running", agent="workflow", company=name,
                     pk=f"meta#run#{name.lower()}",
                     detail=f"▶ {name}: one-company run — discovering…")
                found = run_discovery(only=[name], profile_id=profile_id)
                n_new = (found.get("enqueued") or 0) + (found.get("crawled") or 0)
                emit("running", agent="workflow", company=name,
                     pk=f"meta#run#{name.lower()}",
                     detail=f"{name}: discovery finished — {n_new} new posting(s); "
                            f"scoring + tailoring the backlog…")
            except Exception:
                logging.getLogger("server").exception("run-company discover failed")
            finally:
                _RUNNING["discover"] = False
            _RUNNING["process"] = True
            try:
                process_backlog_once(companies=[name])
                # Report the outcome, not the fact that the code reached the end.
                # "Tailored jobs await your approval" when nothing was found sends
                # the owner to look at an empty board and doubt the board.
                stores = make_stores(settings)
                waiting = sum(1 for r in stores.tracking.all()
                              if (r.get("company") or "").strip().lower() == name.strip().lower()
                              and r.get("status") == "tailored")
                # Distinguish "nothing matched" from "nothing was READ". They look
                # the same from here and mean opposite things: the first is a
                # preferences question the owner can act on, the second is a
                # careers page that could not be reached, and telling them to
                # loosen their preferences would waste their time and never work.
                total = sum(1 for r in stores.tracking.all()
                            if (r.get("company") or "").strip().lower()
                            == name.strip().lower()
                            and not str(r.get("pk", "")).startswith("meta#"))
                if waiting:
                    msg = f"{name}: {waiting} tailored job(s) awaiting your approval"
                elif total:
                    msg = (f"{name}: run complete — nothing NEW matched your "
                           f"preferences ({total} already tracked)")
                else:
                    msg = (f"{name}: run complete — no postings could be read from "
                           f"this careers page at all. That is a reading problem "
                           f"rather than a preferences one; check the Logs view")
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

            from agent.run import run_job
            try:
                run_job(pk, make_stores(settings))
            except Exception:
                logging.getLogger("server").exception("run-job failed for %s", pk)

        background.add_task(_run)
        return {"ok": True, "status": "running", "pk": pk}

    @app.post("/actions/tailor-text")
    def tailor_text(body: dict, background: BackgroundTasks):
        """Tailor a résumé to text you PASTE, with no URL and no application.

        The case this exists for: a recruiter messages you on LinkedIn with the
        role described in the message itself. There is nothing to fetch, often no
        posting to link to, and you want a résumé to send back rather than an
        application to submit. `/actions/apply-role` cannot serve that — it starts
        from a URL and ends at an approval gate.

        Same scorer, writer and truthfulness guard as every other tailor, so the
        résumé it produces is grounded in the same approved facts. It stops at
        TAILORED and never queues an apply: there is nothing to apply to.
        """
        import hashlib

        from core.models import JobRecord

        text = ((body or {}).get("text") or "").strip()
        if len(text) < 80:
            return {"ok": False,
                    "error": "paste the role description — a line or two is not "
                             "enough to tailor against"}
        company = ((body or {}).get("company") or "").strip() or "Reach out"
        title = ((body or {}).get("title") or "").strip() or "Role from a message"
        # Hash the TEXT, so pasting the same message twice re-tailors one row
        # instead of littering the board with near-duplicates.
        job_id = "text-" + hashlib.sha1(text.encode()).hexdigest()[:12]

        stores = make_stores(settings)
        job = JobRecord(company=company, job_id=job_id, title=title,
                        jd_url="", jd_text=text, ats="custom")
        pk = job.pk
        if not stores.tracking.put_new(job):
            stores.tracking.set_status(pk, Status.FOUND, jd_text=text, title=title,
                                       skip_reason="", fail_kind="", fail_reason="")

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

        background.add_task(_run)
        return {"ok": True, "status": "tailoring", "pk": pk,
                "company": company, "title": title}

    @app.post("/actions/apply-role")
    def apply_role(body: dict, background: BackgroundTasks):
        """Single-role workflow: paste a job URL (Greenhouse/Lever/Ashby/…) — we
        create the row, fetch the JD, score + tailor a résumé to it, and (in
        gated mode) stop at 'ready to apply' so you approve the submit. No
        discovery, no watchlist needed — one role, straight to a tailored résumé."""
        import hashlib
        from urllib.parse import urlparse

        url = ((body or {}).get("url") or "").strip()
        if url and not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        if not url:
            return {"ok": False, "error": "a job URL is required"}
        from core.models import JobRecord
        from discovery.handler import company_for_url
        # Proper company: a watchlist match by ATS host beats the ugly URL slug.
        company = ((body or {}).get("company") or "").strip() or company_for_url(url)
        if not company:
            company = _company_from_url(url)
        title = ((body or {}).get("title") or "").strip() or "Role from URL"

        job_id = hashlib.sha1(url.encode()).hexdigest()[:12]
        stores = make_stores(settings)
        job = JobRecord(company=company, job_id=job_id, title=title,
                        jd_url=url, jd_text=title, ats="custom")
        pk = job.pk
        is_new = stores.tracking.put_new(job)
        if not is_new:  # already tracked — re-tailor it from scratch
            stores.tracking.set_status(pk, Status.FOUND, skip_reason="", fail_kind="")

        def _run() -> None:
            import logging

            from agent.run import run_job
            from core.events import emit
            from tools.jd import fetch_jd_meta
            nonlocal company, title
            _RUNNING["process"] = True
            try:
                # Fetch the page to get the REAL title (and company if still unknown)
                # so the card reads "Rivian / Staff SWE - AI Platform", not
                # "Rivianvw.Tech / Role from URL".
                meta = fetch_jd_meta(url)
                if meta.get("title"):
                    title = meta["title"]
                    stores.tracking.set_status(pk, Status.FOUND, title=title,
                                               jd_text=meta.get("text") or title)
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
        return {"ok": True, "status": "running", "pk": pk, "company": company, "title": title}

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

        def _run() -> None:
            from daemon import run_discovery_once
            _RUNNING["discover"] = True
            try:
                run_discovery_once(only=only or None, profile_id=profile_id)
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
                process_backlog_once(companies=companies)
            except Exception:  # noqa: BLE001
                import logging
                logging.getLogger("server").exception("manual process failed")
            finally:
                _RUNNING["process"] = False

        background.add_task(_run)
        return {"ok": True, "status": "processing", "companies": companies}

    @app.post("/actions/resume/{pk}")
    def resume(pk: str, body: dict, background: BackgroundTasks):
        # Continue the gated run in the background so the click returns instantly;
        # the pipeline's next steps stream to the Logs view as they happen.
        from agent.run import resume_job
        background.add_task(resume_job, pk, body.get("answer", ""))
        return {"ok": True, "status": "resuming"}

    @app.post("/actions/mark-applied/{pk}")
    def mark_applied(pk: str, body: dict | None = None):
        """Human confirms an application went through out-of-band (got the email
        / saw the ACK). Marks it applied and clears any gate — the safe fix for a
        mis-detected submit, and it prevents a resubmit."""
        note = ((body or {}).get("note") or "").strip() or "Confirmed by you (email / on-screen ACK)."
        make_stores(settings).tracking.set_status(
            pk, Status.APPLIED, confirmation_id=note, gate_reason="", gate_pending=None)
        from core.events import emit
        emit("applied", pk=pk, agent="applier", detail="Marked applied by you — no resubmit.")
        return {"ok": True}

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
        from tools.claude_chrome import kill_live_sessions

        log = logging.getLogger("server")
        stores = make_stores(settings)

        what = ((body or {}).get("what") or "discover").lower()
        if what not in ("discover", "process", "apply", "all"):
            return {"ok": False,
                    "error": "what must be discover, process, apply or all"}

        was = {"discover": _RUNNING["discover"], "process": _RUNNING["process"]}
        killed = 0
        if what in ("discover", "all"):
            # The crawl and the posting reads it spawns. Never "apply".
            killed += kill_live_sessions("crawl") + kill_live_sessions("jd")
            _RUNNING["discover"] = False
            # Clear every company claim. The runs themselves release in a finally,
            # so this only matters when one died without unwinding — but a claim
            # left behind would refuse that company for good.
            _handler._release(_handler.scanning())
        if what in ("process", "all"):
            _RUNNING["process"] = False
        if what in ("apply", "all"):
            # Ending an application mid form is the one stop with a real cost: the
            # page may be half filled, and if it had already been submitted the
            # confirmation is now unread. It is still the owner's call to make, so
            # the button exists — but nothing else in the product reaches here.
            killed += kill_live_sessions("apply")
            from core.apply_queue import ApplyQueue

            q = ApplyQueue(stores.tracking.r)
            freed = q.reset_leases()
            log.info("apply stopped by owner: %d session(s), %d lease(s) freed",
                     killed, freed)

        from core.events import emit
        emit("running", agent="daemon",
             detail=f"{what} stopped by you — {killed} browser session(s) ended")
        return {"ok": True, "what": what, "stopped": was, "sessions_killed": killed}

    @app.get("/apply-queue")
    def apply_queue_state():
        """The application queue: what is waiting, per company, and what is
        running right now. `running` is the answer to "why has mine not started":
        either the limit is reached, or that company already has one going."""
        from core import flags
        from core.apply_queue import ApplyQueue, MAX_ATTEMPTS

        q = ApplyQueue(make_stores(settings).tracking.r)
        return {**q.depth(), "pending": q.pending(),
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

        def _flush() -> None:
            done = 0
            try:
                while True:
                    item = q.next(only=company)
                    if item is None:
                        return                 # drained, or the rest is backing off
                    run_queued(item, q)        # releases the lease in its own finally
                    done += 1
                    if co not in q.flushing():
                        log.info("flush of %s stopped after %d", company, done)
                        return
            finally:
                q.stop_flush(company)
                log.info("flush of %s finished: %d application(s)", company, done)

        background.add_task(_flush)
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
        n = ApplyQueue(make_stores(settings).tracking.r).revive(pk)
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
        stores.tracking.set_status(pk, Status.SKIPPED, skip_reason="user_skipped")
        q = ApplyQueue(stores.tracking.r)
        removed = q.remove(pk)
        q.drop_dead_letter(pk)
        return {"ok": True, "dequeued": removed,
                "note": ("It is being applied to right now — use Stop applying to "
                         "end that.") if pk in q.in_flight() else ""}

    @app.post("/actions/apply-now/{pk}")
    def apply_now(pk: str, background: BackgroundTasks):
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
        if row.get("status") in ("applied", "applied_manual"):
            return {"ok": False, "error": f"Already {row['status']} — not reapplying."}
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
        background.add_task(run_queued, item, q)
        log.info("apply-now: %s", pk)
        return {"ok": True, "started": pk}

    @app.post("/actions/queue-remove/{pk}")
    def queue_remove(pk: str):
        """Take a job OUT of the apply queue without judging it.

        Different from skip: the row keeps its status, so it stays on the board and
        can be queued again later. This is "not now", where skip is "not at all".
        """
        from core.apply_queue import ApplyQueue

        stores = make_stores(settings)
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

    @app.post("/actions/retry/{pk}")
    def retry(pk: str, background: BackgroundTasks):
        """Re-run a failed/errored job from scratch (clean session, current KB)."""
        from agent.run import retry_job
        background.add_task(retry_job, pk)
        return {"ok": True, "status": "retrying"}

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
        from core.apply_queue import ApplyQueue

        company = (body.get("company") or "").strip().lower()
        stores = make_stores(settings)
        picked: list[tuple[str, str]] = []
        # Approval gates live on TAILORED rows (tailoring done, awaiting the
        # go-ahead); stragglers from the old flow may still sit in needs_human.
        for r in [*stores.tracking.query_status(Status.TAILORED),
                  *stores.tracking.query_status(Status.NEEDS_HUMAN)]:
            if company and company != "__all__" and (r.get("company") or "").lower() != company:
                continue
            q_ = (r.get("gate_pending") or {}).get("question", "")
            if (r.get("status") == "tailored"
                    or r.get("gate_reason") == "approval"
                    or q_.startswith("Ready to apply")):
                picked.append((r["pk"], r.get("company") or ""))

        q = ApplyQueue(stores.tracking.r)
        queued = sum(1 for pk, co in picked if q.put(pk, co))
        log.info("approve-all queued %d of %d eligible job(s)%s",
                 queued, len(picked), f" for {company}" if company else "")
        return {"ok": True, "approving": queued, "queued": queued,
                "already_queued": len(picked) - queued}

    @app.post("/actions/pause")
    def set_pause(body: dict):
        """Freeze/unfreeze the worker + discovery without stopping the daemon."""
        from core import flags
        flags.set_flag("paused", "yes" if body.get("paused") else "no")
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
        from tools.claude_chrome import kill_live_sessions

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
                if not str(r.get("pk", "")).startswith("meta#")]
        from agent.run import release_claim

        stuck = [r for r in rows if r.get("status") == "submitting"]
        for r in stuck:
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
