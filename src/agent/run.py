"""Drive one job through the ADK pipeline (Runner) — mode-agnostic.

For each queued {pk}: load the job + seed résumé into an ADK session, run the
root agent (score → tailor → apply), and mirror progress to the tracking store
so the UI shows it. When the applier calls the long-running `ask_human` tool,
the run pauses: we persist `needs_human` + the question and return. `resume_job`
supplies the human's answer and continues.

Running needs the model (Anthropic local / Bedrock cloud) — set the key/creds.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from google.adk.runners import Runner
from google.genai import types

from core.config import get_settings
from core.logging import get_logger
from core.models import Status
from core.stores import make_stores

from .graph import root_agent

log = get_logger(__name__)
_APP = "appliedin"
_USER = "owner"   # ADK session namespace — one local user per install


def _session_service():
    # Durable sessions so a gated run can resume later. Local: sqlite file;
    # cloud: point APPLIEDIN_SESSION_DB at RDS. ADK builds an ASYNC engine, so
    # the URL needs an async driver (sqlite+aiosqlite / postgresql+asyncpg).
    from google.adk.sessions import DatabaseSessionService

    s = get_settings()
    url = getattr(s, "session_db_url", "") or f"sqlite:///{Path(s.local_dir)/'sessions.db'}"
    if url.startswith("sqlite:///"):
        url = url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    Path(s.local_dir).mkdir(parents=True, exist_ok=True)
    return DatabaseSessionService(db_url=url)


def _run(coro: Any) -> Any:
    """Drive an async coroutine to completion from this sync entry point. Uses a
    dedicated thread if a loop is already running (so the daemon/CLI, a Lambda,
    or the FastAPI server can all call run_job/resume_job the same way)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import threading

    box: dict[str, Any] = {}
    def _worker() -> None:
        box["v"] = asyncio.run(coro)
    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    return box.get("v")


def _base_latex() -> str:
    # Strip \pdfinfo metadata — its aspirational "Staff AI Engineer" title was
    # being read by the agents as the real current role. They should only see
    # the résumé body (real titles live in the \resumeSubheading lines).
    from tools.render import _sanitize

    path = Path(get_settings().config_dir).parent / "resume" / "base.tex"
    return _sanitize(path.read_text()) if path.exists() else ""


def seed_fingerprint() -> str:
    """Short hash of the base résumé. Stored on a row when it is tailored, so an
    edit to base.tex can be detected later: a tailored artifact is a snapshot, and
    without this a row tailored before the edit would keep being submitted with
    the old content forever — the change silently never reaches an employer."""
    import hashlib

    return hashlib.sha256(_base_latex().encode()).hexdigest()[:16]


def _prefs_notes() -> str:
    """Hard-constraint brief from preferences.yaml (no clearance, WA/CA only, …) —
    fed to the scorer so JD-level dealbreakers are caught, not just title ones."""
    from discovery.watchlist import load_preferences

    try:
        cfg = Path(get_settings().config_dir) / "preferences.yaml"
        return load_preferences(cfg).notes.strip()
    except Exception:
        return ""


def _github_context() -> str:
    """The candidate's public GitHub summary (cached), for tailoring context."""
    from discovery.watchlist import load_preferences
    from tools.github import fetch_github_context

    try:
        prefs = load_preferences(Path(get_settings().config_dir) / "preferences.yaml")
        return fetch_github_context(prefs.github or "")
    except Exception:
        return ""


# A job may reach run_job from TWO places at once: the daemon's evaluate worker
# draining the tailor queue, and process_backlog_once sweeping `found` rows for a
# scoped run. Both used to score + tailor the SAME job, doubling the LLM bill and
# producing two contradictory outcomes. This lock makes run_job idempotent.
_LOCK_TTL_SECONDS = 1800  # longer than any real score+tailor; frees a killed run


def _claim(pk: str, stores: Any) -> bool:
    """Claim exclusive ownership of this job. False = someone else has it."""
    client = getattr(stores.tracking, "r", None)
    if client is None:  # cloud mode / a tracking backend without Redis
        return True
    try:
        return bool(client.set(f"lock:job:{pk}", "1", nx=True, ex=_LOCK_TTL_SECONDS))
    except Exception:  # noqa: BLE001 — a lock outage must not stop the pipeline
        log.debug("job lock unavailable for %s", pk, exc_info=True)
        return True


def _release(pk: str, stores: Any) -> None:
    client = getattr(stores.tracking, "r", None)
    if client is None:
        return
    try:
        client.delete(f"lock:job:{pk}")
    except Exception:  # noqa: BLE001
        log.debug("could not release job lock for %s", pk, exc_info=True)


# A run killed mid-flight (daemon restart, kill -9) never reaches the `finally`
# that frees its claim, so the claim survives for the whole TTL. Startup recovery
# resets such a job's status, and MUST clear its claim in the same breath —
# otherwise the job looks runnable but every attempt is refused as "already being
# processed" until the TTL expires, which is silent and looks like a hang.
release_claim = _release


def run_job(pk: str, stores: Any = None) -> dict:
    """Run the pipeline for one discovered job through the ADK agent graph."""
    stores = stores or make_stores()
    row = stores.tracking.get(pk)
    if row is None:
        return {"result": "missing", "pk": pk}

    # Already past evaluation? Re-scoring a tailored/applied job wastes an LLM
    # round trip and can overwrite a good result with a worse one.
    status = row.get("status")
    if status not in ("found", "tailoring", None, ""):
        return {"result": "already_done", "pk": pk, "status": status}
    if not _claim(pk, stores):
        log.info("skipping %s — already being processed", pk)
        return {"result": "already_running", "pk": pk}

    from core.events import emit
    # Mark it in-progress so the board shows it WORKING (yellow, in Tailored)
    # instead of sitting silently in Found. The graph resets it to
    # tailored/skipped/gated when it finishes; an orphan (killed mid-run) is
    # recovered back to found on restart.
    if status == "found":
        stores.tracking.set_status(pk, Status.TAILORING)
    emit("running", pk=pk, detail=f"{row.get('title','')} @ {row.get('company','')}",
         url=row.get("jd_url"))
    try:
        return _run(_run_job_async(pk, row, stores))
    finally:
        _release(pk, stores)


async def _run_job_async(pk: str, row: dict, stores: Any) -> dict:
    jd_text = await _jd_text(row)  # fetch the FULL JD (discovery only had the title)

    if _no_sponsorship(jd_text):  # dead end before we waste tailoring / an application
        from core.events import emit
        stores.tracking.set_status(pk, Status.FAILED, fail_reason=_NO_SPONSOR_REASON,
                                   skip_reason="no_sponsorship")
        emit("failed", pk=pk, detail="no visa sponsorship — closed", url=row.get("jd_url"))
        log.info("no-sponsorship, closing pk=%s", pk)
        return {"result": "failed", "pk": pk, "reason": "no_sponsorship"}

    sessions = _session_service()
    state = {
        "pk": pk, "company": row.get("company", ""), "ats": row.get("ats", ""),
        "jd_url": row.get("jd_url", ""), "jd_text": jd_text,
        "base_latex": _base_latex(), "github_context": _github_context(),
        "prefs_notes": _prefs_notes() or "(none)",
    }
    # create_session is async; a retry may find it already there.
    existing = await sessions.get_session(app_name=_APP, user_id=_USER, session_id=pk)
    if existing is not None and _stale_seed(existing, state["base_latex"]):
        # The session holds the résumé the tailor edits AND the anchors the
        # truthfulness check validates against. Both come from the state captured
        # when the session was FIRST created, so a row whose session predates an
        # edit to base.tex keeps tailoring from the old résumé — and the check
        # cannot object, because it is comparing against that same old copy. A
        # project added to base.tex silently never reached an employer, and the
        # stale-résumé guard could not help: it re-queues for tailoring, but
        # re-tailoring reused this session and produced the same stale result.
        await _reset_session(pk)
        existing = None
        log.info("base résumé changed since %s was first seen — starting it fresh", pk)
    if existing is None:
        await sessions.create_session(app_name=_APP, user_id=_USER, session_id=pk, state=state)
    runner = Runner(agent=root_agent, app_name=_APP, session_service=sessions)

    msg = types.Content(role="user", parts=[types.Part(
        text=f"Apply to this job: {row.get('title','')} at {row.get('company','')}. "
             f"URL: {row.get('jd_url','')}")])
    result = await _drive_async(runner, pk, msg, stores)
    _save_output(pk, row, jd_text, stores)  # inspection folder: JD + tailored résumé
    return result


async def _jd_text(row: dict) -> str:
    """The full JD. Discovery stores only a title, so fetch the posting text from
    its URL; fall back to whatever discovery captured."""
    import asyncio

    from tools.jd import fetch_jd

    captured = row.get("jd_text", "") or ""
    url = row.get("jd_url", "")
    if url and len(captured) < 400:  # looks like just a title — fetch the real thing
        fetched = await asyncio.to_thread(fetch_jd, url)
        if fetched:
            return fetched
    return captured


# Phrases that mean the employer will NOT sponsor a work visa. You require
# sponsorship, so a posting with any of these is a dead end — close it WITHOUT
# applying rather than burn a tailored résumé + an application. Kept
# high-precision (only unambiguous negatives) so a job that DOES sponsor is
# never dropped; whitespace is collapsed before matching.
_NO_SPONSOR_PHRASES = (
    "do not sponsor", "does not sponsor", "not sponsor visa", "will not sponsor",
    "cannot sponsor", "can not sponsor", "unable to sponsor", "not able to sponsor",
    "not offer sponsorship", "not offer visa sponsorship", "do not offer visa sponsorship",
    "not offer immigration sponsorship", "not provide sponsorship",
    "not provide visa sponsorship", "unable to provide sponsorship",
    "unable to provide visa sponsorship", "no visa sponsorship",
    "no sponsorship is available", "sponsorship is not available",
    "sponsorship will not be provided", "not require sponsorship now or in the future",
    "not require immigration sponsorship", "without visa sponsorship",
    "without the need for sponsorship", "without sponsorship now or in the future",
    "must be able to work without sponsorship",
    "authorized to work without sponsorship",
    "authorized to work in the united states without sponsorship",
)

_NO_SPONSOR_REASON = ("Employer states it does not sponsor work visas — you require "
                      "sponsorship, so this was closed without applying.")


def _no_sponsorship(jd_text: str) -> bool:
    """True if the JD clearly states the employer won't sponsor a work visa."""
    if not jd_text:
        return False
    norm = " ".join(jd_text.lower().split())
    return any(p in norm for p in _NO_SPONSOR_PHRASES)


def _save_output(pk: str, row: dict, jd_text: str, stores: Any) -> None:
    """Drop the JD + tailored résumé into output/<stamp>_<company>_<id>/."""
    from tools.output import write_job_output

    tex = pdf = None
    try:
        tex = stores.artifacts.get(f"resumes/{pk}.tex").decode()
    except Exception:
        pass
    try:
        pdf = stores.artifacts.get(f"resumes/{pk}.pdf")
    except Exception:
        pass
    write_job_output(pk, company=row.get("company", ""), title=row.get("title", ""),
                     url=row.get("jd_url", ""), score=row.get("match_score"),
                     jd_text=jd_text, tex=tex, pdf=pdf)



def _enqueue_apply(pk: str, stores: Any) -> dict:
    """Hand an approved job to the apply queue instead of applying it here.

    Approving is a decision; dispatching is the queue's job. When ▶ Apply ran the
    browser itself, approving three roles at one employer opened three sessions
    against it at once — the per company rule held only for work that happened to
    arrive through the queue, and the button was the easiest way around it. A queue
    that can be walked around is not a queue.

    The row keeps its approval so the queue has something to dispatch, and the
    daemon starts it under the company lease and the live concurrency flag.
    """
    from core.apply_queue import ApplyQueue

    row = stores.tracking.get(pk) or {}
    # Terminal states are refused here as well as at dispatch. Queueing an applied
    # row is harmless (the duplicate guard catches it) but it spends that company's
    # turn on a job that cannot run.
    if row.get("status") in ("applied", "applied_manual"):
        log.warning("refusing to queue %s: already %s", pk, row.get("status"))
        return {"result": "duplicate", "pk": pk, "reason": "already_applied"}

    q = ApplyQueue(stores.tracking.r)
    fresh = q.put(pk, row.get("company") or "")
    stores.tracking.set_status(pk, Status.TAILORED, gate_reason="approval",
                               fail_reason="", fail_kind="")
    ahead = q.depth()["queued"].get((row.get("company") or "").strip().lower(), 0)
    log.info("queued %s for apply (%s in that company's queue)", pk, ahead)
    return {"result": "queued", "pk": pk, "queued": fresh, "company_depth": ahead}


def resume_job(pk: str, answer: str, stores: Any = None) -> dict:
    """Human answered the gate: SAVE the answer as a reusable fact, then continue.

    APPLIER gates (the "Ready to apply?" approval, or a field the browser needed)
    resume DETERMINISTICALLY: code drives the browser apply directly — we never
    depend on an LLM deciding to call the tool again. Gates from other stages
    (tailor/critic questions) resume through the ADK session as before.
    """
    from core.models import AnswerScope

    stores = stores or make_stores()
    row = stores.tracking.get(pk) or {}
    call_id = row.get("gate_call_id")
    if not call_id:
        # Ungated TAILORED rows (tailored before approval gates kept status, or
        # re-queued items) still take the one-click ▶ Apply: approving one means
        # "run the browser apply for it now".
        if row.get("status") == "tailored":
            return _enqueue_apply(pk, stores)
        return {"result": "not_gated", "pk": pk}

    question = (row.get("gate_pending") or {}).get("question") or ""
    approval = question.startswith("Ready to apply")
    if question and not approval and answer.strip().lower() not in ("", "approved"):
        # Bank real answers so the same question never gates again. Facts (name,
        # work auth, …) are GLOBAL; company/role-specific prose ("why this role?")
        # stays scoped to THIS company.
        company = row.get("company", "")
        personal = question.lower().startswith("why") or "this role" in question.lower() \
            or (company and company.lower() in question.lower())
        scope = AnswerScope.COMPANY if personal else AnswerScope.GLOBAL
        stores.answer_bank.put(question, answer, scope,
                               company=company or None, source="dashboard")

    if approval or row.get("gate_source") == "applier" or call_id == "direct":
        return _enqueue_apply(pk, stores)
    return _run(_resume_job_async(pk, answer, call_id, stores))


def run_queued(item: dict, q: Any) -> dict:
    """Run ONE leased application and settle its place in the queue.

    Shared by the daemon's apply worker and the dashboard's per company drain, so
    the retry rules, the dead letter path and the lease release cannot drift apart
    between them. Releasing the lease in a `finally` is the whole point: a lease
    left behind by a crash blocks that employer until the process restarts.
    """
    from core.apply_queue import is_retryable

    pk = item["pk"]
    try:
        result = _run(_apply_direct(pk, make_stores())) or {}
        log.info("apply: %s", result)
        again, why = is_retryable(result)
        if again:
            q.retry(item, why)
        return result
    except Exception as exc:  # noqa: BLE001 — one bad apply must not kill the worker
        log.exception("apply crashed for %s", pk)
        q.retry(item, f"crashed: {exc}")
        return {"result": "error", "pk": pk, "reason": str(exc)}
    finally:
        q.done(item)


async def _apply_direct(pk: str, stores: Any) -> dict:
    """Drive the browser apply for a human-approved job — pure code, no LLM in the
    control path. Fills from the answer bank, uploads the tailored PDF, drafts
    open-ended answers via the writer, submits, and records the honest outcome."""
    import base64

    from core.events import emit
    from tools.browser_apply import apply as browser_apply
    from tools.credentials import get_login

    row = stores.tracking.get(pk) or {}
    company, jd_url = row.get("company", ""), row.get("jd_url", "")

    # DUPLICATE GUARD — the one mistake this system must never make twice: a row
    # already applied (by the pipeline or by the owner's own hand) is refused
    # loudly, and its applied status is never overwritten. The tool layer checks
    # again right before the submit click; this is the cheap early exit.
    # SKIPPED belongs here too. Skipping only set the tracking status and left the
    # pk sitting in the apply queue, so a job the owner had explicitly declined was
    # still dispatched and applied to. "I do not want this one" has to mean it.
    if row.get("status") == Status.SKIPPED.value:
        detail = ("Refusing to apply: you skipped this job. Removing it from the "
                  "queue rather than sending it.")
        emit("response", pk=pk, agent="applier", detail=f"🛑 {detail}", url=jd_url)
        log.warning("skipped job reached the applier, refused: pk=%s", pk)
        return {"result": "skipped", "pk": pk, "reason": "user_skipped"}

    if row.get("status") in ("applied", "applied_manual"):
        detail = (f"Refusing to apply: this job is already '{row.get('status')}'"
                  " — a duplicate application under your name is worse than a missed one.")
        emit("response", pk=pk, agent="applier", detail=f"🛑 {detail}", url=jd_url)
        log.warning("duplicate apply refused for pk=%s (status=%s)", pk, row.get("status"))
        return {"result": "duplicate", "pk": pk, "reason": "already_applied"}

    # STALE RÉSUMÉ GUARD — the tailored .tex is a snapshot of base.tex taken when
    # the row was tailored. Editing base.tex does not touch it, so without this an
    # approved-but-old row keeps going out with the content you just removed. Send
    # it back through the tailor rather than submitting something you have edited
    # away; re-tailoring is cheap next to an application you cannot take back.
    if row.get("resume_tex_key") and row.get("resume_seed") != seed_fingerprint():
        stores.tracking.set_status(pk, Status.FOUND, gate_reason="", fail_reason="")
        stores.queue.enqueue(stores.tailor_queue, {"pk": pk})
        emit("running", pk=pk, agent="tailor", url=jd_url,
             detail="base résumé changed — re-tailoring before it goes out")
        log.info("stale résumé for pk=%s (seed %s ≠ %s); re-queued for tailoring",
                 pk, row.get("resume_seed") or "none", seed_fingerprint())
        return {"result": "retailoring", "pk": pk, "reason": "stale_resume"}

    # Claim the row and say so BEFORE the slow part. Reading the posting can take
    # a full minute (its own browser subprocess), and until this moved up the card
    # sat on its pre-approval status with no log line and no activity marker, so an
    # approved apply looked like a click that did nothing.
    prior_status = row.get("status") or Status.TAILORED
    stores.tracking.set_status(pk, Status.SUBMITTING)
    emit("running", pk=pk, agent="applier", detail="reading the posting…", url=jd_url)

    try:
        jd_text = await _jd_text(row)
    except Exception as exc:  # noqa: BLE001
        # The row was claimed as SUBMITTING a moment ago, and SUBMITTING has no
        # Apply button — leaving it there would turn a transient read failure into
        # a job the owner cannot retry. Hand the row back exactly as it was.
        stores.tracking.set_status(pk, prior_status,
                                   gate_reason=row.get("gate_reason", ""))
        emit("error", pk=pk, agent="applier", url=jd_url,
             detail=f"could not read the posting ({exc}) — not submitted, try again")
        log.exception("JD fetch failed for pk=%s", pk)
        return {"result": "error", "pk": pk, "reason": "jd_fetch_failed"}

    if _no_sponsorship(jd_text):  # don't submit an application that's a guaranteed no
        from core.events import emit
        stores.tracking.set_status(pk, Status.FAILED, fail_reason=_NO_SPONSOR_REASON,
                                   skip_reason="no_sponsorship")
        emit("failed", pk=pk, agent="applier", detail="no visa sponsorship — closed", url=jd_url)
        log.info("no-sponsorship, skipping apply for pk=%s", pk)
        return {"result": "failed", "pk": pk, "reason": "no_sponsorship"}

    facts = stores.answer_bank.all_facts(company)
    # Whichever profile this application is going out under supplies the contact
    # details, overriding the bank's defaults for this job only.
    from core import profiles as _profiles
    _prof = _profiles.resolve((stores.tracking.get(pk) or {}).get("profile_id", ""))
    if _prof:
        facts = _prof.override(facts)
    facts = _profiles.expand_all(facts)   # "{date:+6w}" -> a real date
    creds = get_login(company, stores.secrets)
    if creds:
        facts["Login email/username"] = creds.get("username", "")
        facts["Login password"] = creds.get("password", "")
    resume_tex = ""
    if row.get("resume_tex_key"):
        try:
            resume_tex = stores.artifacts.get(row["resume_tex_key"]).decode()
        except Exception:
            pass

    emit("running", pk=pk, agent="applier", detail="filling the form…", url=jd_url)

    result = await browser_apply(
        jd_url, company, facts, get_settings().browser_model,
        pk=pk, jd_text=jd_text, resume_tex=resume_tex,
        github=_github_context(), resume_path=_resume_pdf_path(row),
    )

    shot = result.pop("screenshot_b64", None)
    if shot:
        try:
            key = stores.artifacts.put("screenshots", f"{pk}.png",
                                       base64.b64decode(shot), "image/png")
            cur = stores.tracking.get(pk) or {}
            stores.tracking.set_status(pk, cur.get("status", "submitting"),
                                       screenshot_s3_key=key)
        except Exception as exc:
            log.warning("could not save screenshot for %s: %s", pk, exc)
    fields = result.pop("fields", None)
    if fields:  # what the form REALLY held (incl. every checkbox) — for the drawer
        cur = stores.tracking.get(pk) or {}
        stores.tracking.set_status(pk, cur.get("status", "submitting"), fields=fields)
    for q, a in (result.pop("drafted", None) or {}).items():
        # Bank writer-drafted answers (company scope) so a re-run or another job at
        # the same company reuses them instead of redrafting from scratch.
        from core.models import AnswerScope
        stores.answer_bank.put(q, a, AnswerScope.COMPANY, company=company, source="writer")

    status = result.get("status")
    if status == "applied":
        conf = result.get("confirmation") or "submitted"
        stores.tracking.set_status(pk, Status.APPLIED, confirmation_id=conf)
        emit("applied", pk=pk, detail=conf, url=jd_url)
        return {"result": "done", "pk": pk, "confirmation": conf}
    if status == "gate":
        q = result.get("question") or "The applier needs your input to continue."
        stores.tracking.set_status(pk, Status.NEEDS_HUMAN,
                                   gate_reason=result.get("reason") or "unknown_field",
                                   gate_pending={"question": q},
                                   gate_call_id="direct", gate_source="applier")
        emit("gate", pk=pk, agent="applier", detail=q, url=jd_url)
        log.info("gated pk=%s: %s", pk, q)
        return {"result": "gated", "pk": pk, "question": q}
    reason = _fail_reason(result)
    # A browser held by something else is an ENVIRONMENT fault, not a verdict on
    # this job: the form was never reached, nothing was filled, and the same job
    # succeeds once the browser is free. Recording it as failed burns a good
    # application and hides it in the Unable lane, so hand it back to the queue.
    from tools.claude_chrome import _is_browser_conflict

    if _is_browser_conflict(reason):
        stores.tracking.set_status(pk, Status.TAILORED, gate_reason="approval",
                                   fail_reason="", fail_kind="")
        stores.queue.enqueue(stores.apply_queue, {"pk": pk})
        emit("running", pk=pk, agent="applier", url=jd_url,
             detail="browser was busy — re-queued, nothing was submitted")
        log.info("browser conflict for pk=%s — re-queued rather than failed", pk)
        return {"result": "requeued", "pk": pk, "reason": "browser_conflict"}

    code = str(result.get("reason") or "failed")
    stores.tracking.set_status(pk, Status.FAILED, fail_reason=reason, fail_kind=code)
    emit("error", pk=pk, agent="applier", detail=reason, url=jd_url)
    log.info("failed pk=%s [%s]: %s", pk, code, reason)
    # `reason` is the CODE, because that is what the queue matches against TERMINAL.
    # It used to be the human sentence, so `application_limit` never matched and a
    # refusal that can never succeed was retried to the attempt limit: four browser
    # sessions spent on one Ramp job the board had already declined. Every terminal
    # outcome had the same fault — a duplicate, a guardrail refusal and a
    # no-sponsorship close were all retried too. The sentence moves to `detail`.
    return {"result": "failed", "pk": pk, "reason": code, "detail": reason}


def _resume_pdf_path(row: dict) -> str:
    """Absolute path to this job's tailored résumé PDF (for the browser upload)."""
    key = row.get("resume_s3_key") or ""
    if not key.endswith(".pdf"):
        return ""
    p = Path(get_settings().local_dir) / "artifacts" / key
    return str(p.resolve()) if p.exists() else ""


async def _resume_job_async(pk: str, answer: str, call_id: str, stores: Any) -> dict:
    sessions = _session_service()
    runner = Runner(agent=root_agent, app_name=_APP, session_service=sessions)
    # Answer the pending long-running ask_human call.
    resp = types.Content(role="user", parts=[types.Part(
        function_response=types.FunctionResponse(
            id=call_id, name="ask_human", response={"answer": answer}))])
    return await _drive_async(runner, pk, resp, stores)


def retry_job(pk: str, stores: Any = None) -> dict:
    """Re-run a job from scratch after a failed/errored attempt. Drops the old
    (completed) ADK session and clears the terminal state so the pipeline runs
    clean — picking up any facts added to the KB since the last try."""
    stores = stores or make_stores()
    row = stores.tracking.get(pk)
    if row is None:
        return {"result": "missing", "pk": pk}
    # Never let a retry wipe an APPLIED row back to found — that path re-runs the
    # whole pipeline including the submit, i.e. a duplicate application.
    if row.get("status") in ("applied", "applied_manual"):
        log.warning("retry refused for pk=%s: already %s", pk, row.get("status"))
        from core.events import emit
        emit("response", pk=pk, agent="applier", url=row.get("jd_url"),
             detail=f"🛑 Retry refused — this job is already '{row.get('status')}'.")
        return {"result": "duplicate", "pk": pk, "reason": "already_applied"}
    _run(_reset_session(pk))  # drop the finished session so the re-run starts clean
    stores.tracking.set_status(pk, Status.FOUND, fail_reason="", fail_kind="",
                               gate_pending=None, gate_call_id=None, skip_reason="")
    from core.events import emit
    emit("running", pk=pk, detail=f"retry · {row.get('title','')} @ {row.get('company','')}",
         url=row.get("jd_url"))
    return run_job(pk, stores)


def _stale_seed(session: Any, current_base: str) -> bool:
    """Whether a session was seeded from a different base résumé than today's.

    Compared by content rather than by a stored fingerprint so it is right even for
    sessions created before this check existed.
    """
    try:
        seeded = (getattr(session, "state", None) or {}).get("base_latex") or ""
    except Exception:  # noqa: BLE001 — an unreadable session is safest treated as stale
        return True
    return bool(current_base) and seeded != current_base


async def _reset_session(pk: str) -> None:
    """Delete the ADK session for a pk so run_job re-creates it fresh."""
    sessions = _session_service()
    try:
        await sessions.delete_session(app_name=_APP, user_id=_USER, session_id=pk)
    except Exception:  # no session yet, or already gone — fine
        pass


def _short(obj: Any, n: int = 600) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return s if len(s) <= n else s[:n] + "…"


def _min_score() -> int:
    """The stage-2 bar from preferences.yaml (min_match_score)."""
    from discovery.watchlist import load_preferences

    try:
        cfg = Path(get_settings().config_dir) / "preferences.yaml"
        return int(load_preferences(cfg).min_match_score)
    except Exception:
        return 7


def _score_gate(pk: str, text: str, stores: Any) -> dict | None:
    """Record the scorer's match score on the row (so the UI shows it) and SKIP
    the job when it's below min_match_score — a weak match shouldn't burn
    tailoring + your apply approval. Returns a skip verdict, or None to proceed."""
    from core.events import emit

    t = text.strip()
    a, b = t.find("{"), t.rfind("}")
    if a == -1 or b <= a:
        return None
    try:
        score = int(json.loads(t[a : b + 1]).get("score"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None

    # A weak model sometimes REFUSES ("I'm an AI assistant and don't have a
    # resume…") instead of scoring; the refusal parses as score 0 and used to
    # bury a perfectly good job as 'low_score'. Surface it as a retryable,
    # VISIBLE error instead.
    import re as _re
    if score <= 1 and _re.search(
            r"i'?m an ai|as an ai|i cannot|i can'?t|unable to (access|assist)"
            r"|don'?t have (a|any) (resume|r\u00e9sum\u00e9|personal)", t, _re.I):
        stores.tracking.set_status(pk, Status.ERROR,
                                   error="the scorer model refused the task instead of "
                                         "scoring — hit Retry (or switch the scorer model)")
        emit("error", pk=pk, detail="scorer refused instead of scoring — retry the job")
        log.warning("scorer refusal for pk=%s", pk)
        return {"result": "error", "pk": pk, "reason": "scorer_refused"}

    row = stores.tracking.get(pk) or {}
    stores.tracking.set_status(pk, row.get("status", "running"), match_score=score)
    threshold = _min_score()
    if score < threshold:
        stores.tracking.set_status(pk, Status.SKIPPED, skip_reason="low_score", match_score=score)
        emit("skipped", pk=pk, detail=f"match {score}/10 < {threshold} — skipped before tailoring")
        log.info("skipped pk=%s: score %s < %s", pk, score, threshold)
        return {"result": "skipped", "pk": pk, "score": score}
    return None


def _art_url(key: str | None) -> str | None:
    """Same-origin artifact URL for the dashboard. Keys hold '#' (company#job_id),
    so URL-encode it (# starts a fragment) but keep the path slashes."""
    if not key:
        return None
    from urllib.parse import quote

    return f"/artifact/{quote(key, safe='/')}"


def _apply_outcome(resp: Any) -> dict:
    """Unwrap the apply_to_job return from ADK's function-response envelope, so we
    can see whether the browser agent actually confirmed a submit."""
    if isinstance(resp, dict):
        if "status" in resp:
            return resp
        for v in resp.values():  # ADK sometimes wraps as {"result": {...}}
            if isinstance(v, dict) and "status" in v:
                return v
    return {}


# Reason codes whose plain meaning is "the board said no", not "something broke".
# Without this they surfaced as a bare code next to red FAILED styling and read as
# a bug in the pipeline, which sent the owner looking for a fault that was not
# there. Nothing was submitted in any of these; the run stopped where it should.
_BOARD_SAID_NO = {
    "application_limit": (
        "This EMPLOYER refused the submission under its own application cap. "
        "Nothing was submitted and nothing is wrong here: the cap belongs to this "
        "one company and clears on their schedule, not by retrying. It says "
        "nothing about any other employer, including others using the same job "
        "board, since each configures its own limit or none at all."),
    "duplicate_application": (
        "The board already has an application from you for this role, so it "
        "refused a second one. Nothing was submitted — this is the duplicate "
        "guard working, not a failure."),
    "already_applied": (
        "You have already applied to this role. Nothing was submitted; a second "
        "application under your name is the one thing this must never do."),
}


def _fail_reason(outcome: dict) -> str:
    """A plain sentence for why the apply didn't complete — shown on the card."""
    if not outcome:
        return ("The apply step didn't run a browser submission — no application was "
                "completed. Try again, or check that the posting is still live.")
    status, detail = outcome.get("status"), (outcome.get("detail") or "").strip()
    reason = str(outcome.get("reason") or "")
    if reason in _BOARD_SAID_NO:
        return _BOARD_SAID_NO[reason] + (f" The board said: {detail}" if detail else "")
    if status == "uncertain":
        return ("The form was filled and submit was clicked, but NO confirmation "
                "appeared (the page redirected). The application may not have gone "
                "through — verify it manually, or retry.")
    if status == "unknown":
        if not detail:  # agent produced no final report at all — an internal error
            return ("The browser agent ended without any final report — most likely "
                    "an internal error during the run (check the Logs), or the "
                    "posting has no application form. Retry usually resolves it.")
        from tools.claude_chrome import is_infrastructure

        if is_infrastructure(detail):
            return detail      # already a complete explanation; a prefix saying the
                               # agent "finished" would contradict it
        return ("The browser agent finished without confirming a submission: "
                f"{detail}")
    return detail or "Could not confirm the application was submitted."


def _auto_decision(pk: str, stores: Any) -> str:
    """When the applier asks for approval: 'go' (auto-apply now) or '' (gate to
    the human as usual)."""
    from core import flags

    if flags.apply_mode() != "auto" or flags.paused():
        return ""
    score = (stores.tracking.get(pk) or {}).get("match_score")
    if score is None or int(score) < get_settings().auto_min_score:
        return ""  # not a confident enough match — a human still decides
    return "go"


async def _drive_async(runner: Runner, pk: str, message: Any, stores: Any) -> dict:
    """Run the agent, streaming each step's input/response; catch the human gate.
    In AUTO mode the "Ready to apply?" approval is decided by code (score ≥
    threshold, under the daily cap) and the browser apply runs immediately —
    that's the find-and-apply-while-you-sleep path."""
    from contextlib import aclosing

    from core.events import emit

    last_author = None
    apply_result: Any = None  # the browser apply() return, if the applier ran it
    auto_go = False           # approval auto-granted → run _apply_direct after close
    # aclosing() closes the run generator in THIS context when we return early
    # (score-skip / gate) — otherwise ADK's OTel span detach fires in the wrong
    # context and prints a spurious traceback.
    async with aclosing(
        runner.run_async(user_id=_USER, session_id=pk, new_message=message)
    ) as agen:
        async for event in agen:
            author = getattr(event, "author", "agent")
            if author != last_author:  # entered a new pipeline step
                emit("step", pk=pk, agent=author, detail=author)
                last_author = author

            content = getattr(event, "content", None)  # the agent's response text
            if content and getattr(content, "parts", None):
                text = " ".join(p.text for p in content.parts if getattr(p, "text", None))
                if text.strip():
                    emit("response", pk=pk, agent=author, detail=_short(text.strip()))
                    if author == "scorer":  # record the score; skip if below the bar
                        verdict = _score_gate(pk, text, stores)
                        if verdict:
                            return verdict

            for call in event.get_function_calls() or []:  # tool call = step INPUT
                if call.name == "ask_human":
                    question = (call.args or {}).get("question", "")
                    # NEVER offer "the tailored résumé is saved" unless one actually
                    # is. The tailor sometimes drifts into chat and finishes without
                    # calling save_tailored_resume; approving that gate would open a
                    # browser and submit an application with NO résumé attached.
                    if question.startswith("Ready to apply") and not (
                            stores.tracking.get(pk) or {}).get("resume_s3_key"):
                        stores.tracking.set_status(
                            pk, Status.FAILED, fail_kind="no_resume",
                            fail_reason="Tailoring finished without saving a résumé — "
                                        "nothing to upload, so the apply was not offered. "
                                        "Retry to tailor it again.")
                        emit("failed", pk=pk, agent=author,
                             detail="no résumé was saved — apply gate suppressed",
                             url=(stores.tracking.get(pk) or {}).get("jd_url"))
                        log.warning("suppressed apply gate for %s: no resume saved", pk)
                        return {"result": "failed", "pk": pk, "reason": "no_resume"}
                    if question.startswith("Ready to apply"):
                        verdict = _auto_decision(pk, stores)
                        if verdict == "go":
                            auto_go = True
                            break  # close the run cleanly, then apply directly
                    # An HONEST gate reason — "approval" when it's just waiting for
                    # your go-ahead, "unknown_field" when it needs an answer.
                    reason = ("approval" if question.startswith("Ready to apply")
                              else "unknown_field")
                    # A pure approval is NOT a question: the job has FINISHED
                    # tailoring and only waits for the go-ahead, so it stays
                    # TAILORED (the board's Tailored lane) with the gate fields
                    # set for one-click approval. Real questions gate needs_human.
                    gate_status = (Status.TAILORED if reason == "approval"
                                   else Status.NEEDS_HUMAN)
                    stores.tracking.set_status(pk, gate_status,
                                               gate_reason=reason,
                                               gate_pending={"question": question},
                                               gate_call_id=call.id,
                                               gate_source=author)
                    row = stores.tracking.get(pk) or {}
                    # A finished tailor goes straight into the apply queue. Before
                    # the queue existed, approving each card WAS the throttle —
                    # there was nowhere else to hold work, so the gate had to. Now
                    # the queue holds it: one application per company at a time,
                    # under a concurrency you set, with Remove and Skip on every
                    # row. So the tailor feeds it and the queue becomes the place
                    # you decide from.
                    #
                    # This does NOT apply anything. In gated mode the apply worker
                    # leaves the queue alone and nothing is submitted until you
                    # press Process for a company. The rule that an application
                    # only goes out when you say so is unchanged; what moved is
                    # where you say it — once per company, not once per card.
                    if reason == "approval":
                        _enqueue_apply(pk, stores)
                    emit("gate", pk=pk, agent=author, detail=question, url=row.get("jd_url"),
                         screenshot=_art_url(row.get("screenshot_s3_key")))
                    log.info("gated pk=%s: %s", pk, question)
                    return {"result": "gated", "pk": pk, "question": question}
                emit("action", pk=pk, agent=author, detail=call.name,
                     input=_short(call.args or {}))

            if auto_go:
                break  # leave the agent loop; aclosing() closes the generator

            for fr in event.get_function_responses() or []:  # tool result = OUTPUT
                if fr.name == "apply_to_job":
                    apply_result = fr.response  # capture, to verify a real submit
                emit("result", pk=pk, agent=author, detail=fr.name, output=_short(fr.response))

    if auto_go:
        # DECOUPLED: don't apply inline (a slow browser session would block the
        # evaluate worker from scoring/tailoring the rest of the backlog). Hand the
        # job to the apply queue; the separate apply worker submits it under the cap.
        # Mark it TAILORED (ready to apply) — it LEAVES the `found` pool so it's
        # never re-swept, but does NOT show as "applying" while it just waits in the
        # apply queue. The apply worker flips it to SUBMITTING when it actually
        # starts, so only the jobs truly in-flight read as submitting.
        stores.tracking.set_status(pk, Status.TAILORED)
        stores.queue.enqueue(stores.apply_queue, {"pk": pk})
        emit("running", pk=pk, agent="applier",
             detail="auto-approved (score ≥ threshold) — queued to apply")
        log.info("queued-to-apply pk=%s", pk)
        return {"result": "queued_apply", "pk": pk}

    # The run finished without gating. Only call it APPLIED if the browser agent
    # actually confirmed a submit — otherwise it FAILED (dead/404 posting, no form
    # found, or the apply step never ran), and we record WHY on the row.
    final = stores.tracking.get(pk) or {}
    outcome = _apply_outcome(apply_result)
    if outcome.get("status") == "applied":
        confirmation = outcome.get("confirmation") or "submitted"
        stores.tracking.set_status(pk, Status.APPLIED, confirmation_id=confirmation)
        emit("applied", pk=pk, detail=confirmation, url=final.get("jd_url"),
             screenshot=_art_url(final.get("screenshot_s3_key")))
        return {"result": "done", "pk": pk}

    reason = _fail_reason(outcome)
    stores.tracking.set_status(pk, Status.FAILED, fail_reason=reason,
                               fail_kind=outcome.get("reason") or "")
    emit("error", pk=pk, agent="applier", detail=reason, url=final.get("jd_url"),
         screenshot=_art_url(final.get("screenshot_s3_key")))
    log.info("failed pk=%s: %s", pk, reason)
    return {"result": "failed", "pk": pk, "reason": reason}


def handler(event, context):  # noqa: ANN001 - cloud SQS event source
    """Cloud trigger: one pipeline run per SQS record (local uses the CLI loop)."""
    import json

    stores = make_stores()
    out = []
    for record in event.get("Records", []):
        pk = json.loads(record["body"])["pk"]
        try:
            out.append(run_job(pk, stores))
        except Exception:
            log.exception("pipeline failed pk=%s", pk)
            out.append({"result": "error", "pk": pk})
    return {"processed": out}
