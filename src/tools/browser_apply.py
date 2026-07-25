"""The apply entry point.

`apply()` fills and submits one application. The work happens in the owner's own
Chrome (tools.claude_chrome); this module holds the parts that are true whichever
way that goes: the duplicate guard, the résumé filename, the site-quirk rules, and
the event emitter.
"""

from __future__ import annotations

import contextvars
import json

from core.logging import get_logger

log = get_logger(__name__)

# Per-run Chrome profile override. Chrome locks a user_data_dir to a single
# process, so parallel applies (approve-all runs 5 at once) can't share the one
# configured profile. A batch runner sets this contextvar to a distinct dir per
# concurrent worker; a normal single apply leaves it empty and uses the config.
_PROFILE_OVERRIDE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "appliedin_profile_dir", default="")


def set_profile_override(path: str) -> None:
    """Point THIS run's apply browser at a specific profile dir (contextvar-scoped
    to the calling thread/task). Empty string clears it → falls back to config."""
    _PROFILE_OVERRIDE.set(path or "")




_APPLIED_STATUSES = ("applied", "applied_manual")


def _tracking_status(pk: str) -> str:
    """The tracking row's current status for pk, '' when unavailable.

    Best-effort by design: the callers in agent.run checked the row moments ago
    with the same store, so a transient store error here must not kill an apply
    — the portal-side "already applied" check still stands behind it.
    """
    if not pk:
        return ""
    try:
        from core.stores import make_stores
        return str((make_stores().tracking.get(pk) or {}).get("status") or "")
    except Exception:  # noqa: BLE001
        log.debug("could not read tracking row for %s", pk, exc_info=True)
        return ""


def _duplicate_refusal(pk: str, url: str = "") -> dict | None:
    """Refuse loudly when the tracking row already shows this job as applied.

    This is the guard for the incident that motivated it: a job the owner had
    applied to by hand was submitted again by the pipeline. A duplicate
    application under a real person's name is worse than a missed one, so the
    check sits in the tool layer where every engine passes through it — and it
    runs again immediately before every submit click.
    """
    status = _tracking_status(pk)
    if status in _APPLIED_STATUSES:
        detail = (f"REFUSED: this job is already marked '{status}' in tracking — "
                  "not submitting a duplicate application.")
        log.warning("duplicate guard: %s (pk=%s)", detail, pk)
        _emit(pk, "response", agent="applier", url=url, detail=f"🛑 {detail}")
        return {"status": "failed", "reason": "duplicate_application", "detail": detail}
    return None


async def apply(url: str, company: str, facts: dict, model: str, *, pk: str = "",
                jd_text: str = "", resume_tex: str = "", github: str = "",
                resume_path: str = "") -> dict:
    """Fill and SUBMIT this application. Returns one of:
      {status:'applied', confirmation}   {status:'gate', reason, question}
      {status:'failed', reason, detail}  {status:'unknown', detail}

    The application runs in the owner's own Chrome. A driven browser has no
    history and no sessions, so portals challenge it — a security code on one
    posting, "flagged as possible spam" on the next — and a form filled perfectly
    still does not go out. Their browser is not challenged the same way.
    """
    facts = dict(facts)
    full = (facts.get("Full name") or facts.get("Name") or "").strip()
    if full:
        first, *rest = full.split()
        facts.setdefault("First name", first)
        facts.setdefault("Last name", " ".join(rest) or first)

    from .claude_chrome import apply_chrome, available

    ready, why = available()
    if not ready:
        return {"status": "unknown", "detail": why}
    try:
        return await apply_chrome(url, company, facts, model, pk=pk, jd_text=jd_text,
                                  resume_tex=resume_tex, github=github,
                                  resume_path=resume_path)
    except Exception as exc:  # noqa: BLE001
        # The form may already have been filled and submitted, so re-running could
        # send a SECOND application. A duplicate under someone's real name is
        # worse than an unconfirmed one; never retry blind.
        log.exception("apply errored")
        return {"status": "uncertain",
                "detail": f"The browser session failed partway through ({exc}). It was "
                          "NOT retried — check the portal before applying again."}



def _site_rules(url: str, company: str) -> str:
    """Custom instructions for this site, if we've learned any. Never raises —
    a bad note must not stop an application."""
    try:
        from tools.company_skills import instructions_for

        return instructions_for(url, company)
    except Exception:  # noqa: BLE001
        log.debug("could not load company skills", exc_info=True)
        return ""


# --- scripted apply: code drives, the model only maps + writes -----------------

# A browser ERROR page — DNS failure, timeout, connection refused, blank tab.
# This matters because the structural "the form is gone" test cannot tell a
# confirmation screen from a page that never loaded: an error page has no submit
# control, no text inputs and a short body, so a DNS failure was being recorded
# as a submitted application. Positive proof of failure must veto that inference.




def _map_fields(fields: list, facts: dict, company: str, jd_text: str) -> dict:
    """ONE LLM call mapping each form label to a fact KEY, 'ESSAY', or 'SKIP'.
    Values are substituted in code, so the model can never invent an answer."""
    from litellm import completion

    from core.config import get_settings

    def _fline(f: dict) -> str:
        opts = f" (options: {' | '.join(f['options'][:8])})" if f.get("options") else ""
        req = " REQUIRED" if f.get("required") else ""
        return f"- {f.get('label')!r} [{f.get('type')}{opts}]{req}"

    flist = "\n".join(_fline(f) for f in fields
                      if f.get("label") and f.get("type") not in ("file", "submit", "button"))
    keys = "\n".join(f"- {k!r}" for k, v in facts.items() if v)
    prompt = (
        "Map each job-application form field to the candidate fact that answers it.\n\n"
        "FORM FIELDS — a [choice-group] is one QUESTION answered by picking one of "
        f"its options:\n{flist}\n\n"
        f"AVAILABLE FACT KEYS (the only permitted sources):\n{keys}\n\n"
        "Return ONLY a JSON object mapping EVERY field label to exactly one of:\n"
        "- a fact key (verbatim from the list) whose value answers it. For a "
        "choice-group, pick the fact whose value matches one of the options (e.g. a "
        "sponsorship question maps to the sponsorship fact whose value is 'Yes').\n"
        "- \"ESSAY\" for open-ended questions needing prose (tell us about…, why…, "
        "describe…).\n"
        "- \"SKIP\" for fields that are optional AND have no matching fact (EEO "
        "demographics with no fact value, marketing opt-ins), or don't apply.\n"
        "Never map a field to a fact that doesn't answer it."
    )
    model = get_settings().agent_model("") or "openai/gpt-4.1-mini"
    resp = completion(model=model, messages=[{"role": "user", "content": prompt}],
                      response_format={"type": "json_object"})
    try:
        out = json.loads(resp["choices"][0]["message"]["content"])
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


# Scores a document on how much it looks like a JOB APPLICATION form rather than
# a page that merely has inputs. A résumé file input is the strongest signal, an
# email field next; a plain count is not enough, because a careers page carries a
# nav search box and a cookie form while the real application sits in an iframe.





def _clean_resume_copy(src: str, name: str) -> str:
    """Copy the tailored PDF to a temp file named '<Name> Resume.pdf' — a clean
    name for the recruiter, and one that won't trip the upload widget (the raw
    artifact name contains '#'). Falls back to the original path on any error."""
    if not src:
        return ""
    import shutil
    import tempfile
    from pathlib import Path

    try:
        safe = "".join(c for c in name if c.isalnum() or c in " _").strip() or "Resume"
        d = Path(tempfile.gettempdir()) / "appliedin_uploads"
        d.mkdir(parents=True, exist_ok=True)
        dst = d / f"{safe} Resume.pdf"
        shutil.copyfile(src, dst)
        return str(dst)
    except Exception:
        return src


def _emit(pk: str, kind: str, **fields: object) -> None:
    if not pk:
        return
    from core.events import emit
    emit(kind, pk=pk, **fields)
