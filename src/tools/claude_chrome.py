"""Browser control through Claude Code's Chrome integration, and the apply
engine built on it.

One entry point, `run_task`, which runs a task in the owner's own Chrome and
returns the structured JSON report the task was asked to end with. Used by the
apply engine and by discovery.

Requires Claude Code and a direct Anthropic plan; the Chrome integration does not
accept API keys. Callers check `available()` and fall back when it is missing.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from core.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT_S = 600

# Browser tools only. Claude Code can read files, run bash and edit code; none of
# that is needed to read or fill a page, and an agent that can do it anyway is a
# far larger thing to trust with an unattended run than one that can only click.
# Browser tools, plus Write so the task can hand back a structured report. Write
# is scoped by --add-dir to a scratch directory: no repo file is reachable.
ALLOWED_TOOLS = ("mcp__claude-in-chrome", "Write")


def available() -> tuple[bool, str]:
    """Whether Chrome control can run, and why not when it cannot."""
    if not shutil.which("claude"):
        return False, ("Claude Code is not installed — browser control runs through "
                       "it. Install it, or stay on the scripted path.")
    return True, ""


def json_objects(text: str) -> list[dict]:
    """Every balanced JSON object in `text`, in order.

    A regex cannot do this. Reports embed nested objects, and any pattern that
    stops at the first closing brace truncates them into nonsense — which reads
    downstream as "the session ended without saying what happened" for a run that
    actually worked.
    """
    out, depth, start, in_str, esc = [], 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    out.append(json.loads(text[start:i + 1]))
                except Exception:  # noqa: BLE001 — not every brace run is JSON
                    pass
    return out


def _report(stdout: str, key: str) -> dict:
    """The task's structured report, out of whatever else the session said."""
    try:
        env = json.loads(stdout)
        text = env.get("result") or env.get("text") or ""
    except Exception:  # noqa: BLE001 — not JSON: treat it all as the reply
        text = stdout
    if isinstance(text, list):  # some envelopes carry content blocks
        text = " ".join(str(b.get("text", "")) for b in text if isinstance(b, dict))
    for obj in reversed(json_objects(str(text))):  # the LAST report is the final one
        if key in obj:
            return obj
    return {}


async def run_task(task: str, *, report_key: str, model: str = "",
                   timeout_s: int = DEFAULT_TIMEOUT_S,
                   allow_dirs: list[str] | None = None) -> tuple[dict, str]:
    """Run `task` in the owner's Chrome. Returns (report, problem).

    `report_key` is a field the task's closing JSON must contain, so a report can
    be told apart from any other JSON the session happened to print. `problem` is
    empty on success and human-readable otherwise — callers surface it rather than
    guessing what went wrong.
    """
    ok, why = available()
    if not ok:
        return {}, why

    # The report comes back as a FILE the task writes, so the happy path is a
    # json.load of exactly one object rather than a guess at which braces in a
    # page of prose were the result. Brace scanning stays as the fallback for a
    # session that answers in text anyway.
    out_dir = Path(tempfile.mkdtemp(prefix="appliedin_chrome_"))
    out_file = out_dir / "report.json"
    task = (f"{task}\n\nWrite your final JSON report to {out_file} using the Write "
            f"tool — the file must contain that JSON object and nothing else. Then "
            f"repeat it in your reply.")

    cmd = [
        "claude", "--chrome", "-p", task,
        "--output-format", "json",
        "--allowed-tools", *ALLOWED_TOOLS,
        # Every browser action pre-approved. An unattended run cannot answer a
        # permission prompt, and one that stalls on it silently does nothing.
        # Safe only because --allowed-tools already limits the session to the
        # browser: there is no file, bash or edit permission left to bypass.
        "--permission-mode", "bypassPermissions",
    ]
    if model:
        cmd += ["--model", model]
    for d in [*(allow_dirs or []), str(out_dir)]:
        cmd += ["--add-dir", str(d)]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {}, (f"The Chrome session ran past {timeout_s // 60} minutes without "
                    "reporting. Check the tab it left open before retrying.")

    stdout, stderr = out.decode(errors="replace"), err.decode(errors="replace")
    if proc.returncode != 0 and not stdout.strip():
        tail = stderr.strip().splitlines()[-1] if stderr.strip() else "no output"
        return {}, f"claude --chrome failed: {tail[:200]}"

    if out_file.exists():
        try:
            report = json.loads(out_file.read_text())
            if isinstance(report, dict) and report_key in report:
                return report, ""
        except Exception:  # noqa: BLE001 — fall through to reading the reply
            log.debug("report file was not valid JSON — parsing the reply instead")
    report = _report(stdout, report_key)
    if not report:
        return {}, ("The Chrome session ended without a structured result, so what "
                    "it did is unknown. Check the browser.")
    return report, ""


def resume_dirs(resume_path: str) -> list[str]:
    """Directories a session needs read access to in order to upload a résumé."""
    return [str(Path(resume_path).parent)] if resume_path else []


# --- the apply engine ------------------------------------------------------

TIMEOUT_S = 900  # a real form with an upload on a slow portal

def _task(url: str, company: str, facts: dict, resume_path: str, site_rules: str) -> str:
    """What Claude is asked to do. Written for someone acting on another's behalf."""
    resume_line = (f"\nRÉSUMÉ: attach the file at {resume_path} to the résumé field "
                   f"(NOT to a cover-letter field, and NOT to any 'Autofill from "
                   f"resume' box, which is a parser that overwrites answers).\n"
                   if resume_path else "")
    return f"""Fill in and submit this job application on the owner's behalf.

APPLICATION: {url}
COMPANY: {company}

A job page is not an application: the fields may sit behind an "Application" tab
or an Apply button. Open that first, then work through the form.

THE OWNER'S APPROVED ANSWERS — the only facts you may use:
{json.dumps(facts, indent=2)[:8000]}
{resume_line}{site_rules}

RULES — these are not style preferences, they decide whether this application is
honest:

1. Never invent a fact. If a required field has no answer above, do not guess it:
   stop and report it as needing the owner. An employer receives this under their
   real name.
2. Never declare a protected characteristic. Disability, veteran status, race and
   gender questions are voluntary; answer them ONLY from an explicit answer above.
   If there is none, leave them blank. Blank is a valid answer; a guess is not.
3. Sanctions questions ("are you a citizen of, or located in, Cuba, Iran, North
   Korea, Syria…") are always answered with the negative or "None of the above".
4. Free text should be grounded in the owner's real work from the answers above,
   written in plain sentences. Do not use dashes as connectors. Make it
   genuinely useful to a reader deciding whether to interview them.

When the form is complete, submit it and read what the page says afterwards.

Then end your reply with ONLY this JSON object and nothing after it:

{{"outcome": "applied" | "needs_owner" | "blocked",
  "confirmation": "<the page's confirmation wording, if it submitted>",
  "question": "<what to ask the owner, if outcome is needs_owner>",
  "detail": "<what blocked it, if outcome is blocked>",
  "filled": {{"<field label>": "<value you entered>"}}}}

Report what actually happened. "applied" means the page confirmed a submission you
watched go through, not that you clicked Submit and hoped."""


def _verify(report: dict) -> tuple[bool, str]:
    """Re-check the owner's guarantees against what Claude says it entered.

    The other engines enforce these inside the tools, where a model cannot get
    past them. Here the acting happens in another process, so the same rules are
    checked after the fact instead. A rule that is only ever a sentence in a
    prompt is a rule nobody is enforcing.
    """
    from .browser_apply import _is_placeholder, _is_self_id_affirmation

    for label, value in (report.get("filled") or {}).items():
        if _is_self_id_affirmation(str(label), value):
            return False, (f"It declared a protected characteristic ({label[:60]}). "
                           "That answer is the owner's alone, so this is not "
                           "recorded as applied.")
        if _is_placeholder(value):
            return False, f"It entered placeholder text into {label[:60]}."
    return True, ""


async def apply_chrome(url: str, company: str, facts: dict, model: str, *, pk: str = "",
                       jd_text: str = "", resume_tex: str = "", github: str = "",
                       resume_path: str = "") -> dict:
    """Fill and submit `url` in the owner's Chrome. Same shape as every engine."""
    from core.config import get_settings
    from core.events import emit
    from core.stores import make_stores

    from .browser_apply import _clean_resume_copy, _site_rules

    ok, why = available()
    if not ok:
        return {"status": "unknown", "detail": why}

    # Applying twice under a real person's name is worse than not applying, and a
    # portal that accepts the second one leaves them looking careless to an
    # employer they wanted. Checked here rather than trusted to the caller.
    row = (make_stores().tracking.get(pk) or {}) if pk else {}
    if row.get("status") in ("applied", "applied_manual"):
        log.warning("refusing to re-apply to %s — already %s", pk, row["status"])
        return {"status": "unknown",
                "detail": f"Already {row['status'].replace('_', ' ')} — not applying "
                          "again. Clear that status first if this is deliberate."}

    # The tailored PDF is stored as "openai#f763c6b3-….pdf". That is the filename
    # the employer would receive, and the '#' breaks some upload widgets outright,
    # so it is copied to "<Name> Resume.pdf" first — same file, a name a person
    # would have chosen.
    resume_path = _clean_resume_copy(
        resume_path, str(facts.get("Full name") or facts.get("Name") or "Resume"))

    emit("running", pk=pk, agent="browser", url=url,
         detail="🌐 applying in your own Chrome")
    log.info("chrome engine: handing %s to claude --chrome", url)

    report, problem = await run_task(
        _task(url, company, facts, resume_path, _site_rules(url, company)),
        report_key="outcome",
        model=(getattr(get_settings(), "chrome_model", "") or ""),
        timeout_s=TIMEOUT_S,
        allow_dirs=resume_dirs(resume_path),
    )
    if problem:
        return {"status": "unknown", "detail": problem}

    passed, why_not = _verify(report)
    fields = [{"label": k, "value": v}
              for k, v in (report.get("filled") or {}).items()][:40]
    if not passed:
        log.warning("chrome result rejected: %s", why_not)
        return {"status": "failed", "reason": "guardrail",
                "detail": why_not, "fields": fields}

    outcome = str(report.get("outcome") or "").lower()
    if outcome == "applied" and report.get("confirmation"):
        return {"status": "applied",
                "confirmation": str(report["confirmation"])[:120], "fields": fields}
    if outcome == "applied":
        # Claimed without evidence. Two engines have reported a submission that
        # never happened, so the bar is a confirmation the page actually showed.
        return {"status": "unknown",
                "detail": "It reported applying but captured no confirmation from "
                          "the page, so this is not recorded as applied.",
                "fields": fields}
    if outcome == "needs_owner":
        return {"status": "gate", "reason": "unknown_field",
                "question": str(report.get("question") or
                                "This form needs an answer only you can give."),
                "fields": fields}
    return {"status": "failed", "reason": "blocked",
            "detail": str(report.get("detail") or
                          "The application could not be completed.")[:300],
            "fields": fields}
