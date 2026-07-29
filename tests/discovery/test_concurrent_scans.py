"""Scanning one company must not block scanning another.

Two crawls of different employers share nothing: different sites, different
feeds, different browser sessions, and findings under different keys. Only two
scans of the SAME company collide, duplicating work and racing each other's
writes.
"""

import threading

import pytest

from discovery import handler


@pytest.fixture(autouse=True)
def _clean():
    handler._release(handler.scanning())
    yield
    handler._release(handler.scanning())


def test_a_second_company_can_be_claimed_while_the_first_runs():
    assert handler._claim({"waymo"}) == {"waymo"}
    assert handler._claim({"netflix"}) == {"netflix"}, "a different company is free"
    assert handler.scanning() == {"waymo", "netflix"}


def test_the_same_company_cannot_be_claimed_twice():
    assert handler._claim({"waymo"}) == {"waymo"}
    assert handler._claim({"waymo"}) == set(), "already being scanned"


def test_a_partial_overlap_claims_only_what_is_free():
    """A whole-watchlist run while one company is mid scan should get on with the
    rest instead of refusing everything."""
    handler._claim({"waymo"})
    assert handler._claim({"waymo", "netflix", "apple"}) == {"netflix", "apple"}


def test_releasing_frees_it_again():
    handler._claim({"waymo"})
    handler._release({"waymo"})
    assert handler._claim({"waymo"}) == {"waymo"}


def test_two_threads_cannot_both_claim_one_company():
    """The claim is atomic — otherwise two requests arriving together both start."""
    got, barrier = [], threading.Barrier(2)

    def go():
        barrier.wait()
        got.append(handler._claim({"waymo"}))

    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert sorted(len(g) for g in got) == [0, 1], "exactly one claim wins"
