"""One-time codes handed to a browser session that is still open.

Oracle emails a validation code on a first application and the form will not go
on without it. The session cannot read the email or any file — its tools are the
browser plus Write — so it reads a page instead, and waits there.
"""

import fakeredis
import pytest

from core import verification as vf
from core.verification import Verification


@pytest.fixture
def v():
    return Verification(fakeredis.FakeRedis(decode_responses=True))


def test_a_code_reaches_the_waiting_session(v):
    v.start_waiting("oracle#1", "Oracle")
    assert v.code_for("oracle#1") == "", "nothing until the owner types it"
    v.give("oracle#1", "483920")
    assert v.code_for("oracle#1") == "483920"


def test_an_expired_code_is_never_handed_over(monkeypatch, v):
    """Typing a stale code reads as the FORM failing rather than the code being
    old, which sends the owner debugging the wrong thing."""
    monkeypatch.setattr(vf, "CODE_TTL_S", -1)
    v.give("oracle#1", "483920")
    assert v.code_for("oracle#1") == ""


def test_the_wait_is_announced_by_opening_the_page(v):
    """Registering the wait on the page read, rather than as a separate report,
    means a run cannot sit blocked with nobody knowing."""
    assert v.pending() == []
    v.start_waiting("oracle#1", "Oracle")
    assert [p["pk"] for p in v.pending()] == ["oracle#1"]
    assert v.pending()[0]["company"] == "Oracle"


def test_a_stale_wait_stops_being_offered(monkeypatch, v):
    """The session behind it has given up; a box that reaches nothing is worse
    than no box."""
    v.start_waiting("oracle#1", "Oracle")
    monkeypatch.setattr(vf, "WAIT_TTL_S", -1)
    assert v.pending() == []


def test_clearing_stops_a_code_being_reused(v):
    """A code left behind would be typed into the NEXT application and rejected."""
    v.start_waiting("oracle#1"); v.give("oracle#1", "483920")
    v.clear("oracle#1")
    assert v.code_for("oracle#1") == ""
    assert v.pending() == []


def test_an_empty_code_is_not_accepted(v):
    assert v.give("oracle#1", "   ") is False


def test_the_session_waits_less_time_than_the_code_survives():
    """Waiting longer than the code can live only buys the chance to type
    something already rejected."""
    assert vf.WAIT_TTL_S < vf.CODE_TTL_S


def test_the_clock_starts_once_not_on_every_poll(v):
    """The session reloads every ~20s. Restamping on each read meant the wait
    never aged: the cap could not fire and the owner was told the same time
    remained while the code expired behind it."""
    import time as _t

    v.start_waiting("oracle#1", "Oracle")
    first = v.pending()[0]["waiting_s"]
    _t.sleep(1.1)
    v.start_waiting("oracle#1", "Oracle")      # the next poll
    assert v.pending()[0]["waiting_s"] > first, "the clock must keep running"


def test_the_company_survives_later_polls(v):
    v.start_waiting("oracle#1", "Oracle")
    v.start_waiting("oracle#1", "")            # a poll without the query param
    assert v.pending()[0]["company"] == "Oracle"


def test_every_read_records_that_the_session_looked(v):
    """Without this there is no way to tell a session still watching the page from
    one that wandered off, and those need opposite responses: keep waiting, or
    start the job again. The START time must not move, since the cap depends on it.
    """
    import time as _t

    v.start_waiting("oracle#1", "Oracle")
    first_start = v.pending()[0]["waiting_s"]
    assert v.polls("oracle#1") == 1

    _t.sleep(1.1)
    v.start_waiting("oracle#1", "Oracle")          # the session polls again
    row = v.pending()[0]
    assert v.polls("oracle#1") == 2, "reads are counted"
    assert row["waiting_s"] > first_start, "the clock still runs from the first read"
    assert row["unseen_s"] == 0, "and it was just seen"


def test_a_session_that_stopped_looking_is_visible(v, monkeypatch):
    """The failure this exists to surface: the code is ready, the page serves it,
    and nothing collects it because the session ended."""
    v.start_waiting("oracle#1", "Oracle")
    v.give("oracle#1", "731559")
    # pretend the last read was a while ago
    v.r.hset("verify:seen", "oracle#1", f"{v.last_seen('oracle#1') - 300}|4")
    assert v.pending()[0]["unseen_s"] >= 300


def test_clearing_forgets_the_read_record_too(v):
    v.start_waiting("oracle#1"); v.clear("oracle#1")
    assert v.polls("oracle#1") == 0
    assert v.last_seen("oracle#1") == 0.0
