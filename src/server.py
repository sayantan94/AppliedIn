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


def _throttle_state() -> dict:
    """Whether model calls are being held back, and for how long.

    A pipeline that has gone quiet because it is waiting out a rate limit looks
    identical to one that is stuck, so it says which.
    """
    from core.throttle import state

    return state()


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
                "discovering": _RUNNING["discover"], "processing": _RUNNING["process"],
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
                "filters": flags.company_filters()}

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
        if _RUNNING["discover"] or _RUNNING["process"]:
            return {"ok": False, "status": "already_running"}
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
                emit("applied", agent="workflow", company=name,
                     pk=f"meta#run#{name.lower()}",
                     detail=(f"{name}: {waiting} tailored job(s) awaiting your approval"
                             if waiting else
                             f"{name}: run complete — no new roles matched your "
                             f"preferences this time"))
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
            host = urlparse(url).hostname or ""
            parts = [p for p in urlparse(url).path.split("/") if p]
            slug = (parts[0] if "ashbyhq" in host or "greenhouse" in host or "lever" in host
                    else host.split(".")[-2] if "." in host else host) or "Company"
            company = slug.replace("-", " ").replace(".", " ").title()
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
        if _RUNNING["discover"]:
            return {"ok": False, "status": "already_running"}
        only = [c for c in ((body or {}).get("companies") or []) if isinstance(c, str)]
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
            return {"ok": False, "status": "already_running"}
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
        stores.queue.enqueue(stores.apply_queue, {"pk": pk})
        from core.events import emit
        emit("running", pk=pk, agent="applier", detail="queued for apply…",
             url=row.get("jd_url", ""))
        return {"ok": True, "status": "queued"}

    @app.post("/actions/skip/{pk}")
    def skip(pk: str):
        make_stores(settings).tracking.set_status(pk, Status.SKIPPED, skip_reason="user_skipped")
        return {"ok": True}

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
    def approve_all(body: dict, background: BackgroundTasks):
        """Approve every job waiting only for your go-ahead (gate 'approval'),
        optionally scoped to one company. They run 5 AT A TIME (parallel batches)
        for speed — each apply gets its own Chrome profile so the windows don't
        collide (Chrome locks a profile to one process)."""
        company = (body.get("company") or "").strip().lower()
        stores = make_stores(settings)
        pks = []
        # Approval gates live on TAILORED rows (tailoring done, awaiting the
        # go-ahead); stragglers from the old flow may still sit in needs_human.
        for r in [*stores.tracking.query_status(Status.TAILORED),
                  *stores.tracking.query_status(Status.NEEDS_HUMAN)]:
            if company and company != "__all__" and (r.get("company") or "").lower() != company:
                continue
            q = (r.get("gate_pending") or {}).get("question", "")
            if (r.get("status") == "tailored"
                    or r.get("gate_reason") == "approval"
                    or q.startswith("Ready to apply")):
                pks.append(r["pk"])

        _LANES = 5  # concurrent applies (also the number of Chrome profiles)

        def _run_all(items: list) -> None:
            import asyncio

            from agent.run import _apply_direct
            from core.logging import get_logger
            from core.stores import make_stores as _mk
            from tools.browser_apply import set_profile_override
            lg = get_logger("approve_all")

            # Run every apply as a coroutine in ONE event loop (never one
            # asyncio.run per thread — browser-use shares async objects that then
            # cross loops and crash). A queue of distinct profile dirs both caps
            # concurrency at _LANES and hands each apply its own Chrome profile
            # (Chrome locks a user_data_dir to one process). Lane 0 is the real
            # logged-in profile; 1..N are fresh (fine for public ATS forms).
            base = (getattr(settings, "browser_profile_dir", "") or "").strip()

            async def _driver() -> None:
                lanes: asyncio.Queue[str] = asyncio.Queue()
                for i in range(_LANES):
                    lanes.put_nowait(base if i == 0 else (f"{base}-{i}" if base else ""))

                async def _one(pk: str) -> None:
                    lane = await lanes.get()  # blocks until a lane frees → caps at _LANES
                    try:
                        set_profile_override(lane)  # task-local contextvar → this apply
                        await _apply_direct(pk, _mk())
                    except Exception:
                        lg.exception("approve-all failed for %s", pk)
                    finally:
                        lanes.put_nowait(lane)

                await asyncio.gather(*(_one(pk) for pk in items))

            asyncio.run(_driver())

        background.add_task(_run_all, pks)
        return {"ok": True, "approving": len(pks), "lanes": _LANES}

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
