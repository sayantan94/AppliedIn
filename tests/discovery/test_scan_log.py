"""What each company's scan produced, as a sweep works through them.

A whole watchlist sweep is hours of sequential browser work. Without a per company
record the board can say which company is being read and nothing about the forty
that already finished, so a long run is a spinner rather than a list filling up.
"""

import fakeredis
import pytest

from discovery import scan_log


@pytest.fixture
def r():
    return fakeredis.FakeRedis(decode_responses=True)


def test_each_company_records_what_it_produced(r):
    scan_log.start_run(r, 3)
    scan_log.finished(r, "Adobe", found=4, relevant=4, enqueued=4, seconds=31.2)
    out = scan_log.results(r)
    assert out["run"]["total"] == 3
    row = out["companies"][0]
    assert row["company"] == "Adobe" and row["enqueued"] == 4
    assert row["seconds"] == 31.2, "how long it took is the point on a slow sweep"


def test_newest_first_so_the_list_reads_as_it_fills(r):
    scan_log.start_run(r, 2)
    scan_log.finished(r, "Adobe", found=1, relevant=1, enqueued=1, seconds=1)
    scan_log.finished(r, "Airbnb", found=2, relevant=2, enqueued=2, seconds=1)
    assert [c["company"] for c in scan_log.results(r)["companies"]] == ["Airbnb", "Adobe"]


def test_a_new_run_clears_the_previous_one(r):
    """A list mixing two runs cannot be read: "Adobe found nothing" means one
    thing this run and something else three runs ago."""
    scan_log.start_run(r, 1)
    scan_log.finished(r, "Adobe", found=9, relevant=9, enqueued=9, seconds=1)
    scan_log.start_run(r, 1)
    assert scan_log.results(r)["companies"] == []


def test_a_company_that_found_nothing_still_appears(r):
    """Absence must be recorded, not inferred. A company that found nothing is a
    different fact from one the sweep never reached."""
    scan_log.start_run(r, 1)
    scan_log.finished(r, "Adobe", found=0, relevant=0, enqueued=0, seconds=12)
    assert scan_log.results(r)["companies"][0]["enqueued"] == 0


def test_logging_never_breaks_a_scan():
    class Broken:
        def pipeline(self): raise RuntimeError("redis down")
    scan_log.start_run(Broken(), 1)
    scan_log.finished(Broken(), "Adobe", found=1, relevant=1, enqueued=1, seconds=1)
    assert scan_log.results(None) == {"run": None, "companies": []}
