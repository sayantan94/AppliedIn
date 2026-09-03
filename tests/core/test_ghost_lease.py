"""A company lease with no application behind it is released after a grace period.

An Apply press re-tailors a stale row before applying and holds the company's
lease meanwhile. A model call that never returns held Netflix's lease for three
and a half hours: the row read `tailoring`, `/apply-queue` said `running:
['netflix']`, and every other Netflix job waited on a ghost. Recovery only
looked the other way — a `submitting` row with no lease."""

from __future__ import annotations

import fakeredis
import pytest

import daemon
from core.apply_queue import ApplyQueue


class _Tracking:
    def __init__(self, rows):
        self.rows = {r["pk"]: dict(r) for r in rows}

    def all(self):
        return [dict(r) for r in self.rows.values()]

    def set_status(self, pk, status, **kw):
        self.rows[pk]["status"] = getattr(status, "value", status)
        self.rows[pk].update(kw)


class _Stores:
    def __init__(self, rows):
        self.tracking = _Tracking(rows)


@pytest.fixture
def world(monkeypatch):
    q = ApplyQueue(fakeredis.FakeRedis(decode_responses=True))
    stores = _Stores([{"pk": "netflix#1", "company": "Netflix", "status": "tailoring"},
                      {"pk": "stripe#1", "company": "Stripe", "status": "submitting"}])
    q.r.sadd("applyq:inflight", "netflix", "stripe")
    q.r.sadd("applyq:inflight:pks", "netflix#1", "stripe#1")
    clock = [1000.0]
    monkeypatch.setattr(daemon.time, "monotonic", lambda: clock[0])
    daemon._LAST_RECLAIM[0] = -1e9
    daemon._LEASE_SEEN.clear()
    return q, stores, clock


def test_a_hung_retailor_keeps_its_lease_inside_the_grace(world):
    q, stores, clock = world
    daemon._reclaim_orphans(stores, q)
    assert "netflix" in q.r.smembers("applyq:inflight")


def test_and_loses_it_after(world):
    q, stores, clock = world
    daemon._reclaim_orphans(stores, q)
    clock[0] += daemon.LEASE_GRACE_S + 1
    daemon._LAST_RECLAIM[0] = -1e9
    daemon._reclaim_orphans(stores, q)
    assert "netflix" not in q.r.smembers("applyq:inflight")
    assert stores.tracking.rows["netflix#1"]["status"] == "found"


def test_a_real_application_is_never_touched(world):
    q, stores, clock = world
    clock[0] += daemon.LEASE_GRACE_S * 3
    daemon._LAST_RECLAIM[0] = -1e9
    daemon._reclaim_orphans(stores, q)
    assert "stripe" in q.r.smembers("applyq:inflight")
    assert stores.tracking.rows["stripe#1"]["status"] == "submitting"
