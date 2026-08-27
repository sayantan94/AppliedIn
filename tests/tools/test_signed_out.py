"""A signed-out CLI must not look like a bad application.

This is here because it cost a real queue. The Claude CLI's OAuth session
expired, and its own result envelope said so — but the envelope carried
`subtype: "success"` alongside `is_error: true`, and the classifier only looked
at `subtype`, so it produced nothing and the failure was reported as "the Chrome
session ended without a structured result, check the browser". The owner was
sent to look at a browser that was never the problem.

The second cost is the one that matters. Nothing was submitted, so the job was
retryable, and at three attempts each a queue of two hundred jobs would have
dead-lettered itself against the same wall in about a minute.
"""

from __future__ import annotations

from tools.claude_chrome import _envelope_reason, is_infrastructure, is_signed_out

# The exact envelope from the incident, trimmed to the fields that decide it.
EXPIRED = (
    '{"is_error":true,"num_turns":1,"terminal_reason":"api_error",'
    '"api_error_status":null,"subtype":"success",'
    '"result":"Failed to authenticate: OAuth session expired and could not be refreshed"}'
)


def test_an_expired_login_is_recognised_from_its_own_envelope():
    """`subtype: success` means "the run completed", not "the work succeeded"."""
    why = _envelope_reason(EXPIRED, 1)
    assert why, "the envelope explained itself and the classifier returned nothing"
    assert "signed out" in why.lower()
    # It must say what to do. Every other fault here retries by itself; this one
    # waits for a human, so an answer that invites a retry is the wrong answer.
    assert "sign in" in why.lower()


def test_an_expired_login_is_infrastructure_not_a_verdict_on_the_job():
    why = _envelope_reason(EXPIRED, 1)
    assert is_infrastructure(why)
    assert is_signed_out(why)


def test_a_signed_out_session_is_told_apart_from_other_plumbing_faults():
    """Both are infrastructure; only one of them stops the queue."""
    rate_limited = _envelope_reason('{"subtype":"error","api_error_status":429}', 0)
    assert is_infrastructure(rate_limited)
    assert not is_signed_out(rate_limited), "a 429 clears on its own; a logout does not"


def test_a_clean_run_still_reports_nothing():
    """The classifier speaks only when something went wrong."""
    assert _envelope_reason(
        '{"subtype":"success","terminal_reason":"stop","is_error":false}', 0) == ""


def test_an_unexplained_error_still_says_what_it_can():
    """An envelope with is_error and no message must not fall back to silence."""
    why = _envelope_reason('{"is_error":true,"subtype":"success"}', 1)
    assert why and is_infrastructure(why)
