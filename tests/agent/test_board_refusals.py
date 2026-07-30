"""A board saying no is not a pipeline failure.

These land as `failed` with red styling, so without a plain explanation a cap or a
duplicate reads as a bug and sends the owner hunting a fault that is not there.
Nothing was submitted in any of them.
"""

from agent.run import _fail_reason


def test_an_application_cap_is_explained_as_a_safe_stop():
    """It lands as `failed` with red styling, so without this it reads as a bug and
    sends the owner hunting a fault that is not there."""
    msg = _fail_reason({"status": "failed", "reason": "application_limit"})
    assert "Nothing was submitted" in msg
    assert "nothing is wrong here" in msg
    assert "not by retrying" in msg


def test_the_board_s_own_words_are_kept():
    """The owner should see what the site actually said, not only our summary."""
    msg = _fail_reason({"status": "failed", "reason": "application_limit",
                        "detail": "no more than 5 times in any 180 day span"})
    assert "180 day span" in msg


def test_a_duplicate_reads_as_the_guard_working():
    msg = _fail_reason({"status": "failed", "reason": "duplicate_application"})
    assert "duplicate guard working" in msg


def test_a_real_failure_is_not_dressed_up_as_safe():
    """Only the codes that mean 'the board said no' get this treatment."""
    msg = _fail_reason({"status": "unknown", "detail": "the form never loaded"})
    assert "safety stop" not in msg
    assert "the form never loaded" in msg


# --- a cap is one employer's, never the board's ----------------------------

def test_a_cap_is_not_described_as_a_board_wide_rule():
    """The message used to say "Ashby boards allow 5 per 180 days", which states a
    single employer's setting as a property of software hundreds of companies use.
    That is false, and it teaches the reader exactly the wrong generalisation: that
    hitting a cap at one company means anything about the next.
    """
    msg = _fail_reason({"status": "failed", "reason": "application_limit"})
    assert "EMPLOYER" in msg
    assert "Ashby" not in msg and "180 days" not in msg
    assert "any other employer" in msg


def test_the_applier_is_told_not_to_predict_a_refusal():
    """The failure this guards: a session refusing to submit because a DIFFERENT
    company on the same job board refused earlier. A refusal you predicted is not
    a refusal, it is an application the owner never got."""
    from tools.claude_chrome import _task

    task = _task("https://jobs.ashbyhq.com/ramp/x", "Ramp", {}, "", "")
    assert "NEVER DECIDE AN APPLICATION IS POINTLESS" in task
    assert "Caps are set" in task and "previous session" in task


def test_ashby_rules_say_a_cap_is_per_company():
    from tools.browser_apply import _site_rules

    rules = _site_rules("https://jobs.ashbyhq.com/ramp/x", "Ramp")
    assert "belongs to ONE employer" in rules
    assert "ALWAYS fill the form and submit" in rules
