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
import re
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


def _global_prefs() -> Any:
    from discovery.watchlist import Preferences, load_preferences

    try:
        return load_preferences(Path(get_settings().config_dir) / "preferences.yaml")
    except Exception:  # noqa: BLE001 — a scorer without preferences still works
        return Preferences()


def _effective_prefs(company: str) -> Any:
    """The preferences that govern THIS company: the global file with the
    company's own overrides on top. The screen has always read them this way;
    the scorer read the file alone, so "US only" on one company screened its
    titles and then scored its postings as if the rule did not exist."""
    from core import flags as _flags

    return _flags.effective_prefs(company or "", _global_prefs())


def _prefs_brief(p: Any) -> str:
    """What the owner is looking for, for the per-job scorer."""
    bits = []
    if p.titles:
        bits.append(f"Target roles: {', '.join(p.titles)}.")
    if p.seniority:
        bits.append(f"Seniority: {', '.join(p.seniority)} or above.")
    if p.include_keywords:
        bits.append("Work that RAISES fit (not required, but score it higher): "
                    + ", ".join(p.include_keywords) + ".")
    if p.exclude_keywords:
        bits.append("AVOID: " + ", ".join(p.exclude_keywords) + ".")
    if p.locations:
        loc = ", ".join(p.locations)
        bits.append(f"LOCATION REQUIRED: {loc}. A role based clearly outside of these, "
                    f"and not remote within them, is a dealbreaker — reject it.")
    if p.remote_only:
        bits.append("REMOTE ONLY: an on-site or hybrid role is a dealbreaker.")
    return " ".join(bits) or "(no stated preferences)"


def _prefs_notes(company: str = "") -> str:
    """Hard-constraint brief (no clearance, WA/CA only, …) for this company —
    fed to the scorer so JD-level dealbreakers are caught, not just title ones."""
    return (_effective_prefs(company).notes or "").strip()


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


def _claimed(pk: str, stores: Any) -> bool:
    client = getattr(stores.tracking, "r", None)
    if client is None:
        return False
    try:
        return bool(client.exists(f"lock:job:{pk}"))
    except Exception:  # noqa: BLE001
        return False


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


_ALREADY_RUNNING = ("An earlier run is still working on this job. Wait for it to "
                    "finish; if it never does, restart the daemon and it frees itself.")


def run_job(pk: str, stores: Any = None, *, prepare_only: bool = False) -> dict:
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
    from core.events import emit
    if not _claim(pk, stores):
        log.info("skipping %s — already being processed", pk)
        emit("response", pk=pk, url=row.get("jd_url"),
             detail=_ALREADY_RUNNING)
        return {"result": "already_running", "pk": pk}

    # Mark it in-progress so the board shows it WORKING (yellow, in Tailored)
    # instead of sitting silently in Found. The graph resets it to
    # tailored/skipped/gated when it finishes; an orphan (killed mid-run) is
    # recovered back to found on restart.
    if status == "found":
        stores.tracking.set_status(pk, Status.TAILORING)
    # NOTE: no address is chosen here. Tailoring is not sending, and an address
    # claimed at tailoring is an address spent on a résumé that may never go
    # anywhere: one OpenAI backlog burned eight of them before a single
    # application existed. `rotation.ensure()` picks one at dispatch instead —
    # the only moment the count of five means what it says — and re-renders the
    # contact line from the saved .tex so the PDF still matches the form.
    emit("running", pk=pk, detail=f"{row.get('title','')} @ {row.get('company','')}",
         url=row.get("jd_url"))
    try:
        return _run(_run_job_async(pk, row, stores, prepare_only=prepare_only))
    finally:
        _release(pk, stores)


async def _run_job_async(pk: str, row: dict, stores: Any, *,
                         prepare_only: bool = False) -> dict:
    jd_text = await _jd_text(row)  # fetch the FULL JD (discovery only had the title)

    if _unreadable(jd_text):
        # A posting we could not read is a posting we must not tailor for. The
        # alternative was a résumé written against an error page and queued for
        # an employer. Closed, not skipped: Retry puts it back through, so a page
        # that was down for an hour, or a site that needs the browser, gets
        # another chance once that is fixed.
        from core.events import emit
        why = ("Could not read the posting — the page returned an error or almost "
               "no text. Nothing was tailored. Retry once the site is reachable.")
        stores.tracking.set_status(pk, Status.FAILED, fail_reason=why, fail_kind="no_jd")
        emit("failed", pk=pk, detail="posting unreadable — not tailored", url=row.get("jd_url"))
        log.warning("unreadable posting for pk=%s (%d chars) — closed as no_jd", pk, len(jd_text or ""))
        return {"result": "failed", "pk": pk, "reason": "no_jd"}

    if _no_sponsorship(jd_text):  # dead end before we waste tailoring / an application
        from core.events import emit
        stores.tracking.set_status(pk, Status.FAILED, fail_reason=_NO_SPONSOR_REASON,
                                   skip_reason="no_sponsorship")
        emit("failed", pk=pk, detail="no visa sponsorship — closed", url=row.get("jd_url"))
        log.info("no-sponsorship, closing pk=%s", pk)
        return {"result": "failed", "pk": pk, "reason": "no_sponsorship"}

    sessions = _session_service()
    state = _session_state(row, jd_text)
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
    from .graph import review_agent
    runner = Runner(agent=review_agent if prepare_only else root_agent,
                    app_name=_APP, session_service=sessions)

    msg = types.Content(role="user", parts=[types.Part(
        text=f"Apply to this job: {row.get('title','')} at {row.get('company','')}. "
             f"URL: {row.get('jd_url','')}")])
    result = await _drive_async(runner, pk, msg, stores, prepare_only=prepare_only)
    _save_output(pk, row, jd_text, stores)  # inspection folder: JD + tailored résumé
    return result


async def _jd_text(row: dict, *, yielding: bool = True) -> str:
    """The full JD. Discovery stores only a title, so fetch the posting text from
    its URL; fall back to whatever discovery captured.

    `yielding` is True for the evaluate sweep, where a browser read is bulk work
    and must wait for a live application; the apply passes False, because there
    the read is part of the application's own flow."""
    import asyncio

    from tools import jd as _jd

    captured = (row.get("jd_text", "") or "").strip()
    url = row.get("jd_url", "")
    if url and len(captured) < 400:  # looks like just a title — fetch the real thing
        kind = "jd_sweep" if yielding else "jd"
        fetched = ((await asyncio.to_thread(_jd.fetch_jd, url, kind)) or "").strip()
        # Never a downgrade. This used to return whatever the fetch produced, so a
        # refusal page replaced a perfectly good listing summary.
        if len(fetched) > len(captured):
            return fetched
    return captured


def _browser_companies() -> set[str]:
    """Companies whose postings can only be read in a browser: marked so in the
    watchlist, or seen refusing a plain request during discovery."""
    from core import flags as _flags

    return _watchlist_browser_companies() | _flags.fetch_refused()


def _watchlist_browser_companies() -> set[str]:
    from pathlib import Path as _P

    from core.models import DiscoveryMode
    from discovery.watchlist import load_watchlist

    try:
        cfg = _P(get_settings().config_dir) / "watchlist.yaml"
        return {c.name.strip().lower() for c in load_watchlist(cfg)
                if c.discovery is DiscoveryMode.BROWSER}
    except Exception:  # noqa: BLE001 — no watchlist means no browser-only companies
        return set()


def prefetch_browser_jds(rows: list[dict], stores: Any) -> int:
    """Fill in descriptions for browser-only postings before the sweep runs them.

    Demand-driven: only rows about to be evaluated, only companies whose site
    refuses a plain fetch, only rows with nothing usable yet. Read a batch per
    browser session, and store what came back on the row so the per-row read
    finds it and skips the browser entirely. Returns how many rows were filled.
    """
    from tools import jd as _jd

    browser = _browser_companies()
    need = [r for r in rows
            if (r.get("company") or "").strip().lower() in browser
            and r.get("jd_url") and _unreadable(r.get("jd_text") or "")]
    if not need:
        return 0
    log.info("reading %d browser-only posting(s) before the sweep", len(need))
    got, gone = _jd.read_postings([r["jd_url"] for r in need], with_gone=True)
    filled = closed = 0
    for r in need:
        if r["jd_url"] in gone:
            # The browser read the employer's own "no longer available" page.
            # That is an answer: close the row rather than read it again next
            # sweep, and rather than tailor for a job that does not exist.
            stores.tracking.set_status(r["pk"], Status.JOB_GONE,
                                       fail_reason="The posting has been removed.")
            closed += 1
            continue
        text = got.get(r["jd_url"])
        if not text:
            continue
        stores.tracking.set_status(r["pk"], r.get("status") or Status.FOUND, jd_text=text)
        filled += 1
    log.info("browser prefetch filled %d and closed %d of %d posting(s)", filled, closed, len(need))
    return filled


_JD_FLOOR = 80
_JD_NOISE = ("something went wrong", "page not found", "access denied", "enable javascript")


def _unreadable(jd_text: str) -> bool:
    """Too little to tailor from.

    Empty, a bare title, or an error page. The floor is low on purpose — a
    three-line posting from a small company is real and must pass — so this
    catches what is plainly not a posting, and nothing more. A row it stops is
    closed as `no_jd` and can be retried, because the owner may know better.
    """
    t = " ".join((jd_text or "").lower().split())
    if len(t) < _JD_FLOOR:
        return True
    return any(n in t for n in _JD_NOISE) and len(t) < 400


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



def _enqueue_apply(pk: str, stores: Any, *, priority: bool = False) -> dict:
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
    fresh = q.put(pk, row.get("company") or "", priority=priority)
    stores.tracking.set_status(pk, Status.TAILORED, gate_reason="approval",
                               fail_reason="", fail_kind="")
    ahead = q.depth()["queued"].get((row.get("company") or "").strip().lower(), 0)
    log.info("queued %s for apply%s (%s in that company's queue)",
             pk, " — NEXT, the owner just answered its question" if priority else "",
             ahead)
    return {"result": "queued", "pk": pk, "queued": fresh, "company_depth": ahead}


# A quoted span that could be a form field label. The lookbehind/lookahead keep
# an apostrophe out of it: in "Replit's application ... field: 'What is your
# desired salary range?'" a naive pattern opens at the apostrophe in "Replit's"
# and closes at the real opening quote, yielding a 90-character span of narrative
# that beats the actual label on length.
_QUOTED = re.compile(r"""(?<![^\W_])['‘]([^'‘’]{4,120})['’](?![^\W_])"""
                     r"""|(?<![^\W_])["“]([^"“”]{4,120})["”](?![^\W_])""")


def _gate_label(question: str) -> str | None:
    r"""The FORM FIELD a gate is really about, if the gate names one.

    The applier does not ask "what is your desired salary range?". It writes a
    paragraph: which company, which role, which field, what the posting says, and
    what it needs. That paragraph was the answer-bank key, and the bank is keyed
    on the normalized form label — so nothing ever looked the answer up again.
    The owner answered, the job was re-queued, the next session read a field
    called "What is your desired salary range?", found no key resembling it, and
    gated a second time with the same question in different words.

    The label inside the quotes is the reusable part; the rest is about one
    application on one afternoon. Returns None when nothing in the gate looks
    like a field, and the caller then banks the whole question as before — a key
    nothing queries is survivable, a wrong key is not, because it puts an
    unrelated answer into a real field on a real application.
    """
    spans = [(m.group(1) or m.group(2) or "").strip() for m in _QUOTED.finditer(question or "")]
    # A label is a question or at least a phrase. A lone button name ("Submit")
    # is neither, and is the shape most likely to be quoted for another reason.
    spans = [s for s in spans if s.endswith("?") or " " in s]
    if not spans:
        return None
    asked = [s for s in spans if s.endswith("?")]
    # Longest wins: gates quote the role and the field in one breath, and the
    # field is the longer, more specific of the two.
    return max(asked or spans, key=len)


def _session_state(row: dict, jd_text: str) -> dict:
    """The world the tailor sees, built the same way from either door.

    The pipeline and a hand-driven re-tailor must agree on this, or a note the
    owner wrote would apply on the button and vanish on the automatic re-tailor
    that the stale-résumé guard triggers — which is the run that actually goes to
    the employer.
    """
    return {
        "pk": row.get("pk", ""), "company": row.get("company", ""),
        "ats": row.get("ats", ""), "jd_url": row.get("jd_url", ""),
        "jd_text": jd_text,
        "base_latex": _base_latex(), "github_context": _github_context(),
        "prefs_brief": _prefs_brief(_effective_prefs(row.get("company", ""))),
        "prefs_notes": _prefs_notes(row.get("company", "")) or "(none)",
        # The owner's standing guidance for THIS job. Empty for almost every row.
        "tailor_note": row.get("tailor_note") or "",
    }


_NO_RETAILOR = {"applied", "applied_manual", "submitting"}


async def _tailor_only(pk: str, state: dict, title: str, company: str) -> None:
    """Run the TAILOR agent alone, on a session of its own.

    Not `root_agent`: that ends at the applier, so using it to adjust a résumé
    would submit an application. A session of its own because the pipeline's
    session for this pk holds the state it was created with, and reusing it would
    tailor from the seed as it stood then — the same staleness that once kept a
    project out of every résumé it was added to.
    """
    from google.adk.runners import Runner
    from google.genai import types

    from .graph import tailor

    sessions = _session_service()
    sid = f"regen:{pk}"
    if await sessions.get_session(app_name=_APP, user_id=_USER, session_id=sid):
        await sessions.delete_session(app_name=_APP, user_id=_USER, session_id=sid)
    await sessions.create_session(app_name=_APP, user_id=_USER, session_id=sid, state=state)
    runner = Runner(agent=tailor, app_name=_APP, session_service=sessions)
    msg = types.Content(role="user", parts=[types.Part(
        text=f"Tailor the résumé for {title} at {company}.")])
    async for _ in runner.run_async(user_id=_USER, session_id=sid, new_message=msg):
        pass


def retailor(pk: str, note: str | None = None, stores: Any = None) -> dict:
    """Re-run the TAILOR for one job, with the owner's guidance folded in.

    The owner reads a tailored résumé, sees it under-plays what the posting is
    actually about, and until now had nothing to say so with: `retry_job` re-runs
    the whole pipeline, applier included, so adjusting a résumé with it submits an
    application. This runs the tailor and nothing else — nothing is submitted and
    no rotation address is spent.

    The note is STORED on the row rather than used once. Editing base.tex makes
    every tailored row stale, and the stale-résumé guard re-tailors a row before
    it applies; a note that lived only for one run would be thrown away by that
    re-tailor, and the résumé the owner had just corrected would go out
    uncorrected. `note=None` means "use whatever is stored"; an empty string
    clears it, because the box the owner types into shows the stored note and
    sending it back empty is how a person removes one.

    What the note can do is bounded by the guard, not by this function:
    `save_tailored_resume` still requires every employer, title and date line
    verbatim and still refuses a dropped bullet. "Lean into Kubernetes" reorders
    and rewords what is on the résumé; it cannot add what is not.

    APPLIED rows are refused. That document reached an employer and this cannot
    reproduce it, so writing a fresh one under the row would present a résumé
    they never received as the one that was sent. A row mid-apply is refused too:
    a browser is on the form, and swapping the PDF under it is how one job's
    résumé ends up attached to another job's application.
    """
    import asyncio

    stores = stores or make_stores()
    row = stores.tracking.get(pk)
    if not row:
        return {"result": "missing_row", "pk": pk}
    status = row.get("status") or ""
    if status in _NO_RETAILOR:
        why = ("that application has already gone out — a fresh résumé under this "
               "row would look like the one the employer received"
               if status != "submitting" else
               "a browser is filling this form right now")
        log.warning("refusing to re-tailor %s (%s)", pk, status)
        return {"result": "refused", "pk": pk, "status": status, "reason": why}

    if note is not None:
        # Keep the row exactly where it is. A queued row that gets a note must not
        # fall out of the queue or lose its approval.
        stores.tracking.set_status(pk, status or Status.FOUND, tailor_note=note.strip())
        row = stores.tracking.get(pk) or row

    jd_text = row.get("jd_text") or ""
    if len(jd_text) < 400 and row.get("jd_url"):
        from tools.jd import fetch_jd
        try:
            jd_text = fetch_jd(row["jd_url"]) or jd_text
        except Exception:  # noqa: BLE001 — a dead posting must not lose the note
            log.warning("could not re-read the posting for %s; using what we have", pk)
    if not jd_text.strip():
        return {"result": "no_jd", "pk": pk}

    state = _session_state(row, jd_text)
    log.info("re-tailoring %s%s", pk,
             " with the owner's note" if state["tailor_note"] else "")
    # Show it working. A click that changes nothing on screen reads as a dead
    # button — the same complaint the gate answer produced. TAILORING is a real
    # pipeline status the board already renders as in-progress, so the card moves
    # to the tailoring view for the duration and the button can show it is busy.
    #
    # BORROWED, not reset: the row keeps its queue entry and gets its own status
    # back at the end. A queued row that gets a note must not fall out of the
    # queue or lose its approval, and one left on `tailoring` by a failure would
    # have no Apply button and no way back.
    stores.tracking.set_status(pk, Status.TAILORING)
    try:
        _run(_tailor_only(pk, state, row.get("title", ""), row.get("company", "")))
    except Exception as exc:  # noqa: BLE001 — the status must come back regardless
        stores.tracking.set_status(pk, status or Status.FOUND)
        log.exception("re-tailor failed for %s", pk)
        return {"result": "error", "pk": pk, "reason": str(exc)}
    from datetime import datetime, timezone
    stores.tracking.set_status(pk, status or Status.FOUND,
                               retailored_at=datetime.now(timezone.utc).isoformat())
    after = stores.tracking.get(pk) or {}
    return {"result": "ok", "pk": pk,
            "resume_key": after.get("resume_s3_key", ""),
            "note": state["tailor_note"]}


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
        # File it under the FIELD the gate named, not the paragraph the gate was
        # written in. Scope is then judged on the label too: "What is your
        # desired salary range?" is a fact about the owner and belongs to every
        # employer, even though the paragraph around it said "Replit" three times
        # and would have locked it to Replit alone.
        label = _gate_label(question) or question
        personal = label.lower().startswith("why") or "this role" in label.lower() \
            or (company and company.lower() in label.lower())
        scope = AnswerScope.COMPANY if personal else AnswerScope.GLOBAL
        if label != question:
            log.info("banking the gate answer for %s under %r (from a %d-char question)",
                     pk, label, len(question))
        stores.answer_bank.put(label, answer, scope,
                               company=company or None, source="dashboard")

    if approval or row.get("gate_source") == "applier" or call_id == "direct":
        # An answered QUESTION resumes an application that is already under way:
        # the owner started it, the browser filled the form, and it stopped on one
        # field it had no approved answer for. It goes to the front, and in gated
        # mode it is the one thing the worker may take without Process, because
        # the decision to apply was made when the job was started.
        #
        # A bare "Ready to apply?" is the opposite — that IS the decision, and in
        # gated mode the decision belongs to Process. So it queues as ordinary
        # work, exactly as before.
        return _enqueue_apply(pk, stores, priority=bool(question) and not approval)
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
            # The DETAIL, not the code. `retry()` decides whether a failure spends
            # one of the job's attempts by reading the opening of what it is told,
            # and "Rate limited by the model API…" is an outage that must not.
            # Passed the code "unknown" instead, it read as an ordinary fault and
            # a rate-limited Netflix job burned attempt 2 of 3 without a browser
            # ever reaching the form.
            q.retry(item, str(result.get("detail") or why))
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

    # A re-tailor is rewriting this row's .tex and .pdf right now. Uploading a
    # file while it is being replaced is how one job's résumé ends up attached to
    # another job's application. `retailor` refuses a row that is already
    # SUBMITTING, which closes the race from its side; this closes the window
    # between the queue leasing this row and the claim below. Retryable on
    # purpose: it will be tailored again in a moment and should apply then.
    if row.get("status") == Status.TAILORING.value:
        log.info("apply deferred for %s: a re-tailor is in progress", pk)
        return {"result": "failed", "pk": pk, "reason": "mid_tailor",
                "detail": "The résumé is being re-tailored right now — this "
                          "application waits for it rather than uploading a file "
                          "that is being rewritten."}

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
        jd_text = await _jd_text(row, yielding=False)
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
    # Last call on which address this goes out under. A job tailored before this
    # company started rotating still carries the old one, and this is the last
    # moment to fix it — the résumé is re-rendered from its saved .tex, so the
    # PDF and the form still agree.
    from core import rotation as _rotation

    # Deliberately NOT wrapped. If this company rotates and no address can be
    # assigned, the application must not go out at all — falling back to the base
    # address is the one outcome rotation exists to prevent, and it cannot be
    # undone. The failure is loud and nothing is submitted.
    _rotation.ensure(pk, company, stores)
    # Whichever profile this application is going out under supplies the contact
    # details, overriding the bank's defaults for this job only.
    from core import profiles as _profiles
    _row = stores.tracking.get(pk) or {}
    _prof = _profiles.resolve_for(_row)
    # The PDF is rendered for the profile it is SENT under, here, every time.
    # Not only when the stamp changed: a board-wide re-render was once cut off by
    # a restart at 141 of 378 rows, leaving 237 rows stamped with a profile whose
    # address their PDF did not carry — a form saying one address while the
    # résumé says another is the mismatch a recruiter notices. Two seconds of
    # LaTeX per application buys the invariant outright. Rotating companies did
    # this in ensure() already; for them it is a second, harmless render.
    if _prof and _row.get("resume_tex_key"):
        if _prof.id != str(_row.get("profile_id") or ""):
            stores.tracking.set_status(pk, _row.get("status") or Status.SUBMITTING,
                                       profile_id=_prof.id)
            log.info("dispatching %s under %s's standing profile %s", pk, company, _prof.id)
        _profiles.retarget(pk, _prof, stores)
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
        _note_rotation_use(pk, stores)
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
    from tools.claude_chrome import _is_browser_conflict, is_disconnected, is_signed_out

    # A signed-out CLI is the same KIND of fault as a busy browser — nothing was
    # reached, nothing was filled — but it differs in one way that matters: it
    # does not clear on its own. Every remaining job would fail against the same
    # wall, and at three attempts each, a queue of two hundred dead-letters in
    # about a minute while the owner is away from the screen. So the job goes back
    # on the queue untouched AND applying is paused, which is the only thing that
    # stops the rest of the board following it down.
    if is_signed_out(reason):
        from core import flags

        stores.tracking.set_status(pk, Status.TAILORED, gate_reason="approval",
                                   fail_reason="", fail_kind="")
        stores.queue.enqueue(stores.apply_queue, {"pk": pk})
        if not flags.paused():
            flags.set_flag("paused", "yes")
            flags.set_flag("paused_reason", "signed out of the Claude CLI")
            log.warning("PAUSED applying: the Claude CLI is signed out")
        emit("gate", pk=pk, agent="applier", url=jd_url, detail=reason)
        return {"result": "requeued", "pk": pk, "reason": "signed_out"}

    if is_disconnected(reason):
        # Same fault class as a signed-out CLI: every queued job would fail the
        # same way until a person reconnects the extension. The job goes back
        # untouched — no attempt spent — and the board pauses with the reason on
        # it, rather than three doomed sessions dead-lettering a job that never
        # reached a form.
        from core import flags

        stores.tracking.set_status(pk, Status.TAILORED, gate_reason="approval",
                                   fail_reason="", fail_kind="")
        stores.queue.enqueue(stores.apply_queue, {"pk": pk})
        if not flags.paused():
            flags.set_flag("paused", "yes")
            flags.set_flag("paused_reason", "the Claude browser extension is disconnected")
            log.warning("PAUSED applying: the browser extension is not connected")
        emit("gate", pk=pk, agent="applier", url=jd_url, detail=reason)
        return {"result": "requeued", "pk": pk, "reason": "extension_disconnected"}

    if _is_browser_conflict(reason):
        stores.tracking.set_status(pk, Status.TAILORED, gate_reason="approval",
                                   fail_reason="", fail_kind="")
        stores.queue.enqueue(stores.apply_queue, {"pk": pk})
        emit("running", pk=pk, agent="applier", url=jd_url,
             detail="browser was busy — re-queued, nothing was submitted")
        log.info("browser conflict for pk=%s — re-queued rather than failed", pk)
        return {"result": "requeued", "pk": pk, "reason": "browser_conflict"}

    # The STATUS is the fallback code, not the word "failed". An uncertain outcome
    # carries no `reason` — the browser died partway and may already have
    # submitted — so collapsing it to "failed" threw away the one fact that makes
    # it terminal, and it reached the queue looking like an ordinary fault. That
    # was harmless only while the retry branch was unreachable; the moment retries
    # work, it is the difference between one application and two under a real name.
    code = str(result.get("reason") or result.get("status") or "failed")
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



def _note_rotation_use(pk: str, stores: Any) -> None:
    """Record against the rotating address that it carried a real application.

    Written to the ledger file, so the count of five survives a lost board. It
    happens HERE — where a submission is confirmed — and not where one is
    dispatched, because five gated attempts that submitted nothing once retired
    an address that had never been used.
    """
    try:
        from core import rotation

        rotation.record_submission(pk, (stores.tracking.get(pk) or {}).get("profile_id", ""))
    except Exception:  # noqa: BLE001 — bookkeeping must not disturb a done application
        log.warning("could not record the address used for pk=%s", pk, exc_info=True)


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
    if _claimed(pk, stores):
        # Wiping a row to `found` under a run that still owns it leaves the row
        # unrunnable once that run dies: found, claimed, and skipped by recovery.
        from core.events import emit
        emit("response", pk=pk, url=row.get("jd_url"), detail=_ALREADY_RUNNING)
        return {"result": "already_running", "pk": pk}
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


async def _drive_async(runner: Runner, pk: str, message: Any, stores: Any, *,
                       prepare_only: bool = False) -> dict:
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

    if prepare_only:
        row = stores.tracking.get(pk) or {}
        if not str(row.get("resume_s3_key") or "").endswith(".pdf"):
            stores.tracking.set_status(pk, Status.FAILED, fail_kind="no_resume",
                                       fail_reason="No PDF was saved. Retry preparation.")
            return {"result": "failed", "pk": pk, "reason": "no_resume"}
        stores.tracking.set_status(pk, Status.TAILORED, gate_reason="approval",
                                   gate_pending={"question":
                                                 "Ready to apply? Review the résumé first."})
        emit("gate", pk=pk, detail="Résumé ready for review. Nothing submitted.")
        return {"result": "prepared", "pk": pk}

    # The run finished without gating. Only call it APPLIED if the browser agent
    # actually confirmed a submit — otherwise it FAILED (dead/404 posting, no form
    # found, or the apply step never ran), and we record WHY on the row.
    final = stores.tracking.get(pk) or {}
    outcome = _apply_outcome(apply_result)
    if outcome.get("status") == "applied":
        confirmation = outcome.get("confirmation") or "submitted"
        stores.tracking.set_status(pk, Status.APPLIED, confirmation_id=confirmation)
        _note_rotation_use(pk, stores)
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
