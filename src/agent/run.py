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
    sessions = _session_service()
    state = {
        "pk": pk, "company": row.get("company", ""), "ats": row.get("ats", ""),
        "jd_url": row.get("jd_url", ""), "jd_text": jd_text,
        "base_latex": _base_latex(), "github_context": _github_context(),
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


def resume_job(pk: str, answer: str, stores: Any = None) -> dict:
    """Human answered the gate: SAVE the answer as a fact, then continue the run.

    The answer becomes a reusable fact in the answer bank (keyed by the exact
    question the agent asked + this company), so the same question never gates
    again — it auto-resolves next time.
    """
    from core.models import AnswerScope

    stores = stores or make_stores()
    row = stores.tracking.get(pk) or {}
    call_id = row.get("gate_call_id")
    if not call_id:
        return {"result": "not_gated", "pk": pk}

    question = (row.get("gate_pending") or {}).get("question")
    if question:
        stores.answer_bank.put(question, answer, AnswerScope.COMPANY,
                               company=row.get("company"), source="dashboard")
    return _run(_resume_job_async(pk, answer, call_id, stores))


async def _resume_job_async(pk: str, answer: str, call_id: str, stores: Any) -> dict:
    sessions = _session_service()
    runner = Runner(agent=root_agent, app_name=_APP, session_service=sessions)
    # Answer the pending long-running ask_human call.
    resp = types.Content(role="user", parts=[types.Part(
        function_response=types.FunctionResponse(
            id=call_id, name="ask_human", response={"answer": answer}))])
    return await _drive_async(runner, pk, resp, stores)


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


async def _drive_async(runner: Runner, pk: str, message: Any, stores: Any) -> dict:
    """Run the agent, streaming each step's input/response; catch the human gate."""
    from contextlib import aclosing

    from core.events import emit

    last_author = None
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
                    stores.tracking.set_status(pk, Status.NEEDS_HUMAN,
                                               gate_reason="low_confidence",
                                               gate_pending={"question": question},
                                               gate_call_id=call.id)
                    row = stores.tracking.get(pk) or {}
                    emit("gate", pk=pk, agent=author, detail=question, url=row.get("jd_url"),
                         screenshot=_art_url(row.get("screenshot_s3_key")))
                    log.info("gated pk=%s: %s", pk, question)
                    return {"result": "gated", "pk": pk, "question": question}
                emit("action", pk=pk, agent=author, detail=call.name,
                     input=_short(call.args or {}))

            for fr in event.get_function_responses() or []:  # tool result = OUTPUT
                emit("result", pk=pk, agent=author, detail=fr.name, output=_short(fr.response))

    stores.tracking.set_status(pk, Status.APPLIED)
    final = stores.tracking.get(pk) or {}
    emit("applied", pk=pk, detail="submitted", url=final.get("jd_url"),
         screenshot=_art_url(final.get("screenshot_s3_key")))
    return {"result": "done", "pk": pk}


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
