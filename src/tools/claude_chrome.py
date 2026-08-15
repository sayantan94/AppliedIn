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


# The browser sessions this process has started, each tagged with WHAT it is
# doing. Tracked so a reset or a stop can end them.
#
# The tag is the point. Stopping a discovery run must not kill an application
# that happens to be in flight: the crawl is disposable and the apply is a form
# already half filled under someone's real name. An untagged set could only offer
# "kill everything", which is the wrong answer to "stop this discovery".
_LIVE: dict[int, str] = {}

# Sessions run CONCURRENTLY. There was briefly a semaphore here serialising them,
# on the theory that two sessions on one browser corrupt each other. That theory
# was wrong: the failures that prompted it happened with a single session running,
# and two sessions launched simultaneously both complete normally — each gets its
# own tab group. Serialising cost real throughput, since a job read would queue
# behind an apply that is allowed to run for 45 minutes. Concurrency is bounded by
# the lane counts in daemon.py, which is the right place for it.

# Said by the browser tools when the tab cannot be driven. Environment, not job:
# nothing was filled and the same posting works once the tab is clean, so it must
# never be recorded as a failed application. The usual cause is an extension frame
# injected into the page — see site-quirks/oracle.md.
_CONFLICT_MARKERS = (
    "cannot access a chrome",          # …-extension:// URL of different extension
    "url of different extension",
    "cannot access contents of the page",
    "different extension",
    # Our OWN wording, below. This layer rewrites the raw browser error into
    # something readable, and the apply layer then re-reads that text to decide
    # whether to re-queue — so the friendly message has to stay recognisable too.
    # Without this the detection worked and the job was still marked failed.
    "would not accept clicks on the tab",
)

# The one phrase the message above must contain, kept next to the markers so the
# two cannot drift apart.
CONFLICT_MESSAGE = ("Chrome would not accept clicks on the tab the session was "
                    "using, so the page could be read but not driven. Nothing was "
                    "filled or submitted.")

# Chrome refuses to let the extension act on a tab owned by a DIFFERENT extension,
# and a tab that has just been created is exactly that until its navigation lands:
# reads of the old page keep working while every click fails, which reads as a
# broken page rather than a mis-targeted tab. So: never act on a tab before its
# URL is the page you want, and treat that error as "wrong tab", not "bad page".
TAB_HYGIENE = """BEFORE YOU CLICK ANYTHING, make sure you are on the right tab.
A tab you just created is not yet the page you asked for. Navigate it, wait for
the load, and confirm its URL is the site you intend before any click, keypress,
screenshot or JavaScript. Never act on a tab whose URL starts with
chrome-extension://, chrome://, or about: — those are browser and extension pages,
not the application.

If any action fails with "Cannot access a chrome-extension:// URL of different
extension" (or a similar access error), you are pointed at the wrong tab; the page
itself is fine. Do NOT give up and do NOT report the posting as unfillable. List
the tabs, switch to the one whose URL is the application, and continue. If no such
tab exists, navigate an existing ordinary tab to the application URL rather than
opening a new one. Only report a browser problem if that recovery also fails.
"""


# Discovery YIELDS to an application. Two sessions reading pages coexist fine, and
# that is why there is no blanket lock — but a crawl is not a reader: it opens
# tabs, types into a search box, scrolls and navigates, all in the same Chrome
# where a form is being filled. It steals the active tab, and the apply is left
# screenshotting somebody else's page.
#
# The two are not equal, which is what makes this easy to decide. A crawl is
# disposable: delay it and nothing is lost but a few minutes. An application is a
# form part filled under a real name, and disturbing it can leave a submission
# nobody can account for. So the crawl waits.
#
# Only "crawl" waits. A "jd" read is part of an apply's own flow, so making it
# wait for other applications would serialise applies and undo the concurrency
# that is wanted.
_YIELDS_TO_APPLY = frozenset({"crawl"})
_YIELD_POLL_S = 5
_YIELD_MAX_S = 1800     # give up waiting after 30 minutes and run anyway


def applies_running() -> int:
    """How many applications are being filled right now."""
    return sum(1 for k in _LIVE.values() if k == "apply")


# The openings of every reason _envelope_reason can produce. These describe the
# INFRASTRUCTURE rather than the application, so they are complete sentences on
# their own and must not be introduced as "the agent finished without confirming
# a submission" — the agent did not finish, it was cut off.
INFRA_OPENINGS = (
    "Rate limited by the model API",
    "The model API returned",
    "The session was cut off",
    "The daemon was restarted",
)


def is_infrastructure(detail: str) -> bool:
    """Whether a failure detail is about the plumbing, not the application."""
    return (detail or "").strip().startswith(INFRA_OPENINGS)


# SIGTERM. Every cluster of these in the log is followed within seconds by
# "daemon up": they are in-flight sessions killed by a restart or by Stop, not
# anything that happened on the page.
_SIGTERM = (143, -15)


def _envelope_reason(stdout: str, returncode: int | None = None) -> str:
    """A plain sentence for how the session ENDED, from its own result envelope.

    `claude -p --output-format json` closes with a record carrying `subtype` and
    `terminal_reason`. When there is no report, that record is the only account of
    what happened, and it distinguishes a run that was cut off from one that
    genuinely could not do the job.
    """
    import json as _json

    # Match on ANY of the envelope's own markers, not one key. Requiring "subtype"
    # meant an envelope that carried only terminal_reason was skipped, and the
    # session was reported as unexplained when the explanation was right there.
    marks = ('"subtype"', '"terminal_reason"', '"api_error_status"', '"num_turns"')
    tail = ""
    for line in reversed(stdout.strip().splitlines()):
        t = line.lstrip()
        if t.startswith("{") and any(m in t for m in marks):
            tail = line
            break
    if not tail:
        return ""
    try:
        env = _json.loads(tail)
    except Exception:  # noqa: BLE001
        return ""

    reason = str(env.get("terminal_reason") or "")
    subtype = str(env.get("subtype") or "")
    turns = env.get("num_turns")
    status = env.get("api_error_status")

    # Checked before the envelope's own fields: a killed session still writes
    # `aborted_streaming`, so reading only the envelope reports a restart as a
    # connection failure and sends the owner looking at their network.
    if returncode in _SIGTERM:
        return ("The daemon was restarted (or Stop was pressed) while this was "
                "running" + (f", after {turns} steps" if turns else "")
                + ". Nothing was submitted, and it goes back on the queue.")

    # The API status is checked FIRST because it is the more specific fact. Almost
    # every aborted stream here is a 429 underneath, and reporting it as a generic
    # connection failure sends the owner looking at their network when the answer
    # is that too much was being asked at once.
    if status == 429:
        return ("Rate limited by the model API partway through"
                + (f", after {turns} steps" if turns else "")
                + ". Nothing was submitted. This retries by itself; if it keeps "
                  "happening, lower how many applications run at once.")
    if status:
        return (f"The model API returned {status} partway through. Nothing was "
                "submitted; this retries by itself.")
    if reason == "aborted_streaming" or subtype == "error_during_execution":
        return ("The session was cut off partway through"
                + (f" after {turns} steps" if turns else "")
                + ". That is a connection failure rather than a problem with the "
                  "application, so nothing was submitted and it is worth retrying.")
    if subtype and subtype != "success":
        return f"The session ended as {subtype!r} without reporting what it did."
    return ""


def _is_browser_conflict(text: str) -> bool:
    """True when a session failed because another one held the browser."""
    low = (text or "").lower()
    return any(m in low for m in _CONFLICT_MARKERS)


def kill_live_sessions(kind: str = "") -> int:
    """End browser sessions this process started; `kind` limits it to one sort
    ("apply", "crawl", "jd"). Empty means all of them, which is what a full reset
    wants and what a scoped stop must never use."""
    import os
    import signal

    killed = 0
    for pid in [p for p, k in list(_LIVE.items()) if not kind or k == kind]:
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except ProcessLookupError:
            pass
        except OSError:
            log.warning("could not stop chrome session %s", pid, exc_info=True)
        _LIVE.pop(pid, None)
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
                   allow_dirs: list[str] | None = None,
                   kind: str = "") -> tuple[dict, str]:
    """Run `task` in the owner's Chrome. Returns (report, problem).

    `report_key` is a field the task's closing JSON must contain, so a report can
    be told apart from any other JSON the session happened to print. `problem` is
    empty on success and human-readable otherwise — callers surface it rather than
    guessing what went wrong.
    """
    ok, why = available()
    if not ok:
        return {}, why

    if kind in _YIELDS_TO_APPLY and applies_running():
        waited = 0
        log.info("%s session waiting: %d application(s) are being filled",
                 kind, applies_running())
        while applies_running() and waited < _YIELD_MAX_S:
            await asyncio.sleep(_YIELD_POLL_S)
            waited += _YIELD_POLL_S
        if applies_running():
            # Running anyway rather than dropping the work: half an hour of
            # applications means something is wedged, and a scan that silently
            # never happens is worse than one that risks a collision.
            log.warning("%s session waited %ds and is starting while an application "
                        "is still running", kind, waited)
        else:
            log.info("%s session waited %ds for the browser", kind, waited)

    return await _run_task_impl(task, report_key=report_key, model=model,
                                timeout_s=timeout_s, allow_dirs=allow_dirs, kind=kind)


async def _run_task_impl(task: str, *, report_key: str, model: str = "",
                         timeout_s: int = DEFAULT_TIMEOUT_S,
                         allow_dirs: list[str] | None = None,
                         kind: str = "") -> tuple[dict, str]:
    """run_task's body, split out so tests can stand in for the subprocess."""
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
    _LIVE[proc.pid] = kind or "other"
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

    _LIVE.pop(proc.pid, None)
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
        # The envelope says WHY, and it is usually not the agent's doing. A session
        # whose stream aborted is a transport failure: the run was cut off partway,
        # so "it did not report" describes the symptom and blames the wrong thing.
        # Naming it matters because the two need opposite responses — retry the
        # transport, investigate the page.
        why = _envelope_reason(stdout, proc.returncode)
        if "Rate limited" in why:
            # Same banner the orchestration rate limit uses. A single card saying
            # "rate limited" reads as bad luck; the banner is what makes a pattern
            # visible, and the pattern is the thing worth acting on.
            try:
                from core import flags

                flags.note_llm_error(
                    "browser", "Applications are being rate limited by the model "
                    "API and cut off partway through. They retry by themselves. "
                    "Lower how many run at once in the apply queue if it persists.")
            except Exception:  # noqa: BLE001 — a banner must never break an apply
                log.debug("could not record the rate limit banner", exc_info=True)
        return {}, (why or "The Chrome session ended without a structured result, "
                           "so what it did is unknown. Check the browser.")

    # A session can produce a perfectly well-formed report whose CONTENT is "I
    # could read the page but every click failed". That is the browser being held
    # by something else, not the posting being unfillable, so say so plainly
    # instead of letting it be recorded against the job.
    if _is_browser_conflict(json.dumps(report)) or _is_browser_conflict(stdout):
        # Log the session's OWN account, not just the verdict. "Browser conflict"
        # names a symptom; which tab and which extension it tripped on is the only
        # thing that identifies the cause, and without it this gets diagnosed by
        # guesswork.
        log.warning("browser conflict (report_key=%s)\n--- report ---\n%s\n"
                    "--- session output ---\n%s",
                    report_key, json.dumps(report)[:4000], stdout[-4000:] or "(empty)")
        return {}, CONFLICT_MESSAGE
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
    import re as _re
    from urllib.parse import parse_qs, urlparse

    u = urlparse(url)
    q = parse_qs(u.query)
    jid = (q.get("gh_jid") or [""])[0].strip()
    board = (q.get("board") or [""])[0].strip()
    if jid.isdigit() and board:
        return f"https://job-boards.greenhouse.io/{board}/jobs/{jid}"

    # Oracle, for a different reason worth knowing. The posting page carries a
    # "LinkedIn Embedded Content" frame; an ad blocker replaces that with a page
    # of its OWN extension, and Chrome then refuses to let another extension act
    # on the tab at all — "Cannot access a chrome-extension:// URL of different
    # extension". The page still READS fine, so it looks like the posting is
    # unfillable rather than the tab being poisoned. Oracle's own candidate site
    # carries the identical application and no social embed, so go straight there.
    if u.netloc.endswith("careers.oracle.com"):
        if m := _re.search(r"/job/(\d+)", u.path):
            return ("https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience"
                    f"/en/sites/jobsearch/job/{m.group(1)}/apply/email")
    return ""


def _history_block(history: str) -> str:
    """The employment and education block, or nothing when there is no résumé."""
    if not history:
        return ""
    return ("\nEMPLOYMENT AND EDUCATION — some forms make you RETYPE this into "
            "structured fields rather than accepting the résumé file. Use exactly "
            "what is below: these employers, titles, dates and degrees, and these "
            "bullets for any role description box. Never invent an entry, a date or "
            "a description to fill a field. If a form needs something that is not "
            "here, ask rather than guess.\n" + history + "\n")


def _skills_block(skills: str) -> str:
    """The choose-your-skills block, or nothing when there is no résumé to back it."""
    if not skills:
        return ""
    return ("\nSKILLS THE RÉSUMÉ EVIDENCES — when a form asks you to CHOOSE skills "
            "rather than type them, these are the ones the owner can defend. Pick "
            "every one the form offers that appears here, and add the ones it "
            "missed. Never pick a skill that is not in this list.\n" + skills + "\n")


def _braced(text: str, start: int) -> tuple[str, int]:
    """The contents of the {...} beginning at `start`, and the index after it.

    A regex cannot do this: résumé bullets nest two and three levels deep
    (\\textbf inside \\resumeItem inside \\small), and a pattern that allows one
    level silently drops exactly the richest lines — which are the ones worth
    having.
    """
    if start >= len(text) or text[start] != "{":
        return "", start
    depth, i = 0, start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
        i += 1
    return text[start + 1:], len(text)


def _args(text: str, macro: str) -> list[list[str]]:
    """Every occurrence of `macro` with its brace arguments, braces matched."""
    out, i, tok = [], 0, "\\" + macro
    while (j := text.find(tok, i)) != -1:
        k = j + len(tok)
        args = []
        while k < len(text) and text[k] == "{":
            arg, k = _braced(text, k)
            args.append(arg)
        out.append(args)
        i = k if k > j else j + len(tok)
    return out


def _resume_history(resume_tex: str) -> str:
    """Employment and education from the tailored résumé, as plain text.

    Workday and its relatives do not just take a résumé file: they ask the
    candidate to RETYPE their history into structured fields, one entry per role,
    with separate date segments, and that page is what a recruiter's search
    queries. The session cannot read the résumé file — its tools are the browser
    and Write — so without this it reached that page with nothing to type and left
    it empty or, worse, guessed.

    Bullets are included because those forms have a "Role Description" box, and a
    description invented to fill it is a claim nobody can stand behind.
    """
    if not resume_tex:
        return ""

    def _clean(t: str) -> str:
        t = re.sub(r"\\href\{[^}]*\}\{([^}]*)\}", r"\1", t)
        t = re.sub(r"\\[a-zA-Z]+\*?", " ", t)
        t = t.replace("{", "").replace("}", "").replace("---", "—").replace("--", "–")
        t = t.replace("\\&", "&").replace("\\%", "%").replace("\\$", "$")
        return re.sub(r"[ \t]+", " ", t).strip(" ,;")

    out: list[str] = []
    for label, head in (("EMPLOYMENT", "Experience"), ("EDUCATION", "Education")):
        m = re.search(rf"\\section\{{{head}\}}(.*?)(?=\\section\{{|\Z)", resume_tex, re.S)
        if not m:
            continue
        rows: list[str] = []
        for block in re.split(r"\\resumeSubheading(?=\{)", m.group(1))[1:]:
            head_args = _args("\\resumeSubheading" + block.split("\\resumeItem")[0],
                              "resumeSubheading")
            parts = [c for a in (head_args[0] if head_args else [])[:4] if (c := _clean(a))]
            if not parts:
                continue
            rows.append("  " + " | ".join(parts))
            for args in _args(block, "resumeItem")[:6]:
                if args and (txt := _clean(args[0])):
                    rows.append("      - " + txt[:300])
        if rows:
            out.append(label + "\n" + "\n".join(rows))
    return "\n\n".join(out)[:4500]


def _resume_skills(resume_tex: str) -> str:
    """The Skills section of the tailored résumé, as plain text.

    Some forms ask the candidate to CHOOSE skills from a list rather than type
    them (Apple's "Add related skills to your profile" is the clearest case, and
    it drives how the profile is matched against the posting). The session cannot
    read the résumé file — its tools are the browser and Write, nothing else — so
    without this it would be picking skills by guesswork, and a skill is a claim
    about a person rather than a keyword to match.
    """
    if not resume_tex:
        return ""
    m = re.search(r"\\section\{Skills\}(.*?)(?=\\section\{)", resume_tex, re.S)
    if not m:
        return ""
    body = re.sub(r"\\textbf\{([^}]*)\}", r"\1", m.group(1))     # keep category labels
    body = re.sub(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?", " ", body)      # drop other commands
    body = body.replace("{", " ").replace("}", " ").replace("\\", " ")
    body = re.sub(r"[ \t]+", " ", body)
    return "\n".join(ln.strip() for ln in body.splitlines() if ln.strip())[:1200]


def verify_url(pk: str, company: str = "") -> str:
    """Where a session waits for a one-time code for THIS job.

    A page rather than a file: the session's tools are the browser plus Write, so
    it cannot read from disk, but it can read a page. Same port the dashboard is
    served on, since it is the same process.
    """
    import os
    from urllib.parse import quote

    port = os.environ.get("APPLIEDIN_WEB_PORT", "8787")
    q = f"?company={quote(company)}" if company else ""
    return f"http://127.0.0.1:{port}/verify/{quote(pk, safe='')}{q}"


def _task(url: str, company: str, facts: dict, resume_path: str, site_rules: str,
          resume_skills: str = "", resume_history: str = "", wait_url: str = "") -> str:
    """What Claude is asked to do. Written for someone acting on another's behalf."""
    wait_block = (f"""
ONE-TIME CODE CALLBACK — available, but only when this site's rules say to use it
Some portals email a code before they will continue. Nothing here can read that
email, so there is a callback the owner answers:

  {wait_url}

Open it in a NEW tab (leave the application tab alone) and reload it. It reads
WAITING until the owner supplies the code, then READY with `CODE: <value>`, or
EXPIRED. Reading the page is what tells them you are waiting, so keep reloading.
Take the value back to the application tab and type it in. Never type a code into
any page other than the employer's own form.

DO NOT use this because a page merely looks like it wants a code. Use it only
where the rules below for this specific site tell you to, and otherwise report a
block as usual. Waiting costs the owner minutes of a browser doing nothing, so
waiting for something nobody will send is worse than stopping.
""" if wait_url else "")
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

{TAB_HYGIENE}
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
{resume_line}{wait_block}{site_rules}
{_skills_block(resume_skills)}{_history_block(resume_history)}

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
4. Free text is grounded in the owner's real work from the answers above, written
   in plain sentences. Do not use dashes as connectors. Make it genuinely useful
   to a reader deciding whether to interview them.

   The answers above are SOURCE MATERIAL, not text to paste. Several of them are
   long stored paragraphs about one project. Pasting one whole into a question
   that did not ask about that project is the single worst thing you can write:
   it reads as a form letter, and a reader stops at the first sentence that is
   plainly not about them. So:

   - Answer the question that was actually asked, in its own words.
   - Choose only the material that fits THIS role, which is on the page in front
     of you. A project is worth naming when its subject matches what this team
     works on. When it does not, leave it out; the professional record is the
     stronger answer nearly every time.
   - Never open with a project the question did not ask about.
   - Do not end with a sentence of enthusiasm that would be true of any employer
     ("I am excited about X because it combines exactly what I do"). If the
     interest is real it is specific to something on this page; if it is not
     specific, write nothing rather than filler.
   - Three or four sentences is usually right. Long is not thorough.

   When the field is an open invitation rather than a question — "Additional
   information", "anything else you want us to know", an optional note — fill it
   rather than leaving it blank, and use it to make the case the form never asked
   for: what this team gets by hiring the owner. One sentence on the fit, named
   concretely with its scale or outcome; one on what the team can do sooner
   because he is on it; and, only if the field asks for motivation, one on why
   this team's problem is the one he wants next. Do not summarise the résumé they
   already have.
5. When a question offers OPTIONS and several approved answers would each be
   true, pick the one closest to THIS role, which is described on the page in
   front of you. The owner's answers deliberately cover more than one option for
   the same question — a preferred technical domain, an area of expertise — and
   choosing by relevance is the difference between an answer and a default. Two
   rules bound it: the value you select must match one of the options offered,
   and it must be one of the approved answers. Never pick an option the owner has
   not claimed in order to fit the posting better.
   Worked example. "What technical domain do you prefer to work in and have most
   expertise with?" with options Front End, Full Stack, Back End, Infrastructure,
   Database Operations / SRE, Low-Level Systems Development, Distributed Systems:
   for a platform or reliability role choose Infrastructure, for a services or
   API role Back End, and for a role about scale, consistency or data movement
   Distributed Systems. All three are approved answers; the posting decides.

6. Remember to click each text box / radio button / checkbox after selecting
7. If the portal offers "Continue with Google" (or Sign in with Google), PREFER IT
   over creating an account, but ONLY when the Google account it is already signed
   in as matches the email this application is going out under, shown in the
   answers above. Check the account it names before clicking through.
   If it names a different account, or offers an account chooser, or asks for a
   password: STOP and report it. Do not switch accounts, do not sign in, do not
   create one. An application sent from the wrong address reaches the wrong inbox
   and cannot be recalled, and a password is never yours to type.

NEVER DECIDE AN APPLICATION IS POINTLESS BEFORE TRYING IT
A limit you met somewhere else is not evidence about this employer. Caps are set
per company, so one at Ramp says nothing about Snowflake even on the same job
board, and one you hit earlier today says nothing about the company in front of
you now. The same goes for anything you recall from a previous session: it
described a different application at a different moment.

So do not skip, and do not report a block, on the strength of a pattern. Fill the
form and submit it. If this employer refuses, report that refusal and quote what
it actually said. A refusal you predicted is not a refusal, it is an application
the owner never got.

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
        _task(url, company, facts, resume_path, _site_rules(url, company),
              _resume_skills(resume_tex), _resume_history(resume_tex),
              # Only for the sites that actually ask. Handing every run a "wait
              # for a code" procedure invites it to be used where no code exists,
              # and a session that decides to wait for something nobody will send
              # burns the whole timeout doing nothing.
              # Always offered; the site's own rules decide whether it is used.
              wait_url=verify_url(pk, company) if pk else ""),
        report_key="outcome",
        model=(getattr(get_settings(), "chrome_model", "") or ""),
        timeout_s=TIMEOUT_S, kind="apply",
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
