"""Why a role the owner can see on a careers page is not on their board.

A job dropped during discovery never becomes a row, so the board had no way to
answer that. The three answers need three different actions, which is why the
reason is recorded rather than just the fact: too old means widen the window,
not relevant means change the preferences, and missing from this trail entirely
means the scan never read it.
"""

import fakeredis
import pytest

from discovery import passed_over


class Job:
    def __init__(self, title, url, posted_at=""):
        self.title, self.jd_url, self.posted_at = title, url, posted_at
        self.location = "Remote"


@pytest.fixture
def r():
    return fakeredis.FakeRedis(decode_responses=True)


def test_a_rejection_keeps_the_fact_it_was_judged_on(r):
    passed_over.record(r, "Scale AI", [Job("Staff SWE, Data Platform", "u1", "2026-07-23")],
                       "too_old", "published outside the 24 hour window")
    row = passed_over.for_company(r, "Scale AI")[0]
    assert row["title"] == "Staff SWE, Data Platform"
    assert row["posted_at"] == "2026-07-23", "the date makes the decision checkable"
    assert row["reason"] == "too_old"
    assert "24 hour window" in row["detail"]


def test_the_two_reasons_stay_apart(r):
    """Widening a window and loosening preferences are different fixes."""
    passed_over.record(r, "Scale AI", [Job("A", "u1")], "too_old", "")
    passed_over.record(r, "Scale AI", [Job("B", "u2")], "not_relevant", "")
    reasons = {row["reason"] for row in passed_over.for_company(r, "Scale AI")}
    assert reasons == {"too_old", "not_relevant"}


def test_newest_first_so_a_long_tail_never_hides_today(r):
    for i in range(5):
        passed_over.record(r, "Scale AI", [Job(f"job {i}", f"u{i}")], "too_old", "")
    assert [row["title"] for row in passed_over.for_company(r, "Scale AI")][0] == "job 4"


def test_it_is_capped_so_one_board_cannot_swamp_the_trail(r):
    passed_over.record(r, "Scale AI", [Job(f"j{i}", f"u{i}") for i in range(200)],
                       "too_old", "")
    assert len(passed_over.for_company(r, "Scale AI", limit=500)) <= 60


def test_recording_never_breaks_a_scan():
    """An explanation is worth less than the scan it explains."""
    class Broken:
        def pipeline(self): raise RuntimeError("redis is down")
    assert passed_over.record(Broken(), "Scale AI", [Job("A", "u1")], "too_old", "") == 0


def test_every_company_appears_however_many_there_are(r):
    """The failure this replaced: a flat list sorted by time and truncated at 200.
    With 29 companies and 1665 records the budget was spent by the four most
    recent scans, and the other 25 companies did not appear at all — a company
    scanned an hour earlier looked identical to one never scanned.

    So the summary is complete and only the detail is sampled.
    """
    for i in range(12):
        passed_over.record(r, f"Company {i}", [Job(f"j{i}.{k}", f"u{i}.{k}")
                                               for k in range(40)], "too_old", "")

    groups = passed_over.by_company(r, sample=5)
    assert len(groups) == 12, "every company is listed, none truncated away"
    assert all(g["total"] == 40 for g in groups), "and its count is the true one"
    assert all(len(g["jobs"]) == 5 for g in groups), "while the rows are a sample"


def test_companies_are_ordered_by_how_much_they_rejected(r):
    """The noisy boards are the ones worth looking at."""
    passed_over.record(r, "Quiet", [Job("a", "u1")], "too_old", "")
    passed_over.record(r, "Noisy", [Job(f"b{i}", f"v{i}") for i in range(9)],
                       "too_old", "")
    assert [g["company"] for g in passed_over.by_company(r)] == ["noisy", "quiet"]


def test_the_reason_split_is_per_company(r):
    """Widening a window fixes one company; loosening preferences fixes another."""
    passed_over.record(r, "Waymo", [Job("a", "u1"), Job("b", "u2")], "too_old", "")
    passed_over.record(r, "Waymo", [Job("c", "u3")], "not_relevant", "")
    g = passed_over.by_company(r)[0]
    assert g["by_reason"] == {"too_old": 2, "not_relevant": 1}


def test_clearing_forgets_one_company_without_touching_the_rest(r):
    passed_over.record(r, "Scale AI", [Job("A", "u1")], "too_old", "")
    passed_over.record(r, "Waymo", [Job("B", "u2")], "too_old", "")
    passed_over.clear(r, "Scale AI")
    assert passed_over.for_company(r, "Scale AI") == []
    assert len(passed_over.for_company(r, "Waymo")) == 1


def test_a_store_with_no_client_keeps_no_trail_and_does_not_raise():
    """The cloud path has no Redis handle. Discovery must still run; it simply
    keeps no explanation."""
    assert passed_over.record(None, "Scale AI", [Job("A", "u1")], "too_old", "") == 0


def test_the_count_is_what_was_rejected_not_what_was_stored(r):
    """Only 60 rows are kept per company, so counting rows understates a noisy
    board: a company that rejected 200 roles read as exactly 60, which is the cap
    talking rather than the board. The header must be the true number."""
    passed_over.record(r, "Airbnb", [Job(f"j{i}", f"u{i}") for i in range(200)],
                       "too_old", "")
    g = passed_over.by_company(r)[0]
    assert g["total"] == 200, "the number actually rejected"
    assert g["kept"] == 60, "the number of rows available to open"
