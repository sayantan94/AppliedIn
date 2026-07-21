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
_USER = "sayantan"


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


def run_job(pk: str, stores: Any = None) -> dict:
    """Run the pipeline for one discovered job through the ADK agent graph."""
    stores = stores or make_stores()
    row = stores.tracking.get(pk)
    if row is None:
        return {"result": "missing", "pk": pk}

    from core.events import emit
    emit("running", pk=pk, detail=f"{row.get('title','')} @ {row.get('company','')}",
         url=row.get("jd_url"))
    return _run(_run_job_async(pk, row, stores))


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


def apply_one(pk: str, stores: Any = None) -> dict:
    """Sync entry for the apply worker: drive the browser apply for ONE queued job.
    Runs in the apply thread so it never blocks the evaluate (score/tailor) worker."""
    stores = stores or make_stores()
    return _run(_apply_direct(pk, stores))


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
        return _run(_apply_direct(pk, stores))
    return _run(_resume_job_async(pk, answer, call_id, stores))


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
    jd_text = await _jd_text(row)

    if _no_sponsorship(jd_text):  # don't submit an application that's a guaranteed no
        from core.events import emit
        stores.tracking.set_status(pk, Status.FAILED, fail_reason=_NO_SPONSOR_REASON,
                                   skip_reason="no_sponsorship")
        emit("failed", pk=pk, agent="applier", detail="no visa sponsorship — closed", url=jd_url)
        log.info("no-sponsorship, skipping apply for pk=%s", pk)
        return {"result": "failed", "pk": pk, "reason": "no_sponsorship"}

    facts = stores.answer_bank.all_facts(company)
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

    stores.tracking.set_status(pk, Status.SUBMITTING)
    emit("running", pk=pk, agent="applier", detail="submitting application…", url=jd_url)

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
    stores.tracking.set_status(pk, Status.FAILED, fail_reason=reason)
    emit("error", pk=pk, agent="applier", detail=reason, url=jd_url)
    log.info("failed pk=%s: %s", pk, reason)
    return {"result": "failed", "pk": pk, "reason": reason}


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
    _run(_reset_session(pk))  # drop the finished session so the re-run starts clean
    stores.tracking.set_status(pk, Status.FOUND, fail_reason="",
                               gate_pending=None, gate_call_id=None, skip_reason="")
    from core.events import emit
    emit("running", pk=pk, detail=f"retry · {row.get('title','')} @ {row.get('company','')}",
         url=row.get("jd_url"))
    return run_job(pk, stores)


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


def _fail_reason(outcome: dict) -> str:
    """A plain sentence for why the apply didn't complete — shown on the card."""
    if not outcome:
        return ("The apply step didn't run a browser submission — no application was "
                "completed. Try again, or check that the posting is still live.")
    status, detail = outcome.get("status"), (outcome.get("detail") or "").strip()
    if status == "uncertain":
        return ("The form was filled and submit was clicked, but NO confirmation "
                "appeared (the page redirected). The application may not have gone "
                "through — verify it manually, or retry.")
    if status == "unknown":
        if not detail:  # agent produced no final report at all — an internal error
            return ("The browser agent ended without any final report — most likely "
                    "an internal error during the run (check the Logs), or the "
                    "posting has no application form. Retry usually resolves it.")
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
                    if question.startswith("Ready to apply"):
                        verdict = _auto_decision(pk, stores)
                        if verdict == "go":
                            auto_go = True
                            break  # close the run cleanly, then apply directly
                    # An HONEST gate reason — "approval" when it's just waiting for
                    # your go-ahead, "unknown_field" when it needs an answer.
                    reason = ("approval" if question.startswith("Ready to apply")
                              else "unknown_field")
                    stores.tracking.set_status(pk, Status.NEEDS_HUMAN,
                                               gate_reason=reason,
                                               gate_pending={"question": question},
                                               gate_call_id=call.id,
                                               gate_source=author)
                    row = stores.tracking.get(pk) or {}
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
    stores.tracking.set_status(pk, Status.FAILED, fail_reason=reason)
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
