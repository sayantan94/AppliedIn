"""Only consider roles the employer published recently.

Being early is the point: a role posted today has a small pile of applications, a
role posted five weeks ago has a large one. The date has to come from the
EMPLOYER, since `discovered_at` only says when we looked, which makes every job on
a first sweep look brand new and every job on the next sweep look stale.
"""

from datetime import datetime, timedelta, timezone

import pytest

from discovery.freshness import age_hours, describe, is_fresh

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


def iso(**delta) -> str:
    return (NOW - timedelta(**delta)).isoformat()


@pytest.mark.parametrize("posted,keep", [
    (iso(hours=2), True),
    (iso(hours=47), True),
    (iso(hours=49), False),
    (iso(days=30), False),
])
def test_a_48_hour_window_keeps_only_what_is_new(posted, keep):
    assert is_fresh(posted, 48, now=NOW) is keep


def test_an_undated_posting_is_kept():
    """Roughly a third of sources publish no date. Dropping those would turn
    "only fresh roles" into "only roles from boards that timestamp", which is a
    different and much worse filter."""
    assert is_fresh("", 48, now=NOW) is True
    assert is_fresh("not a date at all", 48, now=NOW) is True


def test_no_limit_keeps_everything():
    """The default. A company whose board publishes no dates must not silently
    return nothing."""
    for limit in (0, None, ""):
        assert is_fresh(iso(days=400), limit, now=NOW) is True


def test_a_future_date_counts_as_brand_new():
    """A scheduled publish or a board with a clock problem. Whatever it is, it is
    certainly not stale, so it must not be discarded as unparseable."""
    future = (NOW + timedelta(hours=6)).isoformat()
    assert age_hours(future, now=NOW) == 0.0
    assert is_fresh(future, 48, now=NOW) is True


def test_a_naive_timestamp_is_read_as_utc():
    """Some boards omit the offset. Guessing local time would shift the age by
    hours and flip decisions near the boundary."""
    naive = NOW.replace(tzinfo=None) - timedelta(hours=3)
    assert round(age_hours(naive.isoformat(), now=NOW)) == 3


@pytest.mark.parametrize("posted,phrase", [
    (iso(minutes=10), "just now"),
    (iso(hours=5), "5h ago"),
    (iso(days=1, hours=1), "yesterday"),
    (iso(days=4), "4d ago"),
    ("", ""),
])
def test_the_phrase_shown_on_the_board(posted, phrase):
    assert describe(posted, now=NOW) == phrase


def test_the_window_is_a_parameter_not_a_fixed_48():
    """The platform gains windows over time: last 24 hours, last week. A hardcoded
    48 would mean a code change for each."""
    day_old = iso(hours=30)
    assert is_fresh(day_old, 24, now=NOW) is False
    assert is_fresh(day_old, 48, now=NOW) is True
    assert is_fresh(day_old, 24 * 7, now=NOW) is True


# --- a run's window must not become a company's setting --------------------

def test_a_run_window_outranks_the_saved_preference_without_changing_it():
    """Asking for the last 24 hours at a few companies is a question about one
    scan. If it wrote itself into their preferences, every later scan would
    silently inherit a limit nobody chose, and a company whose board publishes no
    dates would quietly return nothing for good.
    """
    import json

    from core import flags
    from discovery.crawler import _age_limit
    from discovery.freshness import run_window, set_run_window

    before = json.dumps(flags.company_prefs(), sort_keys=True)
    # Whatever the standing limit is: a global default, a company override, or
    # none. The invariant is that a run returns things to it, not that it is zero.
    baseline = _age_limit("waymo")
    try:
        set_run_window(24)
        assert run_window() == 24.0
        assert _age_limit("waymo") == 24.0, "the run's window is what gets used"
        assert json.dumps(flags.company_prefs(), sort_keys=True) == before, \
            "and nothing was written while it was in force"
    finally:
        set_run_window(0)

    assert _age_limit("waymo") == baseline, "the run's window leaves with the run"
    assert json.dumps(flags.company_prefs(), sort_keys=True) == before


def test_no_window_means_no_limit_so_undated_boards_still_work():
    """The default. Most boards publish no date, so a limit that lingered would
    turn discovery into silence."""
    from discovery.freshness import run_window, set_run_window

    set_run_window(0)
    assert run_window() == 0.0
    assert is_fresh(iso(days=900), 0, now=NOW) is True


# --- a rejection has to say why --------------------------------------------

def test_every_age_rejection_names_the_job_and_its_date(caplog):
    """A role vanishing from a scan with no explanation is indistinguishable from
    a scan that failed to see it, and the owner cannot tell whether the window was
    too narrow or the board's date was wrong. Those need different actions, so the
    log names each skipped role, the date it was judged on, and the window.
    """
    import logging
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch

    from core.models import JobRecord
    from discovery.crawler import _enqueue
    from discovery.watchlist import CompanyConfig

    now = datetime.now(timezone.utc)

    def job(title, hours):
        return JobRecord(company="Scale AI", job_id=title[:8], title=title,
                         jd_url=f"https://x/{title[:8]}", jd_text="x",
                         posted_at=(now - timedelta(hours=hours)).isoformat())

    class Stores:
        class tracking:
            @staticmethod
            def put_new(j): return True
        class queue:
            @staticmethod
            def enqueue(*a, **k): pass
        tailor_queue = "q"

    co = CompanyConfig(name="Scale AI", careers_url="https://scale.com/careers")
    with caplog.at_level(logging.INFO), \
            patch("discovery.crawler._age_limit", return_value=24), \
            patch("tools.seen.load", return_value=set()), \
            patch("tools.seen.mark", lambda x: None):
        _enqueue(co, [job("Staff Software Engineer, Data Platform", 168),
                      job("Senior Engineer, Inference", 8)], Stores())

    text = caplog.text
    assert "SKIPPED Staff Software Engineer, Data Platform" in text
    assert "168h ago" in text, "the age it was judged on"
    assert "outside the 24h window" in text, "and the window it was judged against"
    assert "Senior Engineer, Inference" not in text.split("SKIPPED")[1], \
        "a role inside the window is not reported as skipped"
