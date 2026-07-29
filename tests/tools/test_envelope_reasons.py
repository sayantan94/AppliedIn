"""Every reason the envelope can give must be recognised as infrastructure.

These messages describe the plumbing — a rate limit, a restart, a dropped stream —
not the application. `_fail_reason` introduces anything it does NOT recognise with
"The browser agent finished without confirming a submission", which contradicts
them: the agent did not finish, it was cut off.

The list and the checker are two places, and they drifted once already: the
restart message was added without its opening, so a killed session was reported as
the agent having finished. This pins them together.
"""

import pytest

from agent.run import _fail_reason
from tools.claude_chrome import _envelope_reason, is_infrastructure

ENVELOPES = [
    ('{"num_turns":75,"subtype":"error_during_execution",'
     '"terminal_reason":"aborted_streaming","api_error_status":429}', 1),
    ('{"num_turns":40,"terminal_reason":"aborted_streaming","api_error_status":500}', 1),
    ('{"num_turns":31,"subtype":"error_during_execution",'
     '"terminal_reason":"aborted_streaming"}', 143),
    ('{"num_turns":12,"terminal_reason":"aborted_streaming"}', None),
]


@pytest.mark.parametrize("raw,returncode", ENVELOPES)
def test_every_envelope_reason_is_recognised(raw, returncode):
    why = _envelope_reason(raw, returncode)
    assert why, "this envelope should produce a reason"
    assert is_infrastructure(why), f"unrecognised opening: {why[:60]!r}"


@pytest.mark.parametrize("raw,returncode", ENVELOPES)
def test_none_of_them_are_told_the_agent_finished(raw, returncode):
    msg = _fail_reason({"status": "unknown", "detail": _envelope_reason(raw, returncode)})
    assert "finished without confirming" not in msg


def test_a_restart_is_named_rather_than_called_a_connection_failure():
    """SIGTERM still writes aborted_streaming, so reading only the envelope blames
    the network for something we did."""
    why = _envelope_reason('{"num_turns":31,"terminal_reason":"aborted_streaming"}', 143)
    assert "daemon was restarted" in why
    assert "connection failure" not in why


def test_a_clean_run_produces_no_reason():
    assert _envelope_reason('{"subtype":"success","terminal_reason":"completed"}', 0) == ""


# --- the one-time code callback -------------------------------------------

def test_the_callback_is_offered_but_not_permitted_by_code():
    """Which portals email a code is a fact about the portal, so the permission
    lives in that site's quirk, not in a list here. The prompt therefore describes
    the callback and explicitly withholds permission to use it."""
    from tools.claude_chrome import _task

    task = _task("https://example.com/job", "Example", {}, "", "",
                 wait_url="http://127.0.0.1:8787/verify/x")
    assert "ONE-TIME CODE CALLBACK" in task
    assert "only when this site's rules say to use it" in task


def test_a_run_with_no_job_gets_no_callback():
    """Nothing to attach a code to, so the instruction would be unfollowable."""
    from tools.claude_chrome import _task

    assert "ONE-TIME CODE" not in _task("https://example.com/job", "Example", {}, "", "")


def test_oracle_is_the_site_that_grants_it():
    from tools.browser_apply import _site_rules

    rules = _site_rules("https://eeho.fa.us2.oraclecloud.com/hcmUI/x", "Oracle")
    assert "ONE-TIME CODE CALLBACK" in rules, "the quirk must grant it explicitly"

    other = _site_rules("https://boards.greenhouse.io/stripe/jobs/1", "Stripe")
    assert "ONE-TIME CODE CALLBACK" not in other


def test_the_wait_url_is_per_job():
    """Two Oracle applications must not read each other's code."""
    from tools.claude_chrome import verify_url

    a, b = verify_url("oracle#1", "Oracle"), verify_url("oracle#2", "Oracle")
    assert a != b
    assert "oracle%231" in a, "the pk is escaped into the path"
