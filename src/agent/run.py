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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from google.adk.runners import Runner
from google.genai import types

from core.config import get_settings
from core.logging import get_logger
from core.models import Status
from core.stores import make_stores
from tools import submit_hold

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
    if url.startswith("sqlite+aiosqlite://"):
        from sqlalchemy import event

        service = DatabaseSessionService(db_url=url, connect_args={"timeout": 30})

        def _configure_sqlite(dbapi_connection, _connection_record):
            # Each pooled connection needs the longer wait before trying WAL,
            # or simultaneous ADK schema checks can fail with "database is locked".
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

        # ADK owns an async engine; configure its synchronous connect-event bridge
        # per service instead of sharing engine state between loops or threads.
        event.listen(service.db_engine.sync_engine, "connect", _configure_sqlite)
        return service
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


def _claim(pk: str, stores: Any) -> bool | None:
    """True = acquired, False = held, None = unsafe claim-store outage."""
    client = getattr(stores.tracking, "r", None)
    if client is None:  # cloud mode / a tracking backend without Redis
        return True
    try:
        return bool(client.set(f"lock:job:{pk}", "1", nx=True, ex=_LOCK_TTL_SECONDS))
    except Exception:  # noqa: BLE001 — a lock error is NOT an acquired claim
        log.warning("job lock unavailable for %s", pk, exc_info=True)
        return None


def _claimed(pk: str, stores: Any) -> bool:
    client = getattr(stores.tracking, "r", None)
    if client is None:
        return False
    try:
        return bool(client.exists(f"lock:job:{pk}"))
    except Exception:  # noqa: BLE001 — uncertainty must refuse recovery
        log.warning("job claim inspection unavailable for %s", pk, exc_info=True)
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


_ALREADY_RUNNING = ("An earlier run is still working on this job. Wait for it to "
                    "finish; if it never does, restart the daemon and it frees itself.")


_READER_FIELDS = ("jd_read_attempts", "jd_read_retry_at", "jd_read_revision",
                  "jd_text", "jd_read_prepare_only")


def _utc_now() -> datetime:
    """Replaceable clock for deterministic UTC reader deadlines."""
    return datetime.now(UTC)


def _reader_witness(row: dict) -> dict:
    # Omission means absence in BOTH backends, not a wildcard/None.
    return {field: row[field] for field in _READER_FIELDS if field in row}


def _reader_eligible(row: dict, *, now: datetime | None = None) -> bool:
    """Persisted FOUND admission; used independently at every entrypoint."""
    if row.get("status") != "found":
        return False
    try:
        if int(row.get("jd_read_attempts") or 0) >= 3:
            return False
        deadline = row.get("jd_read_retry_at")
        if not deadline:
            return True
        cutoff = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
        if cutoff.tzinfo is None:
            return False
        return (now or _utc_now()) >= cutoff
    except (TypeError, ValueError, OverflowError):
        # Corrupt deadline/budget is not authorization to retry immediately.
        return False



class JDURLUnresolvable(ValueError):
    """The stored historic relative posting lacks safe current source authority."""


def _historical_relative(row: dict) -> bool:
    """Classify only the original persisted destination, never a reader projection."""
    from urllib.parse import urlsplit

    raw = row.get("jd_url")
    # An absent legacy field is not a persisted historical relative address;
    # preserve the pre-existing no-URL workflows and their separate guards.
    if not isinstance(raw, str) or not raw:
        return False
    try:
        parsed = urlsplit(raw)
        return parsed.scheme not in ("http", "https") or not parsed.netloc
    except ValueError:
        return True


def _reader_url(row: dict) -> str:
    """Source-bound operational read target; never write it back to a row."""
    from urllib.parse import unquote, urljoin, urlsplit

    from discovery.funding import load_effective_watchlist
    from tools.jd import _valid_reader_url

    raw = row.get("jd_url")
    if not _historical_relative(row):
        return raw
    if (not isinstance(raw, str) or not raw
            or raw.startswith("//") or any(
                c.isspace() or ord(c) < 32 or ord(c) == 127 or c == "\\"
                for c in raw)):
        raise JDURLUnresolvable("Original relative posting URL is unsafe")
    try:
        parts = urlsplit(raw)
        if (parts.scheme or parts.netloc or parts.fragment or not parts.path
                or any(unquote(seg) in (".", "..") or any(
                    x in unquote(seg) for x in ("/", "\\", "%"))
                    or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127
                           for ch in unquote(seg))
                    for seg in parts.path.split("/") if seg)):
            raise JDURLUnresolvable("Original relative posting path is unsafe")
        name = row.get("company")
        ats = row.get("ats")
        if not isinstance(name, str) or not name.strip() or ats not in ("custom", "workday"):
            raise JDURLUnresolvable("Missing exact historical source classification")
        cfg = Path(get_settings().config_dir) / "watchlist.yaml"
        current_watchlist = load_effective_watchlist(cfg)
        matches = [c for c in current_watchlist
                   if c.name.strip().casefold() == name.strip().casefold()
                   and c.ats == ats]
        # Another company entry with the same name but different source is
        # ambiguous too; it must not be used as a source-specific fallback.
        all_names = [c for c in current_watchlist
                     if c.name.strip().casefold() == name.strip().casefold()]
        if len(matches) != 1 or len(all_names) != 1:
            raise JDURLUnresolvable("Current watchlist source is missing or ambiguous")
        company = matches[0]
        careers = company.careers_url
        if not _valid_reader_url(careers):
            raise JDURLUnresolvable("Current careers URL is unsafe")
        base = urlsplit(careers)
        if base.query or base.fragment:
            raise JDURLUnresolvable("Current careers URL is not an origin base")
        if ats == "workday":
            from discovery.adapters.workday import _CXS_PATH, _HOST, _safe_segment

            board_raw = company.board
            if (not board_raw or not _valid_reader_url(board_raw)
                    or any(c.isspace() for c in board_raw)):
                raise JDURLUnresolvable("Explicit Workday board is missing")
            board = urlsplit(board_raw)
            match = _CXS_PATH.fullmatch(board.path)
            if (board.scheme != "https" or base.scheme != "https"
                    or _HOST.fullmatch(board.netloc) is None
                    or board.query or board.fragment or match is None
                    or not all(_safe_segment(seg) for seg in match.groups())
                    or board.hostname != base.hostname or board.port != base.port):
                raise JDURLUnresolvable("Workday careers and CXS board disagree")
            site = match.group(2)
            wrapper = [seg for seg in base.path.split("/") if seg]
            # Workday's public wrapper may have a single locale component.
            if (not wrapper or wrapper[-1] != site or len(wrapper) > 2
                    or (len(wrapper) == 2 and not re.fullmatch(
                        r"[A-Za-z]{2}(?:-[A-Za-z]{2})?", wrapper[0]))):
                raise JDURLUnresolvable("Workday careers wrapper does not match site")
            segments = parts.path.split("/")
            if (len(segments) != 4 or segments[1] != "job"
                    or not all(_safe_segment(seg) for seg in segments[2:])
                    or segments[-1] != row.get("job_id") or parts.query):
                raise JDURLUnresolvable("Workday posting identity does not match")
            return f"https://{board.netloc}/{site}{parts.path}"
        target = urljoin(careers, raw)
        resolved = urlsplit(target)
        if (not _valid_reader_url(target)
                or resolved.hostname != base.hostname
                or resolved.scheme != base.scheme
                or resolved.port != base.port
                or (raw.startswith("/") and resolved.path != parts.path)
                or (not raw.startswith("/") and not resolved.path.endswith(
                    "/" + parts.path))
                or resolved.query != parts.query):
            raise JDURLUnresolvable("Custom posting is not on the current careers origin")
        return target
    except (ValueError, OSError, TypeError) as exc:
        if isinstance(exc, JDURLUnresolvable):
            raise
        raise JDURLUnresolvable("Current posting source cannot be verified") from exc


def _relative_attention(pk: str, stores: Any, *,
                        observed: dict | None = None) -> dict:
    """Review-only settlement under the original reader witness, not a fresh one."""
    reason = "posting_guard_review_required"
    detail = "Historical posting source requires manual same-job verification; no application queued."
    try:
        current = stores.tracking.get(pk)
        if not current:
            return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}
        expected = observed if observed is not None else current
        check = _reader_check(pk, expected, stores)
        if check:
            return check
        if current.get("status") == "tailoring":
            ok, _ = _reader_patch(pk, expected, stores, {
                "status": Status.NEEDS_HUMAN, "gate_reason": reason,
                "fail_kind": "source_html_unavailable", "error": detail,
            })
            if not ok:
                return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}
    except Exception as exc:
        return _reader_storage_error(pk, stores, current if 'current' in locals() else None, exc)
    return {"result": "gated", "pk": pk, "reason": reason, "detail": detail}


def _reader_patch(pk: str, row: dict, stores: Any, changes: dict) -> tuple[bool, dict]:
    """One conditional partial commit against the same observed generation."""
    patch = dict(changes)
    patch["jd_read_revision"] = int(row.get("jd_read_revision") or 0) + 1
    ok = stores.tracking.update_if_status(
        pk, row.get("status"), patch, expected_reader=_reader_witness(row))
    return ok, {**row, **{key: getattr(value, "value", value) if key == "status" else value
                         for key, value in patch.items()}}


def _reader_check(pk: str, observed: dict, stores: Any) -> dict | None:
    """Independent fresh confirmation/hold gate plus status/reader comparison.

    This does not claim a transaction with the separate durable hold key.
    Re-check immediately before preparation and recovery enqueue as well.
    """
    current = stores.tracking.get(pk)
    if (not current or current.get("status") != observed.get("status")
            or any(current.get(field) != observed.get(field)
                   for field in ("pk", "company", "job_id", "ats", "jd_url"))
            or _reader_witness(current) != _reader_witness(observed)):
        return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}
    if current.get("confirmation_id") or submit_hold.blocked(
            pk, current, tracking=stores.tracking):
        return {"result": "refused", "pk": pk, "reason": "submission_protected"}
    return None


def _reader_storage_error(pk: str, stores: Any, row: dict | None,
                          exc: Exception) -> dict:
    """Contain reader-storage faults before either daemon generic ERROR catch.

    At most one conditional attention attempt with the failed operation's
    ORIGINAL witness. Never re-read/rebase a completed or terminal outcome.
    """
    log.error("JD tracking storage error for %s: %s", pk, exc, exc_info=True)
    detail = ("Reader/tracking storage unavailable; restore storage, then use "
              "Retry for recorded attention or existing orphan recovery. "
              f"Cause: {type(exc).__name__}: {exc}")
    persisted = False
    if row and row.get("status") == "tailoring":
        try:
            persisted, _ = _reader_patch(pk, row, stores, {
                "status": Status.ERROR, "jd_read_error": "",
                "fail_kind": "jd_tracking_storage_error",
                "error": ("Posting reader tracking storage failed; nothing was prepared "
                          "or sent. Restore storage and use Retry.")})
        except Exception:
            log.exception("JD tracking attention could not be persisted for %s", pk)
    if not persisted:
        detail += " Attention could not be persisted; restore storage before safe recovery."
    return {"result": "error", "pk": pk, "reason": "jd_tracking_storage_error",
            "detail": detail, "attention_persisted": persisted}


def _retry_session_error(pk: str, stores: Any, admitted: dict,
                         exc: Exception, *, phase: str) -> dict:
    """Finish an admitted review-only Retry when its ADK session is unavailable.

    Only one commit is attempted against the invocation's existing witness:
    neither a newer manual result nor a newer reader generation can be rebased.
    """
    log.error("Retry session %s failed for %s: %s", phase, pk, exc, exc_info=True)
    detail = ("Retry could not reset or set up its preparation session. "
              "No new application was prepared or sent; restore the session "
              "service and use Retry.")
    try:
        committed, _ = _reader_patch(pk, admitted, stores, {
            "status": Status.ERROR, "fail_kind": "jd_retry_session_error",
            "jd_read_error": "", "error": detail})
    except Exception as storage_exc:
        log.exception("Could not persist Retry session attention for %s", pk)
        return {"result": "error", "pk": pk,
                "reason": "jd_tracking_storage_error",
                "detail": (detail + " Attention could not be persisted; restore "
                           "tracking storage before recovery. Cause: "
                           f"{type(storage_exc).__name__}: {storage_exc}"),
                "attention_persisted": False}
    if not committed:
        return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}
    from core.events import emit

    emit("error", pk=pk, url=admitted.get("jd_url"), detail=detail)
    return {"result": "error", "pk": pk, "reason": "jd_retry_session_error",
            "detail": detail, "attention_persisted": True}


def run_job(pk: str, stores: Any = None, *, prepare_only: bool = False) -> dict:
    """Conditionally admit a FOUND row, then settle only this reader generation."""
    stores = stores or make_stores()
    try:
        row = stores.tracking.get(pk)
    except Exception as exc:
        return _reader_storage_error(pk, stores, None, exc)
    if row is None:
        return {"result": "missing", "pk": pk}
    if not _reader_eligible(row):
        return {"result": "already_done", "pk": pk, "status": row.get("status"),
                "reason": "jd_read_not_eligible"}
    # Keep the existing uncertain-submission response contract. A hold is
    # checked independently of reader generation and is never cleared.
    if submit_hold.blocked(pk, row, tracking=stores.tracking):
        return {"result": "failed", "pk": pk, "reason": "uncertain",
                "detail": submit_hold.REASON}
    try:
        check = _reader_check(pk, row, stores)
    except Exception as exc:
        return _reader_storage_error(pk, stores, row, exc)
    if check:
        return check
    prepare_only = bool(prepare_only or row.get("jd_read_prepare_only")
                        or row.get("discovery_source") == "career_ops")
    claim = _claim(pk, stores)
    if claim is None:
        return {"result": "error", "pk": pk, "reason": "jd_tracking_storage_error",
                "detail": "Evaluation claim unavailable; no reader admission occurred."}
    if not claim:
        from core.events import emit
        emit("response", pk=pk, url=row.get("jd_url"), detail=_ALREADY_RUNNING)
        return {"result": "already_running", "pk": pk}
    try:
        try:
            check = _reader_check(pk, row, stores)
            if check:
                return check
            committed, admitted = _reader_patch(
                pk, row, stores, {"status": Status.TAILORING})
        except Exception as exc:
            return _reader_storage_error(pk, stores, row, exc)
        if not committed:
            return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}

        from core.events import emit
        emit("running", pk=pk, detail=f"{row.get('title','')} @ {row.get('company','')}",
             url=row.get("jd_url"))
        try:
            if admitted.get("rescore_requested"):
                _run(_reset_session(pk, strict=True))
            result = _run(_run_job_async(pk, admitted, stores, prepare_only=prepare_only))
        except Exception as exc:
            from .graph import ScorerModelError
            # Preserve the distinct scorer fault behavior; this is not a
            # reader-storage exception and is NOT masked by reader containment.
            detail = (("Scorer model/request failed; no valid score was received. "
                       "Check the error log, then Reopen & re-score.")
                      if isinstance(exc, ScorerModelError)
                      else "Preparation failed — see Logs and retry.")
            committed, _ = _reader_patch(pk, admitted, stores, {
                "status": Status.ERROR, "match_score": None, "error": detail})
            if committed:
                emit("error", pk=pk, detail=detail, url=row.get("jd_url"))
            raise
        # Existing explicit Apply request is NOT created by prepare-only Retry.
        # Re-fetch within the guarded enqueue path; never trust this old row.
        if (result or {}).get("result") == "prepared":
            try:
                current = stores.tracking.get(pk) or {}
                if (current.get("apply_requested_at") and not _historical_relative(current)
                        and not _board_preparation(current)):
                    # Request authority is checked AGAIN inside the queue entry.
                    # The first GET can race a cancellation in any prepare-only
                    # mode, not just a JD-attention recovery.
                    return _enqueue_apply(pk, stores, priority=True)
            except Exception as exc:
                return _reader_storage_error(pk, stores, admitted, exc)
        return result
    finally:
        _release(pk, stores)


async def _run_job_async(pk: str, row: dict, stores: Any, *,
                         prepare_only: bool = False,
                         guard_recovery: dict | None = None) -> dict:
    """Settle the actual admitted TAILORING generation, never an old FOUND row.

    `guard_recovery` is the refused `tailored` row of an explicit posting-guard
    Retry: the JD is reread from the matched source, preparation is review-only
    and nothing is enqueued.
    """
    from tools.jd import PostingReadUnavailable
    # Keep the caller's failure witness current only after our own successful
    # commits. A late exception must never rebase onto a row changed by Skip.
    admitted = row

    # Original relative URLs can only be processed by the review agent, even
    # after a verified operational read and an earlier application request.
    prepare_only = bool(prepare_only or _historical_relative(row)
                        or guard_recovery is not None or _board_preparation(row))
    if _account_portal(row.get("jd_url", "")):
        return _preparation_skip(pk, row, stores, "account portal")
    try:
        jd_meta = await (_jd_meta(row, fresh=True) if guard_recovery is not None
                         else _jd_meta(row))
        jd_text = jd_meta["text"]
    except JDURLUnresolvable as exc:
        if guard_recovery is not None:
            return _guard_recovery_refused(
                pk, row, guard_recovery, stores, "posting_source_unavailable",
                f"Current posting source cannot be resolved: {exc}")
        detail = ("Historical posting URL cannot be resolved against a unique safe "
                  "current source. Verify the source manually; nothing was prepared.")
        try:
            check = _reader_check(pk, row, stores)
            if check:
                return check
            committed, _ = _reader_patch(pk, row, stores, {
                "status": Status.ERROR, "fail_kind": "jd_url_unresolvable",
                "error": detail, "jd_read_error": "", "jd_read_retry_at": None,
            })
        except Exception as exc:
            return _reader_storage_error(pk, stores, row, exc)
        if not committed:
            return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}
        from core.events import emit

        emit("error", pk=pk, detail=detail, url=row.get("jd_url"))
        return {"result": "error", "pk": pk, "reason": "jd_url_unresolvable",
                "detail": detail}
    except PostingReadUnavailable as exc:
        if guard_recovery is not None:
            return _guard_recovery_refused(
                pk, row, guard_recovery, stores, "posting_source_unavailable",
                f"No complete matching current posting source: {exc}")
        try:
            check = _reader_check(pk, row, stores)
            if check:
                return check
            attempts = int(row.get("jd_read_attempts") or 0) + 1
            if attempts >= 3:
                patch = {
                    "status": Status.ERROR,
                    "jd_read_attempts": 3, "jd_read_retry_at": None,
                    "jd_read_error": "", "fail_kind": "jd_reader_exhausted",
                    "error": ("Posting reader exhausted three attempts; this read attempt "
                              "did not prepare or send an application. Restore reader "
                              "availability and use Retry."),
                }
            else:
                cutoff = _utc_now() + timedelta(seconds=60 if attempts == 1 else 300)
                cutoff_text = cutoff.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
                patch = {
                    "status": Status.FOUND, "jd_read_attempts": attempts,
                    "jd_read_retry_at": cutoff.isoformat(),
                    "jd_read_error": (
                        f"Posting reader unavailable: {exc}. Attempt {attempts} of 3; "
                        f"next eligible not before {cutoff_text}. "
                        "This read attempt did not prepare or send an application."),
                    "fail_kind": "", "fail_reason": "", "error": "",
                }
            committed, _ = _reader_patch(pk, row, stores, patch)
        except Exception as fault:
            return _reader_storage_error(pk, stores, row, fault)
        if not committed:
            return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}
        from core.events import emit

        if attempts >= 3:
            detail = patch["error"]
            emit("error", pk=pk, detail=detail, url=row.get("jd_url"))
            return {"result": "error", "pk": pk, "reason": "jd_reader_exhausted",
                    "detail": detail}
        detail = patch["jd_read_error"]
        emit("response", pk=pk, detail=detail, url=row.get("jd_url"))
        log.warning("posting read deferred for pk=%s: %s", pk, detail)
        return {"result": "deferred", "pk": pk, "reason": "jd_reader_unavailable",
                "detail": detail}

    # A current board URL, HTTP 200, or guessed effective address cannot
    # certify the historic original posting. Without an independent same-job
    # HTML witness, leave the existing source guard and stop before preparation.
    if _historical_relative(row):
        from tools.jd import matched_source_witness

        if not matched_source_witness(row, jd_meta):
            return _relative_attention(pk, stores, observed=row)

    # This commit is required even on an initially metadata-free row. A later
    # FAILED exit must use its NEW witness, not the pre-read TAILORING witness.
    try:
        check = _reader_check(pk, row, stores)
        if check:
            return check
        if guard_recovery is not None:
            problem = _guard_recovery_generation_problem(
                guard_recovery, row, jd_meta.get("posting_guard"))
            if problem:
                return _guard_recovery_refused(
                    pk, row, guard_recovery, stores, *problem)
            # Invalidate the old request, gate answers and PDF pointers BEFORE
            # the fresh JD/guard is published: a conflict on this conditional
            # write publishes nothing, and no later conflict can leave the fresh
            # posting beside the old PDF or request. The refusal marker stays
            # until preparation, so a later refusal still restores a closed row.
            cleared, row = _reader_patch(pk, row, stores, _STALE_POSTING_AUTHORITY)
            if not cleared:
                return {"result": "conflict", "pk": pk,
                        "reason": "jd_reader_state_changed"}
        committed_guard = _commit_posting_meta(pk, row, jd_meta, stores)
        if committed_guard is None:
            return {"result": "conflict", "pk": pk, "reason": "posting_guard_state_changed"}
        row = committed_guard
        admitted.update(row)
        if guard_recovery is not None:
            problem = _guard_recovery_generation_problem(
                guard_recovery, row, row.get("posting_guard"))
            if problem:
                return _guard_recovery_refused(
                    pk, row, guard_recovery, stores, *problem)
        committed, settled = _reader_patch(pk, row, stores, {
            "rescore_requested": False,
            "jd_read_attempts": 0,
            "jd_read_retry_at": None, "jd_read_error": "",
            # The refusal itself is settled only once the new generation is bound.
            **(_REFUSAL_MARKER_CLEARED if guard_recovery is not None else {}),
        })
    except Exception as exc:
        return _reader_storage_error(pk, stores, row, exc)
    if not committed:
        return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}
    row = settled
    admitted.update(row)
    jd_text = row.get("jd_text") or ""

    # Distinguish a known infrastructure outage from a successfully returned
    # but unreadable/unsponsored page. Both content-derived terminal exits use
    # the same guarded precondition, including the second post-metadata race.
    if _unreadable(jd_text):
        why = ("Could not read the posting — the page returned an error or almost "
               "no text. Nothing was tailored. Retry once the site is reachable.")
        try:
            check = _reader_check(pk, row, stores)
            if check:
                return check
            committed, _ = _reader_patch(pk, row, stores, {
                "status": Status.FAILED, "fail_reason": why, "fail_kind": "no_jd"})
        except Exception as exc:
            return _reader_storage_error(pk, stores, row, exc)
        if not committed:
            return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}
        from core.events import emit
        emit("failed", pk=pk, detail="posting unreadable — not tailored",
             url=row.get("jd_url"))
        return {"result": "failed", "pk": pk, "reason": "no_jd"}

    from core import steering
    conflict = _eligibility_conflict(
        jd_text, row.get("location") or "",
        _effective_prefs(row.get("company", "")), steering.read()["content"])
    if conflict:
        return _preparation_skip(pk, row, stores, conflict)

    try:
        check = _reader_check(pk, row, stores)
        if check:
            return check
    except Exception as exc:
        return _reader_storage_error(pk, stores, row, exc)

    state = _session_state(row, jd_text)
    try:
        sessions = _session_service()
        # create_session is async; a retry may find it already there.
        existing = await sessions.get_session(app_name=_APP, user_id=_USER, session_id=pk)
        if existing is not None and (
                _stale_seed(existing, state["base_latex"])
                or _stale_posting(existing, row, jd_text)):
            # A session seeded from an earlier résumé cannot be reused against
            # a newer base. Reset and rebuild it, preserving the seed invariant.
            await _reset_session(pk)
            existing = None
            log.info("base résumé changed since %s was first seen — starting it fresh", pk)
        if existing is None:
            await sessions.create_session(
                app_name=_APP, user_id=_USER, session_id=pk, state=state)
    except Exception as exc:
        if row.get("jd_read_prepare_only"):
            return _retry_session_error(pk, stores, row, exc, phase="setup")
        raise
    # Session lookup/create can itself take time. Check the independent hold
    # key and confirmation again immediately before ADK preparation.
    try:
        check = _reader_check(pk, row, stores)
        if check:
            return check
    except Exception as exc:
        return _reader_storage_error(pk, stores, row, exc)
    from .graph import review_agent
    runner = Runner(agent=review_agent if prepare_only else root_agent,
                    app_name=_APP, session_service=sessions)

    msg = types.Content(role="user", parts=[types.Part(
        text=f"Apply to this job: {__import__('tools.posting_guard', fromlist=['analyze']).analyze(row.get('title',''))['text']} at {row.get('company','')}. "
             f"URL: {row.get('jd_url','')}")])
    from .graph import ScorerInvalidOutput

    try:
        extra = {"no_enqueue": True} if guard_recovery is not None or _board_preparation(row) else {}
        result = await _drive_async(runner, pk, msg, stores,
                                    prepare_only=prepare_only, observed=row, **extra)
    except ScorerInvalidOutput:
        # No new diagnostics or artifacts: a stale score/PDF must not appear
        # as evidence of this evaluation or enable a downstream approval.
        from core.events import emit

        detail = ("The scorer did not return a valid score after one correction. "
                  "Nothing was tailored or queued; use Reopen & re-score to retry.")
        committed, _ = _reader_patch(pk, row, stores, {
            "status": Status.SKIPPED, "skip_reason": "scorer_invalid_output",
            "match_score": None, "fail_reason": detail})
        if not committed:
            return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}
        emit("skipped", pk=pk, detail=detail, url=row.get("jd_url"))
        return {"result": "skipped", "pk": pk, "reason": "scorer_invalid_output"}

    # The writer gets the current validated score, never the pre-run row's
    # historical score (which could describe a different evaluation).
    current = stores.tracking.get(pk) or {}
    output = {**row, **current}
    if result.get("result") not in ("prepared", "gated", "queued_apply"):
        output.update(resume_tex_key="", resume_s3_key="")
    _save_output(pk, output, jd_text, stores)
    return result


def _guard_recovery_generation_problem(
        original: dict, row: dict, guard: dict | None) -> tuple[str, str] | None:
    """A Retry that cannot show a NEW generation must not claim to have one."""
    generation = (guard or {}).get("generation")
    if not generation:
        return ("posting_guard_invalid",
                "The current posting has no valid guard generation.")
    if generation == (original.get("posting_guard") or {}).get("generation"):
        return ("posting_guard_generation_unchanged",
                "The current posting has the same guard generation as the refused "
                "one; there is nothing new to prepare. Review the posting instead.")
    if generation == row.get("reviewed_guard_generation"):
        return ("posting_guard_review_inherited",
                "A previous review already covers the current guard generation; "
                "a fresh owner decision cannot be established. Nothing was prepared.")
    return None


def _guard_recovery_refused(pk: str, row: dict, original: dict, stores: Any,
                            reason: str, detail: str) -> dict:
    """Return an admitted guard Retry to its refused `tailored` state.

    Admission changed only status and review-only intent, so one conditional
    write against the admitted generation restores the refusal unchanged.
    """
    try:
        check = _reader_check(pk, row, stores)
        if check:
            return check
        committed, _ = _reader_patch(pk, row, stores, {
            "status": Status.TAILORED,
            "jd_read_prepare_only": bool(original.get("jd_read_prepare_only")),
        })
    except Exception as exc:
        return _reader_storage_error(pk, stores, row, exc)
    if not committed:
        return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}
    from core.events import emit
    emit("response", pk=pk, url=row.get("jd_url"), detail=detail)
    return {"result": "refused", "pk": pk, "reason": reason, "detail": detail}


# Cleared only after the current posting was re-read and shown to have a NEW
# guard generation: the old request, gate answers and old artifacts describe the
# refused posting. `reviewed_guard_generation` cannot be written by the
# conditional writer; the new-generation check covers it.
_STALE_POSTING_AUTHORITY = {
    "gate_pending": None, "gate_call_id": None, "apply_requested_at": None,
    "resume_s3_key": "", "resume_tex_key": "",
}
# Cleared in the same write that finishes binding the new generation.
_REFUSAL_MARKER_CLEARED = {
    "fail_kind": "", "fail_reason": "", "skip_reason": "", "error": "",
}


async def _jd_meta(row: dict, *, yielding: bool = True,
                   approval: bool = False, fresh: bool = False) -> dict:
    """Guarded JD read with separate operational URL and original source proof.

    `fresh` (explicit posting-guard Retry) always rereads the matched source,
    whatever the cached length, and never falls back to cached text.
    """
    from tools import jd as _jd, posting_guard as pg

    # Resolve before even considering cached text or opening Chrome/ATS/HTTP.
    url = _reader_url(row)
    historical = _historical_relative(row)
    if approval or fresh:
        if historical or not _jd._valid_reader_url(url):
            raise JDURLUnresolvable("Historical relative posting is not an application URL")
        remote = await asyncio.to_thread(_jd.fetch_jd_meta, url, "jd")
        if (not isinstance(remote, dict)
                or not isinstance(remote.get("text"), str)
                or not pg.valid(remote["text"], remote.get("posting_guard"))
                or not _jd.matched_source_witness(row, remote)):
            raise _jd.PostingReadUnavailable("No complete matching posting source")
        if approval:
            return remote
        # Findings already recorded for this posting are never dropped.
        captured = row.get("jd_text") or ""
        old = row.get("posting_guard")
        if not pg.valid(captured, old):
            old = pg.projection(captured, old)["posting_guard"]
        combined = pg.merge(remote["text"], remote["posting_guard"], old,
                            source_witness=True)
        return {**combined, "source_kind": remote.get("source_kind", ""),
                "source_url": remote.get("source_url", ""),
                "reader_effective_url": "", "source_identity": None}

    captured = row.get("jd_text") or ""
    old = row.get("posting_guard")
    if not pg.valid(captured, old):
        old = pg.projection(captured, old)["posting_guard"]
    if historical:
        # Previously cached content is not a source witness for an original
        # relative posting, irrespective of its length or old guard projection.
        old = pg.analyze(captured, previous=old,
                         source_required=True)["posting_guard"]
    local = pg.analyze(captured, previous=old,
                       source_required="source_html_unavailable" in old.get("reasons", []))
    local["source_kind"] = ""
    need_source = "source_html_unavailable" in old.get("reasons", [])
    if not url or (len(captured) >= 400 and not need_source):
        return local
    try:
        remote = await asyncio.to_thread(
            _jd.fetch_jd_meta, url, "jd_sweep" if yielding else "jd")
    except _jd.PostingReadUnavailable:
        if historical or _unreadable(local["text"]):
            raise
        return local
    if not isinstance(remote, dict):
        return local
    rtext, guard = remote.get("text"), remote.get("posting_guard")
    if not isinstance(rtext, str) or not pg.valid(rtext, guard):
        return local
    if historical:
        # This tag is transient and is matched to independently parsed HTML
        # canonical + JobPosting identifier, never projected into persisted jd_url.
        remote = {**remote, "reader_effective_url": url}
    source_witness = _jd.matched_source_witness(row, remote)
    use_remote = bool(rtext) and (
        len(rtext) > len(local["text"]) or (need_source and source_witness))
    selected = (rtext.strip() if not need_source else rtext) if use_remote else local["text"]
    combined = pg.merge(selected, guard, old, source_witness=source_witness)
    return {**combined,
            "source_kind": remote.get("source_kind", "") if source_witness else "",
            "source_url": remote.get("source_url", "") if source_witness else "",
            "reader_effective_url": url if historical else "",
            "source_identity": remote.get("source_identity") if historical else None}


def _commit_posting_meta(pk: str, observed: dict, meta: dict,
                         stores: Any) -> dict | None:
    """Commit guarded JD and source provenance against the observed posting."""
    from core.storage.base import guard_observation_matches
    from tools import posting_guard as pg

    current = stores.tracking.get_current(pk)
    if (not current or current.get("pk") != pk
            or current.get("status") != observed.get("status")
            or current.get("jd_url") != observed.get("jd_url")
            or _reader_witness(current) != _reader_witness(observed)
            or not pg.valid(meta.get("text"), meta.get("posting_guard"))):
        return None
    # Direct approval must carry the frozen pre-read observation through the
    # guarded write. Other JD read paths retain their own reader generation CAS.
    direct = observed.get("status") == Status.SUBMITTING.value
    if direct and (not guard_observation_matches(current, observed)
                   or submit_hold.blocked(pk, current, tracking=stores.tracking)):
        return None
    from tools import jd as _jd

    source_witness = _jd.matched_source_witness(
        observed if direct else current, meta)
    title_seen = pg.analyze(current.get("title") or "")
    if title_seen["posting_guard"]["reasons"]:
        meta = {**meta, **pg.merge(meta["text"],
                                   title_seen["posting_guard"],
                                   meta["posting_guard"],
                                   source_witness=source_witness)}
    if direct:
        old = current.get("posting_guard")
        if not pg.valid(current.get("jd_text") or "", old):
            old = pg.projection(current.get("jd_text") or "", old)["posting_guard"]
        expected = pg.merge(meta["text"], meta["posting_guard"], old,
                            source_witness=source_witness)
    if not stores.tracking.update_posting_guard(
            pk, observed if direct else current, meta,
            source_witness=source_witness):
        return None
    updated = stores.tracking.get_current(pk)
    if (not updated or updated.get("pk") != pk
            or updated.get("status") != observed.get("status")
            or not pg.valid(updated.get("jd_text") or "",
                            updated.get("posting_guard"))):
        return None
    if direct:
        expected_row = {**observed, "jd_text": expected["text"],
                        "posting_guard": expected["posting_guard"]}
        if (not guard_observation_matches(updated, expected_row)
                or _reader_witness(updated) != _reader_witness(expected_row)
                or submit_hold.blocked(pk, updated, tracking=stores.tracking)):
            return None
    return updated

def _guard_refusal(pk: str, stores: Any, *, observed: dict | None = None,
                   detail: str | None = None) -> dict:
    """Conditionally undo a *pre-browser* SUBMITTING claim without lost flags."""
    from tools import posting_guard as pg

    reason = "posting_guard_review_required"
    try:
        current = stores.tracking.get_current(pk)
        if not current:
            return {"result": "conflict", "pk": pk,
                    "reason": "posting_guard_state_changed"}
        if (submit_hold.blocked(pk, current, tracking=stores.tracking)
                or current.get("possible_submission")
                or current.get("confirmation_id")
                or current.get("gate_reason") == "submit_uncertain"):
            return {"result": "failed", "pk": pk, "reason": "uncertain",
                    "detail": submit_hold.REASON}
        if observed is not None:
            from core.storage.base import guard_observation_matches

            if (not guard_observation_matches(current, observed)
                    or _reader_witness(current) != _reader_witness(observed)):
                return {"result": "conflict", "pk": pk,
                        "reason": "posting_guard_state_changed"}
        if current.get("status") == "submitting":
            ok = stores.tracking.update_posting_guard(
                pk, current, settle_refusal=True)
            if not ok:
                # A failed conditional write is not evidence that the gate
                # settled. Check for a newly created submission hold first.
                fresh = stores.tracking.get_current(pk)
                if (fresh and (submit_hold.blocked(pk, fresh, tracking=stores.tracking)
                               or fresh.get("possible_submission")
                               or fresh.get("confirmation_id")
                               or fresh.get("gate_reason") == "submit_uncertain")):
                    return {"result": "failed", "pk": pk, "reason": "uncertain",
                            "detail": submit_hold.REASON}
                if fresh and fresh.get("status") in ("applied", "applied_manual"):
                    return {"result": "duplicate", "pk": pk,
                            "reason": "already_applied"}
                log.warning("posting guard refusal settlement raced for %s", pk)
                return {"result": "conflict", "pk": pk,
                        "reason": "posting_guard_state_changed"}
        elif current.get("status") in ("applied", "applied_manual"):
            return {"result": "duplicate", "pk": pk, "reason": "already_applied"}
        elif (current.get("status") not in ("tailored", "needs_human")
              or current.get("fail_kind") != reason):
            return {"result": "conflict", "pk": pk,
                    "reason": "posting_guard_state_changed"}
    except Exception:
        log.exception("posting guard refusal failed to persist for %s", pk)
        return {"result": "conflict", "pk": pk,
                "reason": "posting_guard_state_changed"}
    return {"result": "gated", "pk": pk, "reason": reason,
            "detail": detail or "Current posting generation requires job-specific review."}


def _direct_approval_check(pk: str, expected: dict,
                           stores: Any) -> tuple[dict | None, dict | None]:
    """A row/hold check tied to this approval's immutable observation."""
    from core.storage.base import guard_observation_matches

    current = stores.tracking.get_current(pk)
    if not current:
        return {"result": "conflict", "pk": pk,
                "reason": "posting_guard_state_changed"}, None
    if (submit_hold.blocked(pk, current, tracking=stores.tracking)
            or current.get("confirmation_id")):
        return {"result": "failed", "pk": pk, "reason": "uncertain",
                "detail": submit_hold.REASON}, current
    if current.get("status") in ("applied", "applied_manual"):
        return {"result": "duplicate", "pk": pk,
                "reason": "already_applied"}, current
    if (not guard_observation_matches(current, expected)
            or _reader_witness(current) != _reader_witness(expected)):
        return {"result": "conflict", "pk": pk,
                "reason": "posting_guard_state_changed"}, current
    return None, current


def _approval_legacy_evidence(row: dict) -> str | None:
    """Invalid legacy reasons must not disappear through guard projection."""
    from tools import posting_guard as pg

    old = row.get("posting_guard")
    reasons = old.get("reasons") if isinstance(old, dict) else None
    if not pg.valid(row.get("jd_text") or "", old):
        # Only a well-formed, recognized reason list is safe to reconsider
        # against a complete matched reread. Sanitized JD can lose HTML evidence.
        known = pg._POSITIVE | {"source_html_unavailable"}
        if (not isinstance(reasons, list)
                or any(type(reason) is not str or reason not in known
                       for reason in reasons)
                or reasons != sorted(set(reasons))):
            return "unrecognized"
    if isinstance(reasons, list) and any(reason in pg._POSITIVE
                                         for reason in reasons):
        return "positive"
    return None

async def _jd_text(row: dict, *, yielding: bool = True) -> str:
    """Compatibility string reader; model ingress uses _jd_meta plus commit."""
    return (await _jd_meta(row, yielding=yielding))["text"]


def _browser_companies() -> set[str]:
    """Companies whose postings can only be read in a browser: marked so in the
    watchlist, or seen refusing a plain request during discovery."""
    from core import flags as _flags

    return _watchlist_browser_companies() | _flags.fetch_refused()


def _watchlist_browser_companies() -> set[str]:
    from pathlib import Path as _P

    from core.models import DiscoveryMode
    from discovery.funding import load_effective_watchlist

    try:
        cfg = _P(get_settings().config_dir) / "watchlist.yaml"
        return {c.name.strip().lower() for c in load_effective_watchlist(cfg)
                if c.discovery is DiscoveryMode.BROWSER}
    except Exception:  # noqa: BLE001 — no watchlist means no browser-only companies
        return set()


def prefetch_browser_jds(rows: list[dict], stores: Any) -> int:
    """Fill only persisted, presently eligible browser FOUND rows.

    Results are conditional against the pre-batch reader witness; neither a
    status-only check nor a fresh post-batch GET can license a stale batch.
    Missing results do not imply gone or consume a per-pk reader attempt.
    """
    from tools import jd as _jd

    browser = _browser_companies()
    need = []
    for supplied in rows:
        pk = supplied.get("pk")
        if not pk:
            continue
        try:
            row = stores.tracking.get(pk)
            if not row or not _reader_eligible(row) or not row.get("jd_url"):
                continue
            if ((row.get("company") or "").strip().lower() not in browser
                    or not _unreadable(row.get("jd_text") or "")):
                continue
            if row.get("confirmation_id") or submit_hold.blocked(
                    pk, row, tracking=stores.tracking):
                continue
            try:
                effective = _reader_url(row)
            except JDURLUnresolvable:
                # A browser echo cannot repair missing source authority.
                continue
            if not _jd._valid_reader_url(effective):
                continue
            need.append((row, effective))
        except Exception:
            log.exception("JD prefetch reader/tracking storage failed during selection for %s", pk)
    if not need:
        return 0
    log.info("reading %d browser-only posting(s) before the sweep", len(need))
    try:
        got, gone = _jd.read_postings([url for _, url in need], with_gone=True,
                                      with_meta=True)
    except _jd.PostingReadUnavailable as exc:
        log.warning("browser prefetch deferred: %s", exc)
        return 0
    filled = closed = 0
    for observed, url in need:
        pk = observed["pk"]
        if url not in gone and not got.get(url):
            continue
        if _historical_relative(observed):
            # Batch got/gone is keyed by the calculated URL, not independently
            # verified original same-job HTML. Neither result may write this row.
            continue
        try:
            check = _reader_check(pk, observed, stores)
            if check:
                continue
            if url in gone:
                patch = {"status": Status.JOB_GONE,
                         "fail_reason": "The posting has been removed."}
            else:
                from tools import posting_guard as pg

                result = got[url]
                meta = (result if isinstance(result, dict) else
                        pg.analyze(str(result), source_required=True,
                                   strip_trailing=True))
                updated = _commit_posting_meta(pk, observed, meta, stores)
                if updated is None:
                    continue
                observed = updated
                patch = {"jd_read_attempts": 0,
                         "jd_read_retry_at": None, "jd_read_error": ""}
            committed, _ = _reader_patch(pk, observed, stores, patch)
        except Exception as exc:
            # Do not let the caller's unconditional generic ERROR catch turn a
            # late storage fault into an overwrite of a human terminal result.
            _reader_storage_error(pk, stores, observed, exc)
            continue
        if committed:
            if url in gone:
                closed += 1
            else:
                filled += 1
    log.info("browser prefetch filled %d and closed %d of %d posting(s)",
             filled, closed, len(need))
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

def _account_portal(url: str) -> bool:
    """Bounded URL policy, not a claim that an authentication wall was seen."""
    from urllib.parse import urlsplit
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    if any(c.isspace() or c in "\\%" for c in url):
        return False
    try:
        p = urlsplit(url)
        if not p.hostname or p.netloc != p.hostname or p.fragment:
            return False
    except ValueError:
        return False
    host = p.hostname
    def vendor(*domains):
        return any(host == d or host.endswith("." + d) for d in domains)
    path = p.path.rstrip("/").lower()
    if vendor("myworkdaysite.com", "myworkdayjobs.com"):
        return bool(re.search(r"/(?:login|signin|account|candidatehome)(?:/|$)", path))
    if vendor("taleo.net"):
        return bool(re.fullmatch(r"/careersection/(?:[^/]+/)+(?:login|register|myjobpage)\.ftl", path))
    if vendor("icims.com"):
        return bool(re.fullmatch(r"/(?:login|profile|account)(?:/.*)?|/jobs/[^/]+/(?:login|account|profile)(?:/.*)?", path))
    if vendor("successfactors.com", "successfactors.eu"):
        return bool(re.fullmatch(r"/(?:login|account|candidateprofile|myprofile)(?:/.*)?", path))
    return False


def _preparation_skip(pk: str, row: dict, stores: Any, reason: str) -> dict:
    check = _reader_check(pk, row, stores)
    if check:
        return check
    ok, _ = _reader_patch(pk, row, stores, {
        "status": Status.SKIPPED, "skip_reason": reason,
        "gate_reason": "", "gate_pending": {}, "apply_requested_at": "",
    })
    if not ok:
        return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}
    return {"result": "skipped", "pk": pk, "reason": reason}


def _private_constraint(content: str, family: str) -> str | None:
    """Only complete explicit lines; one ambiguous/contradictory line is unknown.

    This deliberately small recognition grammar is not a general prose parser.
    Values remain runtime-only, and are never used in diagnostics.
    """
    # Prose mentions carry no typed authority and must not mask explicit lines.
    lines = [re.sub(r"^\s*(?:[-*+•]\s*)?", "", line).strip().casefold()
             for line in content.splitlines()]
    lines = [line for line in lines if line.startswith(family + ":")]
    if not lines:
        return None
    if family == "sponsorship":
        allowed = r"sponsorship: (required|not required)"
    elif family == "work authorization":
        allowed = r"work authorization: ((?:not authorized in|authorized only in) [a-z][a-z -]{1,60})"
    else:
        allowed = r"residence: ((?:only in|not resident in) [a-z][a-z -]{1,60})"
    values = set()
    for line in lines:
        match = re.fullmatch(allowed, line)
        if match is None or re.search(
                r"\b(?:maybe|unless|or|and|unknown|not sure|anywhere|worldwide)\b", match[1]):
            return None
        values.add(match[1])
    return next(iter(values)) if len(values) == 1 else None


def _remote_in_home_region(jd: str, location: str, steering: str) -> bool:
    """Only explicit remote-in-region evidence can waive a sponsorship stop."""
    from discovery.filters import _country, posting_country

    residence = _private_constraint(steering, "residence")
    if not residence or not residence.startswith("only in "):
        return False
    home = _country(residence.removeprefix("only in "))
    if not home:
        return False
    labels = [location] if location.strip().casefold() not in (
        "", "unknown", "unspecified", "not specified", "remote") else []
    labels.extend(re.findall(
        r"(?:^|[\n.!?;]\s*)(?:location|work location|work arrangement|worksite|workplace):"
        r"\s*([^\n.;!?]+)", jd, re.I))
    # A scoped remote sentence is evidence; an incidental mention of remote
    # tools or a country elsewhere in the posting is not.
    labels.extend(arrangement + " - " + region for arrangement, region in re.findall(
        r"(?:^|[.!?]\s*|\n)(?:this (?:role|position) is )?(?:fully )?"
        r"(remote|on-site|onsite|on site|hybrid) (?:in|within|from) ([^\n.;!?]+)", jd, re.I))
    # Bare Remote carries no geographic evidence and must not veto a scoped JD;
    # it cannot prove the home-region exception by itself either.
    labels = [label for label in labels if label.strip().casefold() != "remote"]
    if not labels or any(re.search(r"\b(?:on-site|onsite|on site|hybrid)\b", label, re.I)
                         for label in labels):
        return False
    return (any(re.search(r"\bremote\b", label, re.I) for label in labels)
            and all(posting_country(re.sub(r"\bremote (?:in|within|from)\b", "remote -",
                                          label, flags=re.I)) == home for label in labels))


def _eligibility_conflict(jd: str, location: str, prefs: Any, steering: str) -> str | None:
    """Prove both sides before scoring; raw fit/override cannot bypass this."""
    # A newline terminates a requirement even when a bullet has no punctuation.
    text = "\n".join(" ".join(line.casefold().split()) for line in jd.splitlines())
    sponsorship = _private_constraint(steering, "sponsorship")
    if (sponsorship == "required" and _no_sponsorship(jd)
            and not _remote_in_home_region(jd, location, steering)):
        return "eligibility conflict: sponsorship"
    for family, pattern in (
        ("work authorization", r"(?:must (?:already )?be (?:legally )?authorized to work|existing right to work (?:is )?required) in ([a-z][a-z -]{1,60})(?=[.;]|$)"),
        ("residence", r"(?:must (?:currently )?(?:reside|live) in|residence (?:is )?required in) ([a-z][a-z -]{1,60})(?=[.;]|$)"),
    ):
        constraint = _private_constraint(steering, family)
        if not constraint:
            continue
        for required in re.findall(pattern, text, re.M):
            required = required.strip()
            if re.search(r"\b(?:or|and|unless|maybe|anywhere|worldwide)\b", required):
                continue
            only = "authorized only in " if family == "work authorization" else "only in "
            negative = "not authorized in " if family == "work authorization" else "not resident in "
            if ((constraint.startswith(only) and constraint[len(only):] != required)
                    or constraint == negative + required):
                return "eligibility conflict: " + family
    from discovery.filters import location_conflict
    loc = location.strip().casefold()
    if loc in ("", "unknown", "unspecified", "not specified"):
        found = re.search(r"(?:^|\n)location: ([^\n.;]+)", jd, re.I)
        loc = found[1].strip().casefold() if found else loc
    reason = location_conflict(loc, prefs)
    return "eligibility conflict: " + reason if reason else None


def _board_preparation(row: dict) -> bool:
    return row.get("discovery_source") == "greenhouse_board_listing"



def _save_output(pk: str, row: dict, jd_text: str, stores: Any) -> None:
    """Drop the JD + tailored résumé into output/<stamp>_<company>_<id>/."""
    from tools.output import write_job_output

    tex = pdf = None
    try:
        tex = stores.artifacts.get(row["resume_tex_key"]).decode()
    except Exception:
        pass
    try:
        pdf = stores.artifacts.get(row["resume_s3_key"])
    except Exception:
        pass
    write_job_output(pk, company=row.get("company", ""), title=row.get("title", ""),
                     url=row.get("jd_url", ""), score=row.get("match_score"),
                     jd_text=jd_text, tex=tex, pdf=pdf)



def _enqueue_apply(pk: str, stores: Any, *, priority: bool = False,
                   require_request: bool = True, expected_row: dict | None = None) -> dict:
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
    # Submission evidence outranks an absent request. Otherwise a stale row
    # without approval conceals the durable hold from callers expecting the
    # established uncertain-result contract.
    if submit_hold.blocked(pk, row, tracking=stores.tracking):
        return {"result": "failed", "pk": pk, "reason": "uncertain",
                "detail": submit_hold.REASON}
    if _historical_relative(row):
        return {"result": "gated", "pk": pk, "reason": "posting_guard_review_required",
                "detail": "Historical relative posting requires manual source review."}
    if row.get("confirmation_id"):
        return {"result": "refused", "pk": pk, "reason": "already_confirmed"}
    if row.get("status") in ("applied", "applied_manual"):
        log.warning("refusing to queue %s: already %s", pk, row.get("status"))
        return {"result": "duplicate", "pk": pk, "reason": "already_applied"}
    if expected_row is not None:
        check = _reader_check(pk, expected_row, stores)
        if check:
            return check
        if any(row.get(key) != expected_row.get(key) for key in (
                "gate_call_id", "gate_pending", "gate_reason")):
            return {"result": "conflict", "pk": pk, "reason": "approval_withdrawn"}
    if not require_request and row.get("status") not in ("tailored", "needs_human"):
        return {"result": "refused", "pk": pk, "reason": "preparation_not_current"}
    if require_request and (
            not row.get("apply_requested_at")
            or row.get("status") not in ("tailoring", "tailored")):
        return {"result": "refused", "pk": pk,
                "reason": "apply_request_or_preparation_not_current"}

    q = ApplyQueue(stores.tracking.r)
    if require_request and pk in q.in_flight():
        return {"result": "refused", "pk": pk, "reason": "apply_in_flight"}
    # A role the owner pressed Apply on already has its go-ahead. Marking it
    # "approval" is what put it back in front of them as a question.
    requested = bool(row.get("apply_requested_at"))
    def withdraw_stale_preparation():
        latest = stores.tracking.get(pk) or {}
        # Preserve the existing held/committing-row recovery route; never claim
        # to cancel an application that may already have reached its form.
        if (latest.get("status") in ("tailoring", "tailored", "needs_human", "error")
                and not latest.get("confirmation_id")
                and not submit_hold.blocked(pk, latest, tracking=stores.tracking)):
            q.remove(pk)
    try:
        fresh = q.put(pk, row.get("company") or "", priority=priority, requested=requested)
        if not fresh:
            # put() also returns False for an independent durable hold; treating
            # every False as a duplicate used to overwrite APPLIED_MANUAL with
            # TAILORED after a concurrent human outcome.
            latest = stores.tracking.get(pk) or {}
            if submit_hold.blocked(pk, latest, tracking=stores.tracking):
                return {"result": "failed", "pk": pk, "reason": "uncertain",
                        "detail": submit_hold.REASON}
            if latest.get("confirmation_id"):
                return {"result": "refused", "pk": pk, "reason": "already_confirmed"}
            if latest.get("status") in ("applied", "applied_manual"):
                return {"result": "duplicate", "pk": pk, "reason": "already_applied"}
            if (require_request and
                    (not latest.get("apply_requested_at") or
                     latest.get("status") not in ("tailoring", "tailored"))):
                return {"result": "refused", "pk": pk,
                        "reason": "apply_request_or_preparation_not_current"}
            if any(item.get("pk") == pk for item in q.pending()):
                return {"result": "already_queued", "pk": pk, "queued": False}
            return {"result": "refused", "pk": pk, "reason": "apply_queue_put_refused"}

        # A fresh queue item does not authorize a stale status write. Keep the
        # row-local reader generation and exact admitted status as the atomic
        # precondition; independent hold and confirmation are checked afresh.
        check = _reader_check(pk, row, stores)
        if check:
            withdraw_stale_preparation()
            return check
        if require_request and not (stores.tracking.get(pk) or {}).get("apply_requested_at"):
            withdraw_stale_preparation()
            return {"result": "refused", "pk": pk,
                    "reason": "apply_request_or_preparation_not_current"}
        committed, _ = _reader_patch(pk, row, stores, {
            "status": Status.TAILORED, "gate_reason": "" if requested else "approval",
            **({"gate_pending": {}} if requested else {}),
            "fail_reason": "", "fail_kind": "",
        })
        if not committed:
            withdraw_stale_preparation()
            return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}
        ahead = q.depth()["queued"].get((row.get("company") or "").strip().lower(), 0)
    except Exception:
        log.exception("apply queue/tracking operation failed for %s", pk)
        return {"result": "error", "pk": pk, "reason": "apply_queue_tracking_error"}
    log.info("queued %s for apply%s (%s in that company's queue)",
             pk, " — NEXT, the owner just answered its question" if priority else "",
             ahead)
    return {"result": "queued", "pk": pk, "queued": True, "company_depth": ahead}


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


def _group_question_valid(field: dict) -> bool:
    """Issue 278: a stored native checkbox option group, as the browser layer issues it."""
    options, members = field.get('options'), field.get('group_members')
    return (field.get('tag') == 'input' and field.get('owner_kind') is None
            and isinstance(options, list) and isinstance(members, list)
            and len(options) >= 2 and len(options) == len(members)
            and all(isinstance(o, str) and o.strip() for o in options)
            and len({' '.join(o.casefold().split()) for o in options}) == len(options)
            and all(isinstance(m, dict) and all(
                isinstance(m.get(k), str) and m[k].strip()
                for k in ('selector', 'label', 'form_selector'))
                and m['form_selector'] == field['form_selector']
                for m in members)
            and [m['label'] for m in members] == options
            and len({m['selector'] for m in members}) == len(members)
            and members[0]['selector'] == field['selector']
            and members[0]['label'] == field['control_label'])


def _parse_group_answer(answer: str, options: list[str]) -> list[str] | None:
    """A JSON array of exact offered labels; anything else is rejected."""
    try:
        chosen = json.loads(answer)
    except ValueError:
        return None
    if (not isinstance(chosen, list) or not chosen
            or any(not isinstance(c, str) for c in chosen)
            or len(set(chosen)) != len(chosen)
            or any(c not in options for c in chosen)):
        return None
    return chosen


def _direct_form_label(pk: str, row: dict) -> str | None:
    """Identify a live owner question without minting a control grant.

    Generation/identity consumption still prevents replies to stale questions.
    Preparation itself never requires this question's field witness.
    """
    pending = row.get('gate_pending') or {}
    field = pending.get('form_question')
    if (row.get('pk') != pk or row.get('status') != 'needs_human'
            or row.get('gate_source') != 'applier'
            or row.get('gate_call_id') != 'direct'
            or not isinstance(field, dict) or field.get('pk') != pk
            or not isinstance(field.get('label'), str)
            or not field['label'].strip()):
        return None
    return field['label']


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
        "posting_guard_generation": (row.get("posting_guard") or {}).get("generation", ""),
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
    async for event in runner.run_async(user_id=_USER, session_id=sid, new_message=msg):
        for response in event.get_function_responses() or []:
            if response.name == "save_tailored_resume":
                _observe_pdf(response.response)


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
    from tools import submit_review
    if submit_review.guarded_row(row):
        return {"result": "gated", "pk": pk, "reason": "submit_review"}
    status = row.get("status") or ""
    if submit_hold.blocked(pk, row, tracking=stores.tracking):
        return {'result': 'failed', 'pk': pk, 'reason': 'uncertain',
                'detail': submit_hold.REASON}
    if status in _NO_RETAILOR:
        why = ("that application has already gone out — a fresh résumé under this "
               "row would look like the one the employer received"
               if status != "submitting" else
               "a browser is filling this form right now")
        log.warning("refusing to re-tailor %s (%s)", pk, status)
        return {"result": "refused", "pk": pk, "status": status, "reason": why}

    if status == "tailoring":
        return {"result": "refused", "pk": pk, "reason": "preparation_in_progress"}
    if not _claim(pk, stores):
        return {"result": "already_running", "pk": pk}
    from core.apply_queue import ApplyQueue
    queue = stores.queue
    if not callable(getattr(queue, "claim_human_outcome", None)):
        queue = ApplyQueue(stores.tracking.r)
    company = row.get("company") or ""
    leased = False
    try:
        # next() claims this exact company lease BEFORE LREM. It closes the
        # transfer gap that separate pending/pk-lease reads cannot observe.
        if not queue.claim_human_outcome(company):
            return {"result": "refused", "pk": pk, "reason": "apply_in_flight_or_unknown"}
        leased = True
        if _active_apply_lease(pk, stores) is not False:
            return {"result": "refused", "pk": pk, "reason": "apply_in_flight_or_unknown"}
        pending = _pending_apply_item(pk, stores)
        if pending is None:
            return _pending_refusal(pk, pending)
        check = _reader_check(pk, row, stores)
        if check:
            return check
        if pending:
            if not queue.remove(pk):
                return {"result": "refused", "pk": pk, "reason": "pending_withdrawal_lost"}
        # Removal can lose to next(); never assert cancellation of leased work.
        if (_active_apply_lease(pk, stores) is not False
                or _pending_apply_item(pk, stores) is not False):
            return {"result": "refused", "pk": pk, "reason": "apply_dispatch_not_withdrawn"}
        check = _reader_check(pk, row, stores)
        if check:
            return check
        ok, admitted = _reader_patch(pk, row, stores, {
            "status": Status.TAILORING, "gate_reason": "", "gate_pending": {},
            "gate_call_id": "", "gate_source": "", "apply_requested_at": "",
            **({"tailor_note": note.strip()} if note is not None else {}),
        })
        if not ok:
            return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}
        return _run(_retailor_preparation(pk, admitted, stores))
    finally:
        if leased:
            queue.release_human_outcome(company)
        _release(pk, stores)


async def _retailor_preparation(pk: str, row: dict, stores: Any) -> dict:
    from .graph import _PDF_INVOCATION
    from core import steering
    from tools import posting_guard as pg

    invocation = {"row": row, "stores": stores, "retailor": True}
    token = _PDF_INVOCATION.set(invocation)
    try:
        if _account_portal(row.get("jd_url", "")):
            return _preparation_skip(pk, row, stores, "account portal")
        inspected = await _jd_meta(row)
        updated = _commit_posting_meta(pk, row, inspected, stores)
        if updated is None:
            return {"result": "conflict", "pk": pk, "reason": "posting_guard_state_changed"}
        invocation["row"] = updated
        jd = updated.get("jd_text") or ""
        conflict = _eligibility_conflict(jd, updated.get("location") or "",
            _effective_prefs(updated.get("company", "")), steering.read()["content"])
        if conflict:
            return _preparation_skip(pk, updated, stores, conflict)
        await _tailor_only(pk, _session_state(updated, jd),
                           pg.analyze(updated.get("title", ""))["text"],
                           updated.get("company", ""))
        problem = _pdf_problem(pk, stores)
        if problem:
            return problem
        problem = _pdf_patch(pk, stores, {
            "status": Status.TAILORED, "retailored_at": _utc_now().isoformat()})
        if problem:
            return problem
        return {"result": "ok", "pk": pk,
                "resume_key": invocation["row"]["resume_s3_key"],
                "note": invocation["row"].get("tailor_note", "")}
    except Exception:
        log.exception("re-tailor preparation failed")
        detail = "Re-tailoring did not complete. Retry preparation; prior artifacts are retained."
        problem = _pdf_patch(pk, stores, {
            "status": Status.ERROR, "fail_kind": "retailor_failed",
            "error": detail, "fail_reason": detail,
            "gate_reason": "", "gate_pending": {}, "gate_call_id": "",
            "gate_source": "", "apply_requested_at": "",
        })
        return problem or {"result": "error", "pk": pk, "reason": "retailor_failed"}
    finally:
        _PDF_INVOCATION.reset(token)


def _consume_direct_gate(pk: str, row: dict, stores: Any, *, skip: bool = False,
                         bank: tuple | None = None,
                         refusal: str | None = None) -> dict:
    """Spend the exact owner question, then bank and continue (no field grant).

    The atomic commit moves the row to ``tailored`` with the gate cleared, so a
    later enqueue failure leaves a recoverable tailored row with no live gate:
    its actual result is returned, never a claimed ``queued``.
    """
    from tools.browser_skill_apply import owner_question_identity
    pending = row['gate_pending']
    observed = pending['form_question']
    identity = owner_question_identity(pk, observed)
    disposition = {'kind': 'skip' if skip else 'refuse' if refusal else 'answer',
                   'identity': identity, 'generation': pending['generation'],
                   'at': datetime.now(UTC).isoformat(),
                   **({'detail': refusal} if refusal else {})}
    record = None
    if skip:
        record = (identity, {'identity': identity, 'route': observed.get('route'),
                             'kind': observed.get('type'), 'label': observed.get('label'),
                             'generation': pending['generation']})
    won = stores.tracking.consume_direct_form_gate(
        pk, expected_pending=pending, disposition=disposition,
        grant=None, skip=record)
    if not won:
        return {'result': 'gated', 'pk': pk, 'reason': 'invalid_answer',
                'detail': 'This owner question was already answered or changed; '
                          'refresh and answer the current question.'}
    if refusal:
        return {'result': 'gated', 'pk': pk, 'reason': 'cannot_proceed', 'detail': refusal}
    if bank is not None:
        label, answer, scope, company = bank
        stores.answer_bank.put(label, answer, scope, company=company, source="dashboard")
    return _enqueue_apply(pk, stores, priority=True, require_request=False)


def _direct_gate_pending(question: str, form_question: Any, review_route: Any) -> dict:
    """The stored pending map for one newly issued applier gate.

    Issue #202: every newly issued direct field gate gets a fresh,
    unpredictable generation, even when its text and bound field repeat an
    earlier one, so a stale reply can never consume it.
    """
    import secrets
    pending = {"question": question, 'form_question': form_question,
               "review_route": review_route or "hard_refusal"}
    if review_route == "owner" and isinstance(form_question, dict):
        pending['generation'] = secrets.token_urlsafe(24)
    return pending


def _owner_skip_retention(pk: str, stores: Any, form_question: Any) -> dict:
    """Which SKIP records survive a newly issued gate.

    A root-proven (non-mismatch) gate clears them all: a skip never carries
    across the mismatch-to-root transition. A regenerated gate for the very
    question a record names clears that record, since the owner is being
    asked again. So does a gate for the same question and kind at another
    selector: the owner is asked again there, and a later return to the old
    selector must not revive the old SKIP.
    """
    from tools.browser_skill_apply import _MISMATCH_ROUTE, _same, owner_question_identity
    skips = (stores.tracking.get(pk) or {}).get('owner_gate_skips')
    if not isinstance(skips, dict) or not skips:
        return {}
    if not isinstance(form_question, dict) or form_question.get('route') != _MISMATCH_ROUTE:
        return {'owner_gate_skips': {}}
    identity = owner_question_identity(pk, form_question)
    label, kind = form_question.get('label'), form_question.get('type')

    def reasked(key, record):
        return key == identity or (
            isinstance(record, dict) and isinstance(label, str) and label.strip()
            and isinstance(record.get('label'), str) and _same(record['label'], label)
            and record.get('kind') == kind)

    kept = {k: v for k, v in skips.items() if not reasked(k, v)}
    return {} if len(kept) == len(skips) else {'owner_gate_skips': kept}


_DISPOSITIONS = frozenset({'answer', 'skip'})


def _direct_form_refusal(pk: str, row: dict, form_label: str | None, answer: str,
                         disposition: Any, generation: Any, identity: Any) -> dict | None:
    """Exact-request validation for a bound direct field gate, before any effect.

    A gate stored before per-gate generations existed cannot be consumed by any
    caller: only a later bounded pass may reissue it with a fresh generation.
    """
    from tools.browser_skill_apply import owner_question_identity, owner_skip_eligible
    pending = row.get('gate_pending') or {}
    stored = pending.get('generation')
    stale = {'result': 'gated', 'pk': pk, 'reason': 'invalid_answer',
             'detail': 'This owner question is no longer the pending one; '
                       'refresh and answer the current question.'}
    if not isinstance(stored, str) or not stored or form_label is None:
        return stale
    if (disposition not in _DISPOSITIONS or not isinstance(generation, str)
            or not isinstance(identity, str)):
        return {'result': 'gated', 'pk': pk, 'reason': 'invalid_answer',
                'detail': 'Answer or skip the exact current owner question.'}
    if (generation != stored
            or identity != owner_question_identity(pk, pending['form_question'])):
        return stale
    if disposition == 'answer' and not answer.strip():
        return {'result': 'gated', 'pk': pk, 'reason': 'invalid_answer',
                'detail': 'Answer the exact observed field question.'}
    if disposition == 'skip' and not owner_skip_eligible(pending['form_question']):
        return {'result': 'gated', 'pk': pk, 'reason': 'invalid_answer',
                'detail': 'This field cannot be skipped; answer it or apply manually.'}
    return None


def resume_job(pk: str, answer: str, stores: Any = None, *,
               disposition: str | None = None,
               expected_gate_generation: str | None = None,
               expected_question_identity: str | None = None) -> dict:
    """Human answered the gate: SAVE the answer as a reusable fact, then continue.

    APPLIER gates (the "Ready to apply?" approval, or a field the browser needed)
    resume DETERMINISTICALLY: code drives the browser apply directly — we never
    depend on an LLM deciding to call the tool again. Gates from other stages
    (tailor/critic questions) resume through the ADK session as before.
    """
    from core.models import AnswerScope

    stores = stores or make_stores()
    row = stores.tracking.get(pk) or {}
    from tools import submit_review
    if submit_review.guarded_row(row):
        return {"result": "gated", "pk": pk, "reason": "submit_review"}
    if submit_hold.blocked(pk, row, tracking=stores.tracking):
        return {"result": "failed", "pk": pk, "reason": "uncertain",
                "detail": "possible submission; check the employer portal before retrying"}
    call_id = row.get("gate_call_id")
    if not call_id and disposition is not None:
        # An exact owner reply names one pending gate. Once spent, it is not a
        # one-click Apply for the tailored row it left behind.
        return {'result': 'gated', 'pk': pk, 'reason': 'invalid_answer',
                'detail': 'This owner question is no longer the pending one; '
                          'refresh and answer the current question.'}
    if not call_id:
        # Ungated TAILORED rows (tailored before approval gates kept status, or
        # re-queued items) still take the one-click ▶ Apply: approving one means
        # "run the browser apply for it now".
        if row.get("status") == "tailored":
            return _enqueue_apply(pk, stores, require_request=False, expected_row=row)
        return {"result": "not_gated", "pk": pk}

    question = (row.get("gate_pending") or {}).get("question") or ""
    form_label = _direct_form_label(pk, row)
    bound_field = (row.get('gate_pending') or {}).get('form_question')
    if (form_label is None and row.get('gate_source') == 'applier'
            and isinstance(bound_field, dict)):
        # A stale or malformed legal form question is not a reusable applicant
        # fact. Fail closed instead of laundering the owner's reply into GLOBAL.
        from tools.browser_skill_apply import _material_question
        if _material_question(bound_field):
            return {'result': 'gated', 'pk': pk, 'reason': 'invalid_answer',
                    'detail': 'The observed material form question is no longer current.'}
    direct_form = (call_id == 'direct' and row.get('gate_source') == 'applier'
                   and isinstance(bound_field, dict))
    if direct_form:
        refusal = _direct_form_refusal(
            pk, row, form_label, answer, disposition,
            expected_gate_generation, expected_question_identity)
        if refusal is not None:
            return refusal
        if disposition == 'skip':
            return _consume_direct_gate(pk, row, stores, skip=True)
    elif disposition == 'skip':
        return {'result': 'gated', 'pk': pk, 'reason': 'invalid_answer',
                'detail': 'Only a bound owner field question can be skipped.'}
    elif (disposition is not None or expected_gate_generation is not None
          or expected_question_identity is not None):
        # A tokened reply names one exact field question. The current gate is
        # not one (an approval, another stage), so it never approves or banks.
        return {'result': 'gated', 'pk': pk, 'reason': 'invalid_answer',
                'detail': 'This owner question is no longer the pending one; '
                          'refresh and answer the current question.'}
    approval = form_label is None and question.startswith("Ready to apply")
    if (form_label is not None
            or (question and not approval
                and answer.strip().lower() not in ("", "approved"))):
        if form_label is not None and not answer.strip():
            return {"result": "gated", "pk": pk, "reason": "invalid_answer",
                    "detail": "Answer the exact observed field question."}
        # Bank real answers so the same question never gates again. Facts (name,
        # work auth, …) are GLOBAL; company/role-specific prose stays scoped.
        company = row.get("company", "")
        # File it under the FIELD the gate named, not the paragraph the gate was
        # written in. Scope is then judged on the label too: "What is your
        # desired salary range?" is a fact about the owner and belongs to every
        # employer, even though the paragraph around it said "Replit" three times
        # and would have locked it to Replit alone.
        if form_label:
            observed = row['gate_pending']['form_question']
            normalized = ' '.join(answer.casefold().split())
            group_selected = None
            if observed['type'] == 'checkbox_group':
                group_selected = _parse_group_answer(answer, observed['options'])
                if group_selected is None:
                    return {'result': 'gated', 'pk': pk, 'reason': 'invalid_answer',
                            'detail': 'Answer with a JSON array of one or more exact '
                                      'offered option labels, each at most once.'}
            elif observed['type'] == 'checkbox':
                if normalized == 'no':
                    detail = ('Cannot proceed with this application without the required '
                              'acknowledgement: ' + observed['control_label'])
                    return _consume_direct_gate(pk, row, stores, refusal=detail)
                if normalized != 'yes':
                    return {'result': 'gated', 'pk': pk, 'reason': 'invalid_answer',
                            'detail': 'Answer yes or no for the exact checkbox question.'}
            elif observed['type'] == 'button':
                matching = [member for member in observed['boolean_members']
                            if normalized in {
                                member['caption'].casefold(), member['value'].casefold()}]
                if len(matching) != 1:
                    return {'result': 'gated', 'pk': pk, 'reason': 'invalid_answer',
                            'detail': 'Choose exactly one witnessed Boolean caption.'}
            elif observed['type'] == 'radio':
                matching = [member for member in observed['radio_members']
                            if normalized in {
                                ' '.join(member['caption'].casefold().split()),
                                ' '.join(member['value'].casefold().split())}]
                if len(matching) != 1:
                    return {'result': 'gated', 'pk': pk, 'reason': 'invalid_answer',
                            'detail': 'Choose exactly one of the observed radio captions.'}
        label = form_label or _gate_label(question) or question
        personal = label.lower().startswith("why") or "this role" in label.lower() \
            or (company and company.lower() in label.lower())
        scope = AnswerScope.COMPANY if personal else AnswerScope.GLOBAL
        # A bound form label is untrusted page prose; keep it in the owner gate,
        # never in the ordinary answer-bank telemetry.
        if label != question and not form_label:
            log.info("banking the gate answer for %s under %r (from a %d-char question)",
                     pk, label, len(question))
        # Owner-attested material assent is per-job permission, not a global
        # reusable fact. The original observed control grant is persisted below
        # and is the only answer source allowed for material select/combobox.
        material_form = False
        if form_label:
            from tools.browser_skill_apply import _material_question
            material_form = _material_question(row['gate_pending']['form_question'])
        if not form_label:
            stores.answer_bank.put(label, answer, scope,
                                   company=company or None, source="dashboard")
        # Internal answers stay reusable bank facts, not employer disclosure
        # grants. The exact observed direct form question owns the latter.
        if form_label:
            observed = row['gate_pending']['form_question']
            # The owner's answer is an ordinary fact, not a browser write
            # permission or a selector-bound approval token. Material answers
            # are kept company-scoped rather than made GLOBAL.
            bank_scope = AnswerScope.COMPANY if material_form else scope
            bank = (label, answer, bank_scope, company or None)
            return _consume_direct_gate(pk, row, stores, bank=bank)

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
        return _enqueue_apply(pk, stores, priority=bool(question) and not approval,
                              require_request=False, expected_row=row)
    return _run(_resume_job_async(pk, answer, call_id, stores))


def run_queued(item: dict, q: Any) -> dict:
    """Run ONE leased application and settle its place in the queue.

    Shared by the daemon's apply worker and the dashboard's per company drain, so
    the retry rules, the dead letter path and the lease release cannot drift apart
    between them. Releasing the lease in a `finally` is the whole point: a lease
    left behind by a crash blocks that employer until the process restarts.
    """
    from core.apply_queue import is_retryable
    from tools import submit_review

    pk = item["pk"]
    try:
        result = _run(_apply_direct(pk, make_stores())) or {}
        # Owner-only Gate details never enter the queued worker's INFO result.
        navigation_detail = result.get("navigation_provenance_diagnostic") is True
        material_detail = result.get("material_assent_diagnostic") is True
        logged_result = (
            {"result": "gated", "pk": pk,
             "question": ("navigation-provenance: human inspection required"
                          if navigation_detail else
                          "material-assent: human inspection required"
                          if material_detail else
                          "captcha-artifact: human inspection required")}
            if result.get("result") == "gated"
            and (navigation_detail or material_detail
                 or result.get("captcha_artifact_diagnostic") is True) else result
        )
        log.info("apply: %s", logged_result)
        # A no-click review expiry/rejection is terminal for this dispatch.
        try:
            protected = submit_review.guarded_row(
                make_stores().tracking.get(pk) or {})
        except Exception:
            protected = True
        again, why = is_retryable(result)
        if again and not protected:
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
        failed_stores = None
        try:
            failed_stores = make_stores()
            row = failed_stores.tracking.get(pk) or {}
        except Exception:
            row = {"possible_submission": True}  # cannot prove it is safe to retry
        if failed_stores is None or submit_review.guarded_row(row):
            return {"result": "gated", "pk": pk, "reason": "submit_review",
                    "detail": "Current review run cannot be automatically retried."}
        if (row.get('possible_submission')
                or submit_hold.blocked(pk, row, tracking=failed_stores.tracking)):
            try:
                make_stores().tracking.set_status(
                    pk, Status.NEEDS_HUMAN, gate_reason="submit_uncertain",
                    fail_kind="uncertain", fail_reason="possible submission; check the employer portal before retrying")
            except Exception:
                log.exception("could not settle possible submission for %s", pk)
            return {"result": "failed", "pk": pk, "reason": "uncertain",
                    "detail": "possible submission; check the employer portal before retrying"}
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
    from tools import submit_review
    if submit_review.guarded_row(row):
        return {"result": "gated", "pk": pk, "reason": "submit_review"}
    company, jd_url = row.get("company", ""), row.get("jd_url", "")
    # Pre-browser JD handoff: neither the queued row nor a transient reader
    # resolution may turn an original relative href into an Apply destination.
    if _historical_relative(row):
        return {"result": "gated", "pk": pk, "reason": "posting_guard_review_required",
                "detail": "Historical relative posting requires manual source review."}

    # DUPLICATE GUARD — the one mistake this system must never make twice: a row
    # already applied (by the pipeline or by the owner's own hand) is refused
    # loudly, and its applied status is never overwritten. The tool layer checks
    # again right before the submit click; this is the cheap early exit.
    # SKIPPED belongs here too. Skipping only set the tracking status and left the
    # pk sitting in the apply queue, so a job the owner had explicitly declined was
    # still dispatched and applied to. "I do not want this one" has to mean it.
    if submit_hold.blocked(pk, row, tracking=stores.tracking):
        return {"result": "failed", "pk": pk, "reason": "uncertain",
                "detail": "possible submission; check the employer portal before retrying"}
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
    # The approved queue dispatcher owns this run; clear the pre-dispatch UI gate.
    stores.tracking.set_status(pk, Status.SUBMITTING, gate_reason="")
    emit("running", pk=pk, agent="applier", detail="reading the posting…", url=jd_url)

    # set_status does not claim guard/JD ownership. Freeze the post-claim row,
    # comparing it to the pre-claim approval before the slow source read.
    expected_claim = {**row, "status": Status.SUBMITTING.value, "gate_reason": ""}
    changed, observed = _direct_approval_check(pk, expected_claim, stores)
    if changed:
        return changed

    try:
        jd_meta = await _jd_meta(row, yielding=False, approval=True)
        changed, _ = _direct_approval_check(pk, observed, stores)
        if changed:
            return changed
        from tools import posting_guard as pg

        # This PDF was tailored to the captured JD. A different posting must
        # be prepared afresh; updating JD here would falsely certify that PDF.
        baseline = pg.analyze(row.get("jd_text") or "",
                              strip_trailing=True)["text"]
        incoming = pg.analyze(jd_meta["text"], strip_trailing=True)["text"]
        if incoming != baseline:
            return _guard_refusal(
                pk, stores, observed=observed,
                detail="posting changed since tailoring; retry the row to re-tailor")
        legacy = _approval_legacy_evidence(row)
        if legacy == "unrecognized":
            return _guard_refusal(
                pk, stores, observed=observed,
                detail="legacy posting_guard reasons unrecognized; owner review")
        if legacy:
            return _guard_refusal(pk, stores, observed=observed)

        guarded = _commit_posting_meta(pk, observed, jd_meta, stores)
        if guarded is None:
            changed, _ = _direct_approval_check(pk, observed, stores)
            return changed or {"result": "conflict", "pk": pk,
                               "reason": "posting_guard_state_changed"}
        row = guarded
        jd_text = row.get("jd_text") or ""
        # Approval deliberately does not inherit the hidden-only / previously
        # reviewed generation exemptions of other posting_guard callers.
        if (jd_meta["posting_guard"]["status"] != "clean"
                or row["posting_guard"]["status"] != "clean"
                or not pg.authorised(row, text=jd_text)):
            return _guard_refusal(pk, stores, observed=row)
        changed, _ = _direct_approval_check(pk, row, stores)
        if changed:
            return changed
    except Exception as exc:  # noqa: BLE001
        # A reader failure is not permission to use cached verified metadata.
        # Refusal must compare the original claim, not settle a newer owner.
        log.exception("guarded JD fetch failed for pk=%s", pk)
        failure = str(exc).strip() or type(exc).__name__
        detail = f"Posting source read failed ({type(exc).__name__}): {failure}"
        emit("error", pk=pk, agent="applier", url=jd_url, detail=detail)
        refused = _guard_refusal(pk, stores, observed=observed)
        return {**refused, "detail": detail}
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

    # Rotation/profile preparation may take time. No source proof can waive a
    # later row change or independent durable submission hold at admission.
    changed, _ = _direct_approval_check(pk, row, stores)
    if changed:
        return changed
    emit("running", pk=pk, agent="applier", detail="filling the form…", url=jd_url)

    result = await browser_apply(
        jd_url, company, facts, get_settings().browser_model,
        pk=pk, jd_text=jd_text, resume_tex=resume_tex,
        github=_github_context(), resume_path=_resume_pdf_path(row),
    )
    if result.get("reason") == "posting_guard_review_required":
        return _guard_refusal(pk, stores)
    # A manual confirmation can arrive while the browser is filling the form.
    # A pre-submit duplicate refusal must not turn that applied row into a gate.
    if (stores.tracking.get(pk) or {}).get("status") in ("applied", "applied_manual"):
        return {"result": "duplicate", "pk": pk, "reason": "already_applied"}

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
    from tools.browser_runtime import configuration
    drafted = result.pop('drafted', None) or {}
    # BrowserSkill cannot turn a writer draft into future human authorization.
    if configuration()['engine'] != 'browser_skill':
        for q, a in drafted.items():
            from core.models import AnswerScope
            stores.answer_bank.put(q, a, AnswerScope.COMPANY, company=company, source='writer')

    # A consumed review cannot be replaced by a generic BrowserSkill gate/error.
    if submit_review.guarded_row(stores.tracking.get(pk) or {}):
        return {"result": "gated", "pk": pk, "reason": "submit_review"}
    status = result.get("status")
    # The durable pre-click marker outranks a transport return that says
    # "gate" or "unknown": the browser might already have committed a form.
    if submit_hold.blocked(pk, stores.tracking.get(pk) or {}, tracking=stores.tracking) and status != 'applied':
        status = "uncertain"
        result["status"] = status
    if status == "uncertain":
        from tools.browser_skill_apply import handoff
        context = handoff(submit_hold.REASON, result.get('step') or {})
        reason = context['text']
        stores.tracking.set_status(
            pk, Status.NEEDS_HUMAN, gate_reason="submit_uncertain",
            fail_kind="uncertain", fail_reason=reason,
            gate_pending={'question': reason}, gate_call_id=None,
            last_button=context['step']['last_button'], last_url=context['step']['url'])
        emit("gate", pk=pk, agent="applier", detail=reason, url=jd_url)
        return {"result": "failed", "pk": pk, "reason": "uncertain", "detail": reason}
    if status == "applied":
        conf = result.get("confirmation") or "submitted"
        stores.tracking.set_status(
            pk, Status.APPLIED, confirmation_id=conf,
            gate_reason="", gate_pending=None, gate_generation=None,
            review_state=None, review_expires_at=None,
            **_clear_infra_failure(pk, stores))
        if getattr(stores.tracking, 'r', None) is not None:
            submit_hold.clear(pk, tracking=stores.tracking)
        _note_rotation_use(pk, stores)
        emit("applied", pk=pk, detail=conf, url=jd_url)
        return {"result": "done", "pk": pk, "confirmation": conf}
    if status == "gate":
        q = result.get("question") or "The applier needs your input to continue."
        # Only the three explicitly owner-routed questions use the existing
        # direct reply channel. An unsupported private Luna transport is a
        # durable named gate, never a fabricated reviewer session/approval.
        review_route = (result.get("review_route") if configuration()['engine'] == 'browser_skill'
                        else "owner")
        form_question = (result.get('form_question')
                         if configuration()['engine'] == 'browser_skill' else None)
        pending = _direct_gate_pending(q, form_question, review_route)
        item = result.get('item')
        if type(item) is int and item in {1, 2, 3, 4, 5}:
            pending['pre_submit_item'] = item
        stores.tracking.set_status(pk, Status.NEEDS_HUMAN,
                                   **_clear_infra_failure(pk, stores),
                                   **_owner_skip_retention(pk, stores, form_question),
                                   gate_reason=(f'pre_submit_item_{item}'
                                                if type(item) is int and item in {1, 2, 3, 4, 5}
                                                else result.get("reason") or "unknown_field"),
                                   gate_pending=pending,
                                   gate_call_id="direct" if review_route == "owner" else None,
                                   gate_source="applier")
        # Keep full details in the owner row/reply only, never INFO/queued/event.
        material_assent = (configuration()['engine'] == 'browser_skill'
                           and result.get('material_assent_diagnostic') is True)
        # BrowserSkill now reports the code-owned provenance refusal by
        # reason instead of exporting a separate legacy diagnostic flag.
        # An owner-only URL/path must never reach INFO or public event data.
        navigation_detail = (configuration()['engine'] == 'browser_skill'
                             and (result.get('reason') == 'defect_provenance'
                                  or result.get('navigation_provenance_diagnostic') is True))
        captcha_artifact = (configuration()['engine'] == 'browser_skill'
                            and result.get('captcha_artifact_diagnostic') is True)
        routine_detail = ("navigation-provenance: human inspection required"
                          if navigation_detail else
                          "material-assent: human inspection required"
                          if material_assent else
                          "captcha-artifact: human inspection required"
                          if captcha_artifact else q)
        if navigation_detail:
            # Host/path is owner detail, not even ordinary event URL metadata.
            emit("gate", pk=pk, agent="applier", detail=routine_detail)
        else:
            emit("gate", pk=pk, agent="applier", detail=routine_detail, url=jd_url)
        log.info("gated pk=%s: %s", pk, routine_detail)
        return {"result": "gated", "pk": pk, "question": q,
                **({"material_assent_diagnostic": True} if material_assent else {}),
                **({"navigation_provenance_diagnostic": True}
                   if navigation_detail else {}),
                **({"captcha_artifact_diagnostic": True}
                   if captcha_artifact else {})}
    reason = _fail_reason(result)
    # A browser held by something else is an ENVIRONMENT fault, not a verdict on
    # this job: the form was never reached, nothing was filled, and the same job
    # succeeds once the browser is free. Recording it as failed burns a good
    # application and hides it in the Unable lane, so hand it back to the queue.
    from tools.browser_runtime import _is_browser_conflict, is_disconnected, is_signed_out

    # A signed-out CLI is the same KIND of fault as a busy browser — nothing was
    # reached, nothing was filled — but it differs in one way that matters: it
    # does not clear on its own. Every remaining job would fail against the same
    # wall, and at three attempts each, a queue of two hundred dead-letters in
    # about a minute while the owner is away from the screen. So the job goes back
    # on the queue untouched AND applying is paused, which is the only thing that
    # stops the rest of the board following it down.
    if is_signed_out(reason):
        from core import flags

        _requeue_untouched(pk, stores)
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

        _requeue_untouched(pk, stores)
        if flags.auto_pause(releasable=_is_browser_skill_disconnect(reason)):
            log.warning("PAUSED applying: the browser extension is not connected")
        emit("gate", pk=pk, agent="applier", url=jd_url, detail=reason)
        return {"result": "requeued", "pk": pk, "reason": "extension_disconnected"}

    if _is_browser_conflict(reason):
        _requeue_untouched(pk, stores)
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



def _clear_infra_failure(pk: str, stores: Any) -> dict:
    """Clear only the previous infrastructure-attributed failure on settlement."""
    row = stores.tracking.get(pk) or {}
    kind = row.get("fail_kind") or ""
    from tools.browser_runtime import is_infrastructure

    # Old rows used 'unknown' even for permission_denied. Never erase a real
    # unknown/error merely because a later attempt reached a gate.
    if kind in {"unknown", "failed"} and is_infrastructure(str(row.get("fail_reason") or "")):
        return {"fail_kind": "", "fail_reason": ""}
    return {}


def _is_browser_skill_disconnect(reason: str) -> bool:
    """Whether this disconnect came from BrowserSkill, judged by the error that
    was returned and not by the configuration file, which is re-read on every
    backend selection and can already name a different engine than the one that
    ran. The legacy extension wording, a reason matching both, or anything else
    is unknown origin: the pause is left to the owner and never auto-released."""
    from tools import claude_chrome

    return ((reason or "").startswith("BrowserSkill unavailable:")
            and not claude_chrome.is_disconnected(reason))


def _requeue_untouched(pk: str, stores: Any) -> None:
    """Put a job the browser never reached back on the queue, attempt unspent.

    A role the owner pressed Apply on keeps that instruction: it waits for the
    browser, not for a second go-ahead.
    """
    row = stores.tracking.get(pk) or {}
    if submit_hold.blocked(pk, row, tracking=stores.tracking):
        return
    requested = bool(row.get("apply_requested_at"))
    stores.tracking.set_status(pk, Status.TAILORED, gate_reason="" if requested else "approval",
                               fail_reason="", fail_kind="")
    stores.queue.enqueue(stores.apply_queue, {"pk": pk})


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
    current = stores.tracking.get_current(pk)
    if not current:
        return {"result": "gated", "pk": pk, "reason": "posting_guard_review_required"}
    if _historical_relative(current):
        return _relative_attention(pk, stores)
    session = await sessions.get_session(app_name=_APP, user_id=_USER, session_id=pk)
    if session is None or _stale_posting(session, current, current.get("jd_text") or ""):
        return {"result": "gated", "pk": pk, "reason": "posting_guard_review_required",
                "detail": "Posting changed; reopen and re-tailor with the current JD."}
    runner = Runner(agent=root_agent, app_name=_APP, session_service=sessions)
    # Answer the pending long-running ask_human call.
    resp = types.Content(role="user", parts=[types.Part(
        function_response=types.FunctionResponse(
            id=call_id, name="ask_human", response={"answer": answer}))])
    return await _drive_async(runner, pk, resp, stores)


def _active_apply_lease(pk: str, stores: Any) -> bool | None:
    """Inspect the existing queue lease; unknown is never proof of inactivity."""
    try:
        # Test/cloud callers may already hold an existing queue inspector.
        # Do not fabricate absence when the backend offers no such surface.
        inspector = getattr(stores, "queue", None)
        if callable(getattr(inspector, "in_flight", None)):
            return pk in inspector.in_flight()
        client = getattr(stores.tracking, "r", None)
        if client is None:
            return None
        from core.apply_queue import ApplyQueue
        return pk in ApplyQueue(client).in_flight()
    except Exception:
        log.exception("apply-queue lease inspection unavailable for %s", pk)
        return None


_GUARD_REFUSAL = "posting_guard_review_required"


def _pending_apply_item(pk: str, stores: Any) -> bool | None:
    """Read-only: any pending apply-queue item for this pk. None = unknown."""
    try:
        inspector = getattr(stores, "queue", None)
        if callable(getattr(inspector, "has_pending", None)):
            return inspector.has_pending(pk)
        client = getattr(stores.tracking, "r", None)
        if client is None:
            return None
        from core.apply_queue import ApplyQueue
        return ApplyQueue(client).has_pending(pk)
    except Exception:
        log.exception("apply-queue pending inspection unavailable for %s", pk)
        return None


def _pending_refusal(pk: str, pending: bool | None) -> dict:
    if pending is None:
        return {"result": "refused", "pk": pk, "reason": "apply_queue_unknown",
                "detail": "Cannot safely inspect pending applications; restore queue inspection."}
    return {"result": "refused", "pk": pk, "reason": "apply_queue_pending",
            "detail": "An application for this job is still queued; remove it from the "
                      "queue first. Nothing was changed."}


def _posting_retry_static_refusal(pk: str, row: dict) -> dict | None:
    if _historical_relative(row):
        return {"result": "refused", "pk": pk, "reason": _GUARD_REFUSAL,
                "detail": "Historical relative posting requires manual source review."}
    if not row.get("jd_url"):
        return {"result": "refused", "pk": pk, "reason": "no_source_url",
                "detail": "No posting URL to re-read; nothing was changed."}
    return None


def _retry_row_gate(pk: str, row: dict, stores: Any,
                    explicit_review_restart: bool) -> tuple[dict | None, bool, bool]:
    """Row/hold classification shared by Retry and its HTTP precheck.

    Returns (refusal | None, review_restart, posting_retry). `posting_retry` is
    a closed, unsent `tailored` + posting_guard_review_required row; the
    marker is NOT origin proof, only the existing protections gate it.
    """
    from tools import submit_review
    # An independent submit hold is authoritative even if an old writer
    # restored the row's status to FOUND. The public Retry response must not
    # downgrade uncertainty into an innocuous "already_done".
    if submit_hold.blocked(pk, row, tracking=stores.tracking):
        return ({"result": "failed", "pk": pk, "reason": "uncertain",
                 "detail": submit_hold.REASON}, False, False)
    status = row.get("status")
    review_restart = submit_review.guarded_row(row)
    if review_restart and status == "tailored" and row.get("fail_kind") == _GUARD_REFUSAL:
        # Never borrow the separately authorized submit_review restart.
        return ({"result": "gated", "pk": pk, "reason": "submit_review"}, True, False)
    if review_restart and (
            not explicit_review_restart or not submit_review.manual_restart_allowed(
                pk, row, tracking=stores.tracking)):
        return ({"result": "gated", "pk": pk, "reason": "submit_review"}, True, False)
    if status in ("applied", "applied_manual"):
        return ({"result": "duplicate", "pk": pk, "reason": "already_applied"},
                review_restart, False)
    if status == "tailoring":
        from core.events import emit
        emit("response", pk=pk, url=row.get("jd_url"), detail=_ALREADY_RUNNING)
        return {"result": "already_running", "pk": pk}, review_restart, False
    # A closed, unsent posting-guard refusal: explicit Retry re-reads the
    # current posting and re-prepares for review only. Never a general
    # tailored restart.
    posting_retry = (status == "tailored" and not review_restart
                     and row.get("fail_kind") == _GUARD_REFUSAL)
    if posting_retry:
        # Never rely on gate_reason alone: any submission-attempt or review
        # evidence on the row refuses, whatever the review/hold state claims.
        if (row.get("possible_submission") or row.get("confirmation_id")
                or row.get("submit_hold")
                or row.get("submit_hold_state") not in (None, "", "clear", "cleared")):
            return ({"result": "failed", "pk": pk, "reason": "uncertain",
                     "detail": submit_hold.REASON}, False, False)
        if row.get("review_state") or row.get("gate_generation"):
            return ({"result": "gated", "pk": pk, "reason": "submit_review"},
                    True, False)
        return _posting_retry_static_refusal(pk, row), False, True
    if (status not in ("skipped", "failed", "error", "job_gone", "capped")
            and not (review_restart and status == "needs_human")):
        return ({"result": "already_done", "pk": pk, "status": status},
                review_restart, False)
    return None, review_restart, False


def _retry_live_gate(pk: str, row: dict, stores: Any, posting_retry: bool) -> dict | None:
    """Fresh state, claim, lease and (posting Retry) pending-queue inspection."""
    check = _reader_check(pk, row, stores)
    if check:
        return check
    if _claimed(pk, stores):
        return {"result": "already_running", "pk": pk}
    lease = _active_apply_lease(pk, stores)
    if lease is None:
        return {"result": "refused", "pk": pk, "reason": "apply_lease_unknown",
                "detail": "Cannot safely inspect an in-flight application; restore queue inspection."}
    if lease:
        return {"result": "already_running", "pk": pk, "reason": "apply_in_flight"}
    if posting_retry:
        pending = _pending_apply_item(pk, stores)
        if pending is not False:
            return _pending_refusal(pk, pending)
    return None


def retry_precheck(pk: str, stores: Any = None, *,
                   explicit_review_restart: bool = False) -> dict | None:
    """Read-only admission predicate for the HTTP route; None = would start.

    Writes, claims and releases nothing; the worker rechecks every condition.
    """
    stores = stores or make_stores()
    try:
        row = stores.tracking.get(pk)
        if row is None:
            return {"result": "missing", "pk": pk}
        refusal, _review, posting_retry = _retry_row_gate(
            pk, row, stores, explicit_review_restart)
        return refusal or _retry_live_gate(pk, row, stores, posting_retry)
    except Exception as exc:
        log.exception("retry precheck failed for %s", pk)
        return {"result": "error", "pk": pk, "reason": "jd_tracking_storage_error",
                "detail": f"Cannot inspect the job safely: {type(exc).__name__}: {exc}"}


def retry_job(pk: str, stores: Any = None, *,
              explicit_review_restart: bool = False) -> dict:
    """Recover a closed unsent job under ONE claim and admitted TAILORING row.

    JD attention goes directly ERROR -> TAILORING with durable review-only
    intent. No intermediate FOUND window can race the session deletion.
    """
    stores = stores or make_stores()
    try:
        row = stores.tracking.get(pk)
    except Exception as exc:
        return _reader_storage_error(pk, stores, None, exc)
    if row is None:
        return {"result": "missing", "pk": pk}
    from tools import submit_review
    refusal, review_restart, posting_retry = _retry_row_gate(
        pk, row, stores, explicit_review_restart)
    if refusal:
        return refusal
    try:
        blocked = _retry_live_gate(pk, row, stores, posting_retry)
        if blocked:
            return blocked
    except Exception as exc:
        return _reader_storage_error(pk, stores, row, exc)

    # Skip keeps the original failure evidence. Treat it as reader attention
    # even when the current closed status is SKIPPED, so Retry cannot fall
    # back to the full apply graph after an exhausted JD read.
    jd_attention = not review_restart and not posting_retry and (
        int(row.get("jd_read_attempts") or 0) >= 3
        or row.get("fail_kind") in (
            "jd_reader_exhausted", "jd_tracking_storage_error",
            "jd_retry_session_error", "jd_url_unresolvable"))
    claim = _claim(pk, stores)
    if claim is None:
        return {"result": "error", "pk": pk, "reason": "jd_tracking_storage_error",
                "detail": "Cannot acquire evaluation claim; no session reset performed."}
    if not claim:
        return {"result": "already_running", "pk": pk}
    try:
        try:
            check = _reader_check(pk, row, stores)
            if check:
                return check
            if review_restart and not submit_review.manual_restart_allowed(
                    pk, row, tracking=stores.tracking):
                return {"result": "gated", "pk": pk, "reason": "submit_review"}
            lease = _active_apply_lease(pk, stores)
            if lease is None or lease:
                return {"result": "refused", "pk": pk, "reason": "apply_lease_unknown"
                        if lease is None else "apply_in_flight"}
            if posting_retry:
                pending = _pending_apply_item(pk, stores)
                if pending is not False:
                    return _pending_refusal(pk, pending)
            changes = {
                "status": Status.TAILORING, "fail_reason": "",
                "fail_kind": "", "skip_reason": "", "gate_pending": None,
                "gate_call_id": None,
            }
            if posting_retry:
                # Admission changes ONLY status and review-only intent. The
                # refusal marker, old request and old artifacts stay untouched
                # until the current posting is actually re-read, so a failed
                # read can restore the closed refusal unchanged.
                changes = {"status": Status.TAILORING, "jd_read_prepare_only": True}
            if jd_attention:
                changes.update({
                    "jd_read_attempts": 0, "jd_read_retry_at": None,
                    "jd_read_error": "", "error": "",
                    "jd_read_prepare_only": True,
                    # This prior document was prepared against an older JD.
                    # Its PDF key is not evidence of a successful NEW tailor.
                    "resume_s3_key": "",
                })
            else:
                changes["error"] = ""
            if review_restart:
                # Retry is the *only* explicit recovery entrypoint. The spent
                # packet and PDF must never become a shortcut to a new Submit.
                changes.update({
                    "gate_reason": "", "gate_generation": None,
                    "review_state": None, "review_expires_at": None,
                    "review_reject_reason": None,
                    "resume_s3_key": "", "resume_tex_key": "",
                    "resume_seed": "", "jd_read_prepare_only": True,
                    "jd_read_revision": int(row.get("jd_read_revision") or 0) + 1,
                })
                committed = submit_review.begin_manual_restart(
                    pk, row, changes, tracking=stores.tracking)
                admitted = {**row, **{
                    key: getattr(value, "value", value) if key == "status" else value
                    for key, value in changes.items()}}
            else:
                committed, admitted = _reader_patch(pk, row, stores, changes)
        except Exception as exc:
            return _reader_storage_error(pk, stores, row, exc)
        if not committed:
            return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}

        # Nothing below re-acquires the claim, and public run_job rejects
        # TAILORING. The competing evaluator cannot touch this ADK session.
        try:
            check = _reader_check(pk, admitted, stores)
            if check:
                if check["result"] == "refused":
                    # Protection appeared after the committed reset. Make the
                    # interruption visible without clearing its evidence.
                    try:
                        _reader_patch(pk, admitted, stores, {
                            "status": Status.ERROR,
                            "error": ("Retry stopped by submission protection; "
                                      "reconcile the outcome before Reopen.")})
                    except Exception:
                        log.exception("could not record protected recovery for %s", pk)
                return check
        except Exception as exc:
            return _reader_storage_error(pk, stores, admitted, exc)

        try:
            _run(_reset_session(pk))
        except Exception as exc:
            # No FOUND handoff gap and no second attention-write attempt after
            # a storage failure. A later Retry can recover the recorded ERROR.
            return _retry_session_error(pk, stores, admitted, exc, phase="reset")
        from core.events import emit
        emit("running", pk=pk,
             detail=f"retry · {row.get('title','')} @ {row.get('company','')}",
             url=row.get("jd_url"))
        prepare_only = bool(admitted.get("jd_read_prepare_only")
                            or admitted.get("discovery_source") == "career_ops")
        try:
            extra = {"guard_recovery": row} if posting_retry else {}
            result = _run(_run_job_async(
                pk, admitted, stores, prepare_only=prepare_only, **extra))
        except Exception as exc:
            from .graph import ScorerModelError
            detail = (("Scorer model/request failed; no valid score was received. "
                       "Check the error log, then Reopen & re-score.")
                      if isinstance(exc, ScorerModelError)
                      else "Preparation failed — see Logs and retry.")
            committed, _ = _reader_patch(pk, admitted, stores, {
                "status": Status.ERROR, "match_score": None, "error": detail})
            if committed:
                emit("error", pk=pk, detail=detail, url=row.get("jd_url"))
            raise
        if (result or {}).get("result") == "prepared" and not posting_retry:
            try:
                current = stores.tracking.get(pk) or {}
                if current.get("apply_requested_at") and not _historical_relative(current):
                    return _enqueue_apply(pk, stores, priority=True,
                                          require_request=True)
            except Exception as exc:
                return _reader_storage_error(pk, stores, admitted, exc)
        return result
    finally:
        _release(pk, stores)


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


def _stale_posting(session: Any, current: dict, jd_text: str) -> bool:
    """Old same-seed ADK states are not evidence of a guarded posting."""
    try:
        state = getattr(session, "state", None) or {}
        return (state.get("jd_text") != jd_text
                or state.get("posting_guard_generation") !=
                (current.get("posting_guard") or {}).get("generation", ""))
    except Exception:
        return True


async def _reset_session(pk: str, *, strict: bool = False) -> None:
    """Delete the ADK session for a pk so run_job re-creates it fresh."""
    sessions = _session_service()
    try:
        await sessions.delete_session(app_name=_APP, user_id=_USER, session_id=pk)
    except Exception:
        # Reopen promises fresh preferences even when seed/JD are unchanged.
        # A genuinely absent session needs no deletion; a failed delete of a
        # still-readable session must not silently reuse yesterday's state.
        if strict and await sessions.get_session(
                app_name=_APP, user_id=_USER, session_id=pk) is not None:
            raise


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


def _score_gate(pk: str, score_result: Any, stores: Any) -> dict | None:
    """Only an ADK-validated terminal MatchScore may pass the score gate."""
    from core.events import emit
    from tools.schema import MatchScore
    from .graph import _PDF_INVOCATION

    invocation = _PDF_INVOCATION.get()
    row = invocation["row"] if invocation is not None else stores.tracking.get(pk) or {}
    check = _reader_check(pk, row, stores)
    if check or row.get("status") not in ("found", "tailoring"):
        return check or {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}

    # Legacy direct callers pass a complete JSON string; validate it through
    # the same schema, never by extracting a substring. The live runner passes
    # ADK's already-validated current-event output-key dictionary instead.
    original_text = score_result if isinstance(score_result, str) else ""
    score_result = (MatchScore.model_validate_json(score_result)
                    if isinstance(score_result, str)
                    else MatchScore.model_validate(score_result))
    score, reasoning = score_result.score, (score_result.reasoning + " " + original_text)

    # A schema-valid refusal is not evidence of a weak fit and must not be
    # buried as low_score or consume the malformed-content correction.
    if score <= 1 and re.search(
            r"i'?m an ai|as an ai|i cannot|i can'?t|unable to (access|assist)"
            r"|don'?t have (a|any) (resume|r\u00e9sum\u00e9|personal)",
            reasoning, re.I):
        committed, _ = _reader_patch(pk, row, stores, {
            "status": Status.ERROR, "match_score": None,
            "error": "the scorer model refused the task instead of "
                     "scoring — hit Retry (or switch the scorer model)"})
        if not committed:
            return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}
        emit("error", pk=pk, detail="scorer refused instead of scoring — retry the job")
        log.warning("scorer refusal for pk=%s", pk)
        return {"result": "error", "pk": pk, "reason": "scorer_refused"}

    threshold = _min_score()
    low = score < threshold and row.get("score_override") is not True
    committed, updated = _reader_patch(pk, row, stores, {
        "status": Status.SKIPPED if low else Status.TAILORING,
        "match_score": score, **({"skip_reason": "low_score"} if low else {})})
    if not committed:
        return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}
    if invocation is not None:
        invocation["row"] = updated
    if score < threshold and row.get("score_override") is True:
        emit("response", pk=pk, detail=f"match {score}/10 < {threshold} — owner overrode the score; tailoring for review")
        return None
    if score < threshold:
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
        return detail or "possible submission; check the employer portal before retrying"
    if status == "unknown":
        if not detail:  # agent produced no final report at all — an internal error
            return ("The browser agent ended without any final report — most likely "
                    "an internal error during the run (check the Logs), or the "
                    "posting has no application form. Retry usually resolves it.")
        from tools.browser_runtime import is_infrastructure

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
    from tools import posting_guard as pg

    row = stores.tracking.get_current(pk) or {}
    if not pg.authorised(row):
        return ""
    if row.get("discovery_source") == "career_ops":
        return ""
    score = row.get("match_score")
    if score is None or int(score) < get_settings().auto_min_score:
        return ""  # not a confident enough match — a human still decides
    return "go"


def _observe_pdf(response: Any) -> None:
    from .graph import _PDF_INVOCATION
    invocation = _PDF_INVOCATION.get()
    if invocation is None:
        return
    invocation["attempted"] = True
    if (isinstance(response, dict) and response.get("ok") is True
            and response.get("format") == "pdf"
            and response == invocation.get("saved")):
        invocation["observed"] = dict(response)


def _pdf_problem(pk: str, stores: Any) -> dict | None:
    """A current tool response, committed bytes and the same row generation."""
    import hashlib
    from .graph import _PDF_INVOCATION
    invocation = _PDF_INVOCATION.get()
    if invocation is None:
        raise RuntimeError("Missing preparation invocation")
    row = invocation["row"]
    check = _reader_check(pk, row, stores)
    if check:
        return check
    saved = invocation.get("saved")
    if saved and saved == invocation.get("observed"):
        key = saved["resume_s3_key"]
        current = stores.tracking.get(pk) or {}
        if current.get("resume_s3_key") != key:
            return {"result": "conflict", "pk": pk, "reason": "pdf_artifact_changed"}
        try:
            data = stores.artifacts.get(key)
            if (row.get("resume_s3_key") == key and data.startswith(b"%PDF-")
                    and hashlib.sha256(data).hexdigest() == invocation["digest"]):
                return None
        except Exception:
            pass
    attempted = invocation.get("attempted", False)
    failed = attempted or invocation.get("retailor", False)
    reason = "pdf_save_failed" if attempted else "no_resume"
    detail = ("PDF compilation/save did not complete for this run. Fix the LaTeX "
              "or artifact storage and retry; prior artifacts are retained."
              if attempted else "No résumé was saved in this run. Retry preparation.")
    ok, updated = _reader_patch(pk, row, stores, {
        "status": Status.ERROR if failed else Status.FAILED,
        "fail_kind": reason, "error": detail, "fail_reason": detail,
        "gate_reason": "", "gate_pending": {}, "gate_call_id": "",
        "gate_source": "", "apply_requested_at": "",
    })
    if not ok:
        return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}
    invocation["row"] = updated
    return {"result": "error" if failed else "failed", "pk": pk, "reason": reason}


def _pdf_patch(pk: str, stores: Any, changes: dict) -> dict | None:
    from .graph import _PDF_INVOCATION
    invocation = _PDF_INVOCATION.get()
    check = _reader_check(pk, invocation["row"], stores)
    if check:
        return check
    ok, updated = _reader_patch(pk, invocation["row"], stores, changes)
    if not ok:
        return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}
    invocation["row"] = updated
    return None


async def _drive_async(runner: Runner, pk: str, message: Any, stores: Any, *,
                       prepare_only: bool = False, no_enqueue: bool = False,
                       observed: dict | None = None) -> dict:
    from .graph import _PDF_INVOCATION
    row = observed if observed is not None else stores.tracking.get(pk) or {}
    if observed is not None:
        check = _reader_check(pk, row, stores)
        if check or row.get("status") != "tailoring":
            return check or {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}
    if _board_preparation(row):
        prepare_only = no_enqueue = True
    token = _PDF_INVOCATION.set({"row": row, "stores": stores})
    try:
        return await _drive_preparation(runner, pk, message, stores,
                                        prepare_only=prepare_only, no_enqueue=no_enqueue)
    except Exception as exc:
        from .graph import ScorerInvalidOutput, ScorerModelError

        current = _PDF_INVOCATION.get()["row"]
        if (observed is not None and not isinstance(exc, ScorerInvalidOutput)
                and current.get("status") == "tailoring"):
            # Tool/PDF commits advance the witness. Use this invocation's latest
            # witness, never a fresh row that might belong to Skip or Reopen.
            detail = ("Scorer model/request failed; no valid score was received. "
                      "Check the error log, then Reopen & re-score."
                      if isinstance(exc, ScorerModelError)
                      else "Preparation failed — see Logs and retry.")
            _reader_patch(pk, current, stores, {
                "status": Status.ERROR, "match_score": None, "error": detail})
        raise
    finally:
        _PDF_INVOCATION.reset(token)


async def _drive_preparation(runner: Runner, pk: str, message: Any, stores: Any, *,
                       prepare_only: bool = False, no_enqueue: bool = False) -> dict:
    """Run the agent, streaming each step's input/response; catch the human gate.
    In AUTO mode the "Ready to apply?" approval is decided by code (score ≥
    threshold, under the daily cap) and the browser apply runs immediately —
    that's the find-and-apply-while-you-sleep path."""
    from contextlib import aclosing

    from core.events import emit

    # The review-only runner is the only safe agent for a persisted relative
    # posting. Reject before invoking any root/applier tool (including resumes).
    current = stores.tracking.get(pk) or {}
    if _historical_relative(current):
        from .graph import review_agent

        if getattr(runner, "agent", None) is not review_agent:
            return _relative_attention(pk, stores)
        prepare_only = True
    last_author = None
    apply_result: Any = None  # the browser apply() return, if the applier ran it
    auto_go = False           # approval auto-granted → run _apply_direct after close
    board_ready = False
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
                    if author == "scorer" and event.is_final_response():
                        # ADK validated this event's output_key before yielding.
                        # Neither JSON-looking interim text nor an old session
                        # match_score is a current-run scoring witness.
                        payload = event.actions.state_delta.get("match_score")
                        if payload is not None:
                            verdict = _score_gate(pk, payload, stores)
                            if verdict:
                                return verdict

            for call in event.get_function_calls() or []:  # tool call = step INPUT
                if call.name == "ask_human":
                    question = (call.args or {}).get("question", "")
                    if (question.startswith("Ready to apply")
                            and _historical_relative(stores.tracking.get(pk) or {})):
                        return _relative_attention(pk, stores)
                    if question.startswith("Ready to apply"):
                        problem = _pdf_problem(pk, stores)
                        if problem:
                            return problem
                        if _board_preparation(current):
                            board_ready = True
                            break
                    if question.startswith("Ready to apply") and not prepare_only:
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
                    problem = _pdf_patch(pk, stores, dict(
                        status=gate_status, **_clear_infra_failure(pk, stores),
                        gate_reason=reason, gate_pending={"question": question},
                        gate_call_id=call.id, gate_source=author))
                    if problem:
                        return problem
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
                        if prepare_only:
                            # Review-only recovery never invents approval.
                            # An existing current explicit request remains valid.
                            current = stores.tracking.get(pk) or {}
                            if current.get("apply_requested_at") and not no_enqueue:
                                _enqueue_apply(pk, stores, require_request=True)
                        else:
                            _enqueue_apply(pk, stores, require_request=False)
                    emit("gate", pk=pk, agent=author, detail=question, url=row.get("jd_url"),
                         screenshot=_art_url(row.get("screenshot_s3_key")))
                    log.info("gated pk=%s: %s", pk, question)
                    return {"result": "gated", "pk": pk, "question": question}
                emit("action", pk=pk, agent=author, detail=call.name,
                     input=_short(call.args or {}))

            if auto_go or board_ready:
                break  # leave the agent loop; aclosing() closes the generator

            for fr in event.get_function_responses() or []:  # tool result = OUTPUT
                if fr.name == "save_tailored_resume":
                    _observe_pdf(fr.response)
                if fr.name == "apply_to_job":
                    apply_result = fr.response  # capture, to verify a real submit
                emit("result", pk=pk, agent=author, detail=fr.name, output=_short(fr.response))

    if auto_go:
        if _historical_relative(stores.tracking.get(pk) or {}):
            return _relative_attention(pk, stores)
        # DECOUPLED: don't apply inline (a slow browser session would block the
        # evaluate worker from scoring/tailoring the rest of the backlog). Hand the
        # job to the apply queue; the separate apply worker submits it under the cap.
        # Mark it TAILORED (ready to apply) — it LEAVES the `found` pool so it's
        # never re-swept, but does NOT show as "applying" while it just waits in the
        # apply queue. The apply worker flips it to SUBMITTING when it actually
        # starts, so only the jobs truly in-flight read as submitting.
        problem = _pdf_problem(pk, stores) or _pdf_patch(pk, stores, {"status": Status.TAILORED})
        if problem:
            return problem
        stores.queue.enqueue(stores.apply_queue, {"pk": pk})
        emit("running", pk=pk, agent="applier",
             detail="auto-approved (score ≥ threshold) — queued to apply")
        log.info("queued-to-apply pk=%s", pk)
        return {"result": "queued_apply", "pk": pk}

    if prepare_only:
        row = stores.tracking.get(pk) or {}
        if _historical_relative(row):
            return _relative_attention(pk, stores)
        problem = _pdf_problem(pk, stores)
        if problem:
            return problem
        if row.get("apply_requested_at") and not no_enqueue:
            # Check fresh request/status/hold/confirmation inside enqueue, not
            # the row read before preparation or a pre-recovery request.
            queued = _enqueue_apply(pk, stores, require_request=True)
            if queued.get("result") != "queued":
                return queued
            emit("running", pk=pk, agent="applier",
                 detail="Résumé ready — queued to apply, as you asked")
            return {"result": "queued_apply", "pk": pk}
        problem = _pdf_patch(pk, stores, dict(
            status=Status.TAILORED, gate_reason="" if _board_preparation(row) else "approval",
            **_clear_infra_failure(pk, stores),
            gate_pending={} if _board_preparation(row) else {"question": "Ready to apply? Review the résumé first."},
            **({"apply_requested_at": "", "gate_call_id": "", "gate_source": ""}
               if _board_preparation(row) else {})))
        if problem:
            return problem
        emit("gate", pk=pk, detail="Résumé ready for review. Nothing submitted.")
        return {"result": "prepared", "pk": pk}

    # The run finished without gating. Only call it APPLIED if the browser agent
    # actually confirmed a submit — otherwise it FAILED (dead/404 posting, no form
    # found, or the apply step never ran), and we record WHY on the row.
    final = stores.tracking.get(pk) or {}
    outcome = _apply_outcome(apply_result)
    if outcome.get("status") == "applied":
        confirmation = outcome.get("confirmation") or "submitted"
        stores.tracking.set_status(pk, Status.APPLIED, confirmation_id=confirmation,
                                   **_clear_infra_failure(pk, stores))
        if getattr(stores.tracking, 'r', None) is not None:
            submit_hold.clear(pk, tracking=stores.tracking)
        _note_rotation_use(pk, stores)
        emit("applied", pk=pk, detail=confirmation, url=final.get("jd_url"),
             screenshot=_art_url(final.get("screenshot_s3_key")))
        return {"result": "done", "pk": pk}

    if submit_hold.blocked(pk, final, tracking=stores.tracking) or outcome.get('status') == 'uncertain':
        stores.tracking.set_status(
            pk, Status.NEEDS_HUMAN, gate_reason='submit_uncertain', fail_kind='uncertain',
            fail_reason=submit_hold.REASON, gate_pending={'question': submit_hold.REASON})
        return {'result': 'failed', 'pk': pk, 'reason': 'uncertain', 'detail': submit_hold.REASON}
    if outcome.get("reason") == "posting_guard_review_required":
        return _guard_refusal(pk, stores)
    reason = _fail_reason(outcome)
    from .graph import _PDF_INVOCATION
    invocation = _PDF_INVOCATION.get()
    if apply_result is None and invocation is not None:
        committed, _ = _reader_patch(pk, invocation["row"], stores, {
            "status": Status.FAILED, "fail_reason": reason,
            "fail_kind": outcome.get("reason") or ""})
        if not committed:
            return {"result": "conflict", "pk": pk, "reason": "jd_reader_state_changed"}
    else:
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
