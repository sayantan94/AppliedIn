"""A board saying no is not a pipeline failure.

These land as `failed` with red styling, so without a plain explanation a cap or a
duplicate reads as a bug and sends the owner hunting a fault that is not there.
Nothing was submitted in any of them.
"""

from agent.run import _fail_reason


def test_an_application_cap_is_explained_as_a_safe_stop():
    msg = _fail_reason({"status": "failed", "reason": "application_limit"})
    assert "Nothing was submitted" in msg
    assert "safety stop" in msg


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
