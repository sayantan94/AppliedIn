"""A submit that may have gone through is never retried. Never.

`core.apply_queue` promises it in its docstring: "an unconfirmed submit… goes to
the owner, never back into the queue," because a retry may send a second
application under a real name. `uncertain` is in TERMINAL for exactly that.

Two outcomes broke the promise by arriving labelled "unknown", which
`is_retryable` treats as an ordinary fault and tries again, up to three times:

  * the session reported APPLYING but captured no confirmation text — the agent
    says it clicked Submit; we simply could not read the page afterwards;
  * the session hit its time ceiling and was killed — at minute 44, possibly
    after the click. Its own message says "it may have submitted".

Both are `uncertain`. A session that never opened a form — the extension was
disconnected, the CLI was signed out — stays "unknown", because retrying that
cannot double-apply.
"""

from __future__ import annotations

from core.apply_queue import is_retryable
from tools.claude_chrome import TIMEOUT_OPENING, classify, problem_status


def test_applied_without_confirmation_is_uncertain_not_unknown():
    out = classify({"outcome": "applied", "confirmation": "", "filled": {"Email": "a@b"}})

    assert out["status"] == "uncertain"


def test_and_the_queue_will_not_retry_it():
    out = classify({"outcome": "applied", "confirmation": ""})

    again, why = is_retryable({"result": "failed", "reason": out["status"]})
    assert again is False
    assert why == "uncertain"


def test_a_killed_session_that_may_have_submitted_is_uncertain():
    problem = (f"{TIMEOUT_OPENING} 45 minutes and was stopped. Check the tab it "
               "left open — it may have submitted.")

    assert problem_status(problem) == "uncertain"
    assert is_retryable({"result": "failed", "reason": "uncertain"})[0] is False


def test_a_session_that_never_opened_a_form_stays_retryable():
    for problem in (
        "Browser extension is not connected. Please ensure the Claude browser extension is installed",
        "The Claude CLI could not run: exit 1",
        "claude --chrome failed (exit 2): it produced no output at all",
    ):
        assert problem_status(problem) == "unknown", problem


def test_applied_with_confirmation_is_still_applied():
    out = classify({"outcome": "applied", "confirmation": "Thanks, we got your application."})

    assert out["status"] == "applied"
