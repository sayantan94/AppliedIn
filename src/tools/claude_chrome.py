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
import re
import shutil
import tempfile
from pathlib import Path

from core.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT_S = 2700  # 45 minutes

# What a session may say stopped it. Anything else is reported as a plain block
# rather than quietly reinterpreted.
BLOCK_REASONS = frozenset({"application_limit", "already_applied", "captcha", "login"})

# Browser tools only. Claude Code can read files, run bash and edit code; none of
# that is needed to read or fill a page, and an agent that can do it anyway is a
# far larger thing to trust with an unattended run than one that can only click.
# Browser tools, plus Write so the task can hand back a structured report. Write
# is scoped by --add-dir to a scratch directory: no repo file is reachable.
ALLOWED_TOOLS = ("mcp__claude-in-chrome", "Write")


# The browser sessions this process has started. Tracked so a reset can end them:
# emptying the store while a session is still filling a form leaves the owner
# watching a browser do work for a job that no longer exists.
_LIVE: set[int] = set()


def kill_live_sessions() -> int:
    """End every browser session this process started. Returns how many."""
    import os
    import signal

    killed = 0
    for pid in list(_LIVE):
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except ProcessLookupError:
            pass
        except OSError:
            log.warning("could not stop chrome session %s", pid, exc_info=True)
        _LIVE.discard(pid)
    if killed:
        log.info("stopped %d browser session(s)", killed)
    return killed


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
        *cmd,
        # `claude -p` waits on stdin before it starts. From a terminal that costs
        # three seconds and a warning; from the daemon there is no terminal, and
        # the session died two and a half minutes in having produced nothing. The
        # CLI's own advice is to redirect it, so redirect it.
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _LIVE.add(proc.pid)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("chrome session hit the %ds ceiling — killed", timeout_s)
        return {}, (f"The browser session was still working after "
                    f"{timeout_s // 60} minutes and was stopped. Check the tab it "
                    "left open — it may have submitted. Raise the ceiling if this "
                    "portal is simply slow.")

    _LIVE.discard(proc.pid)
    stdout, stderr = out.decode(errors="replace"), err.decode(errors="replace")

    # The REPORT FILE decides, before anything else. A session can do the whole
    # job and still exit oddly — killed while winding down, a broken pipe on a
    # long reply — and treating that as failure throws away work that was already
    # finished, which is how a completed application got reported as "no output".
    report: dict = {}
    if out_file.exists():
        try:
            loaded = json.loads(out_file.read_text())
            if isinstance(loaded, dict) and report_key in loaded:
                report = loaded
        except Exception:  # noqa: BLE001 — fall through to reading the reply
            log.debug("report file was not valid JSON — parsing the reply instead")
    if not report:
        report = _report(stdout, report_key)

    # Only now, with the answer in hand, tidy up a session that outlived it: one
    # was found alive seven minutes after finishing, holding the owner's browser.
    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except TimeoutError:
            log.warning("chrome session ignored terminate — killing it")
            proc.kill()
            await proc.wait()

    if not report:
        # Log everything before deciding it failed. "It produced nothing" is not a
        # diagnosis, and without the session's own words there is nothing to fix.
        log.warning("chrome session gave no report (exit=%s)\n--- stdout ---\n%s\n"
                    "--- stderr ---\n%s", proc.returncode,
                    stdout[-3000:] or "(empty)", stderr[-3000:] or "(empty)")
    if not report and proc.returncode not in (0, None) and not stdout.strip():
        tail = stderr.strip().splitlines()[-1] if stderr.strip() else \
            "it produced no output at all"
        return {}, f"claude --chrome failed (exit {proc.returncode}): {tail[:200]}"
    if not report:
        return {}, ("The Chrome session ended without a structured result, so what "
                    "it did is unknown. Check the browser.")
    # The session's own account of what it did, so the owner can read the whole
    # thing rather than the one-line outcome.
    report.setdefault("_transcript", _transcript(stdout))
    return report, ""


def _transcript(stdout: str) -> str:
    """Everything the session said, for the job log."""
    try:
        env = json.loads(stdout)
        text = env.get("result") or env.get("text") or ""
    except Exception:  # noqa: BLE001
        text = stdout
    if isinstance(text, list):
        text = " ".join(str(b.get("text", "")) for b in text if isinstance(b, dict))
    return str(text)[:20000]


def resume_dirs(resume_path: str) -> list[str]:
    """Directories a session needs read access to in order to upload a résumé."""
    return [str(Path(resume_path).parent)] if resume_path else []


# --- what we refuse to say on the owner's behalf ----------------------------
#
# Each of these exists because the alternative is a real person's name on a claim
# they did not make. The session acts in another process, so these cannot stop the
# keystroke; they stop the result being recorded as a good application, and say
# why. A rule that is only a sentence in a prompt is a rule nobody is checking.

_SANCTIONS_RX = re.compile(
    r"cuba|iran\b|north korea|syria|crimea|donetsk|luhansk|zaporizhzhia|kherson"
    r"|\bbelarus\b|sanction|embargo|export control|restricted (?:country|party)",
    re.I)
# Option wording that SAFELY answers such a question.


_SAFE_OPTION_RX = _SANCTIONS_SAFE_RX = re.compile(
    r"^\s*(?:no|none of the above|not applicable|n/?a|neither|none)\b", re.I)


_PLACEHOLDER_RX = re.compile(
    r"^\s*(?:\{\{.*\}\}|<[A-Z_ ]{3,}>|\[(?:INSERT|YOUR|TODO|DRAFT)[^\]]*\]"
    r"|(?:DRAFT_ESSAY_ANSWER|YOUR_ANSWER_HERE|ANSWER_HERE|TODO|TBD|N/?A_PLACEHOLDER))\s*$",
    re.IGNORECASE)


_SELF_ID_RX = re.compile(
    r"disabilit|protected veteran|veteran status|hispanic|latino"
    r"|race|ethnicit|gender identity|transgender|sexual orientation", re.I)


_SELF_ID_AFFIRM_RX = re.compile(
    r"^\s*yes\b|^\s*i (identify|have|am)\b|i identify as one or more", re.I)


# "I am NOT a protected veteran" and "No, I do not have a disability" are the
# NEGATIVE answers to the same questions, and refusing those would leave a
# required EEO field blank instead of correctly declined.


_SELF_ID_NEGATION_RX = re.compile(
    r"\bnot?\b|\bdon'?t\b|\bdo not\b|\bnone\b|decline|wish to answer", re.I)


def _is_placeholder(value: object) -> bool:
    return isinstance(value, str) and bool(_PLACEHOLDER_RX.match(value))


def _is_self_id_affirmation(label: str, value: object = "") -> bool:
    """True when ticking this would DECLARE a protected characteristic."""
    text = f"{label} {value}".strip()
    head = str(label).strip()
    if _SELF_ID_NEGATION_RX.search(head):
        return False
    return bool(_SELF_ID_RX.search(text) and _SELF_ID_AFFIRM_RX.search(head))


def _safe_sanctions_answer(question: str, values: list) -> list:
    """Force a sanctions question to its negative answer.

    Keeps only options that are plainly negative. If the caller proposed an
    affirmative one, it is dropped and 'None of the above' / 'No' is used, so a
    mis-mapped answer cannot tick "citizen of a sanctioned country".
    """
    if not _SANCTIONS_RX.search(question or ""):
        return values
    safe = [v for v in values if _SANCTIONS_SAFE_RX.match(str(v))]
    if safe != list(values):
        log.warning("sanctions question — forcing the negative answer for %r "
                    "(proposed %r)", (question or "")[:70], values)
    return safe or ["None of the above", "No", "Not applicable"]


# Finds a sanctions checkbox/radio group and its SAFE option, wherever it sits.
# Structure varies (fieldset+legend, a bare paragraph then labels, a div), and
# the generic reader does not always group it — so this works off the OPTION
# text, which is consistent, rather than the question's markup.



def guard_value(label: str, value: object) -> tuple[str | None, str | None]:
    """(value_to_write, refusal_note) — every promise, applied to one answer.

    - placeholder tokens are never typed;
    - a protected-status AFFIRMATION is never made for the owner, while the
      negative answers pass through, because refusing those leaves a required
      EEO field blank rather than correctly declined;
    - a sanctions question takes its safe option whatever was proposed.
    """
    if _is_placeholder(value):
        return None, f"{label}: placeholder text"
    if _is_self_id_affirmation(label, value) or _is_self_id_affirmation(str(value)):
        return None, f"{label}: self-identification is the owner's to answer"
    safe = _safe_sanctions_answer(label, [str(value)])
    if safe and str(safe[0]) != str(value):
        return str(safe[0]), f"{label}: forced to the safe option {safe[0]!r}"
    return str(value), None


# --- the apply engine ------------------------------------------------------

# A long form on a slow portal genuinely takes this long, and cutting off a
# session that is filling correctly wastes the whole run — CoreWeave was
# working when it was killed. Better to wait than to redo.
TIMEOUT_S = 2700  # 45 minutes

def _certain(facts: dict) -> str:
    """The handful of answers that are the same on every form, pre-mapped.

    Making a model work out where the owner's own name goes, on every single
    application, is time the owner spends watching. These are certain, so they
    are handed over already decided and the deliberation is saved for the
    questions that actually need it.
    """
    want = ("Full name", "First name", "Last name", "Email", "Phone",
            "Phone number", "LinkedIn", "GitHub", "Website", "Current location",
            "Where are you currently located?", "Are you authorized to work in "
            "the country where the job is located?", "Will you now or in the "
            "future require sponsorship for an employment visa?")
    lines = [f"  {k} → {facts[k]}" for k in want if str(facts.get(k, "")).strip()]
    return "\n".join(lines) or "  (nothing pre-mapped for this form)"


def _stage_resume(src: str, owner: str) -> str:
    """Put the tailored résumé alone in its own directory, named for the owner.

    The upload is the step that fails most, so everything avoidable is removed
    from it: the raw artifact is called "coreweave#4691973006.pdf" — a name no
    recruiter should see, and one whose "#" breaks some upload widgets outright —
    and the directory it lives in holds every other job's résumé too. Here there
    is one file, with the name a person would have chosen, in a directory that
    contains nothing else to pick by mistake.
    """
    import shutil
    import tempfile

    if not src or not Path(src).exists():
        return ""
    safe = "".join(c for c in owner if c.isalnum() or c in " _").strip() or "Resume"
    try:
        d = Path(tempfile.mkdtemp(prefix="appliedin_resume_"))
        dst = d / f"{safe} Resume.pdf"
        shutil.copyfile(src, dst)
        return str(dst)
    except OSError:
        log.warning("could not stage the résumé — using it where it is", exc_info=True)
        return src


def direct_board_url(url: str) -> str:
    """The board's OWN url when `url` is a company page wrapping one.

    A company careers page often embeds Greenhouse or Lever in a CROSS-ORIGIN
    iframe. Text can still be typed by coordinate, but a file upload cannot reach
    an input inside that frame — which is exactly how a CoreWeave application got
    six fields in and no résumé. The same form served from the board's own domain
    is same-origin and behaves normally, so go there instead of fighting it.
    """
    from urllib.parse import parse_qs, urlparse

    u = urlparse(url)
    q = parse_qs(u.query)
    jid = (q.get("gh_jid") or [""])[0].strip()
    board = (q.get("board") or [""])[0].strip()
    if jid.isdigit() and board:
        return f"https://job-boards.greenhouse.io/{board}/jobs/{jid}"
    return ""


def _task(url: str, company: str, facts: dict, resume_path: str, site_rules: str) -> str:
    """What Claude is asked to do. Written for someone acting on another's behalf."""
    resume_line = (f"""
RÉSUMÉ — attach {resume_path}

Use the file-upload tool and point it straight at the form's file input. That
tool sets the file on the input directly; it does not need a dialog.

Do NOT click "Attach", "Upload a file", "Choose file" or a drag-and-drop area to
get there. Those open the operating system's own file chooser, which is not part
of the page: you cannot see or control it, and while it is open the browser stops
responding to everything else. If one does open, press Escape and go back to
uploading against the input.

The input is often hidden behind that button rather than absent — target it even
when it is not visible. Attach to the field for a Resume or CV, never to a cover
letter, and never to an "Autofill from resume" box, which is a parser that
re-populates the form and overwrites answers already entered.

Then confirm the filename is showing on the page. If it is not, the upload did
not happen: try again rather than carrying on, because an application submitted
without a résumé is worse than one that stopped.
""" if resume_path else "")

    return f"""Fill in and submit this job application on the owner's behalf.

APPLICATION: {url}
COMPANY: {company}

A job page is not an application: the fields may sit behind an "Application" tab
or an Apply button. Open that first, then work through the form.

If the form turns out to be embedded from another domain (a Greenhouse, Ashby,
Lever or Workday iframe inside the company's own page), open that board's URL
DIRECTLY in a new tab and fill it there. You can usually type into an embedded
form, but a file upload cannot reach inside a cross-origin frame, so the résumé
will silently fail to attach and the application will go out without it. The
board's own page has the identical form and behaves normally.

START WITH THESE — they are the same on every application, so fill them without
deliberating and spend your attention on the rest of the form:
{_certain(facts)}

EVERY APPROVED ANSWER — the only facts you may use:
{json.dumps(facts, indent=1)[:9000]}
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
5. Remember to click each text box / radio button / checkbox after selecting

WORK IN PASSES, NOT FIELD BY FIELD. Read the whole form once and note what every
field needs. Then fill everything you can in as few actions as possible: all the
text boxes, then all the dropdowns and radio groups, then the résumé. Check the
result once at the end rather than after each field. Every extra look at the page
costs the owner time they are sitting through, and a form is not twenty decisions
— it is one decision about twenty fields.

When the form is complete, submit it and then find out what happened. Submitting
often navigates somewhere: wait for the page to settle, read the page you have
ENDED UP ON, and scroll it — a confirmation is frequently on that destination
rather than where you clicked. Only report "blocked" once you have actually looked
at where you landed. If you genuinely cannot find a confirmation, say so plainly
and say what the final page was, so the owner knows where to check.

When you are completely finished, CLOSE the tabs you opened. The owner is working
in this browser; leaving your tabs behind makes them clean up after you.

Then end your reply with ONLY this JSON object and nothing after it:

{{"outcome": "applied" | "needs_owner" | "blocked",
  "blocked_by": "application_limit" | "already_applied" | "captcha" | "login" | "other",
  "confirmation": "<the page's confirmation wording, if it submitted>",
  "question": "<what to ask the owner, if outcome is needs_owner>",
  "detail": "<what blocked it, in your own words, if outcome is blocked>",
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

    from .browser_apply import (
        _duplicate_refusal,
        _site_rules,
    )

    ok, why = available()
    if not ok:
        return {"status": "unknown", "detail": why}

    refusal = _duplicate_refusal(pk, url)
    if refusal:
        return refusal

    # The tailored PDF is stored as "openai#f763c6b3-….pdf". That is the filename
    # the employer would receive, and the '#' breaks some upload widgets outright,
    # so it is copied to "<Name> Resume.pdf" first — same file, a name a person
    # would have chosen.
    resume_path = _stage_resume(
        resume_path, str(facts.get("Full name") or facts.get("Name") or "Resume"))

    if (direct := direct_board_url(url)) and direct != url:
        log.info("using the board's own URL instead of the embed: %s", direct)
        emit("running", pk=pk, agent="browser", url=url,
             detail=f"↪ the form is embedded — applying on the board directly: {direct}")
        url = direct

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
        # Say it on the board. A run that failed for a reason the owner could fix
        # — Chrome closed, the extension disconnected, a ceiling hit — is useless
        # to them as a status word they have to go digging behind.
        emit("error", pk=pk, agent="browser", url=url, detail=problem)
        return {"status": "unknown", "detail": problem}

    # The whole account, into the job's feed. The one-line outcome says whether it
    # worked; this says what it did, which is what you want when it did not.
    transcript = str(report.pop("_transcript", "") or "").strip()
    if transcript:
        for para in [p.strip() for p in transcript.split("\n\n") if p.strip()][:40]:
            emit("running", pk=pk, agent="browser", url=url, detail=para[:400])

    return classify(report)


def classify(report: dict) -> dict:
    """A Chrome session's report, turned into a result — or refused.

    Kept separate from the run so the rules can be tested without a browser.
    Two engines have reported a submission that never happened: one read a DNS
    error page as a confirmation, one read "Thank you for applying" off a page
    whose form was still empty. So a claim of success is not enough. The page has
    to have said something, and what was entered has to pass the same guards that
    applied while entering it.
    """
    passed, why_not = _verify(report)
    fields = [{"label": k, "value": v}
              for k, v in (report.get("filled") or {}).items()][:40]
    if not passed:
        log.warning("chrome result rejected: %s", why_not)
        return {"status": "failed", "reason": "guardrail",
                "detail": why_not, "fields": fields}

    outcome = str(report.get("outcome") or "").lower()
    if outcome == "applied":
        confirmation = str(report.get("confirmation") or "").strip()
        if not confirmation:
            return {"status": "unknown",
                    "detail": "It reported applying but captured no confirmation "
                              "from the page, so this is not recorded as applied.",
                    "fields": fields}
        return {"status": "applied", "confirmation": confirmation[:120],
                "fields": fields}
    if outcome == "needs_owner":
        return {"status": "gate", "reason": "unknown_field",
                "question": str(report.get("question") or
                                "This form needs an answer only you can give."),
                "fields": fields}

    # Name the block precisely. "You have already applied" and "5 applications per
    # 180 days" are different problems with different answers, and reporting both
    # as a generic failure sends the owner to the portal to work out which.
    # The session read the page and says WHY in a field of its own. Matching its
    # prose with a regex would be guessing at an answer it already gave, and the
    # guess would be wrong the moment it phrases a cap in its own words — which is
    # exactly what happened.
    detail = str(report.get("detail") or "The application could not be completed.")
    said = str(report.get("blocked_by") or "").strip().lower()
    reason = said if said in BLOCK_REASONS else "blocked"
    return {"status": "failed", "reason": reason, "detail": detail[:300],
            "fields": fields}
