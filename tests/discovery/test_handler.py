"""Handler unit tests: watermark, backfill cap, and dedup-aware enqueue.

The adapter and Queue are faked so the test exercises discovery logic, not HTTP.
"""

from __future__ import annotations

import httpx
import pytest
from core.models import JobRecord
from core.storage.tracking import TrackingStore
from discovery import handler as h
from discovery.watchlist import CompanyConfig, Preferences
from tools import seen as _seen


@pytest.fixture(autouse=True)
def _isolated_seen(tmp_path, monkeypatch):
    """Keep the seen-URL ledger out of the developer's real .local/.

    These tests call discover_company, which marks every enqueued URL as seen.
    Pointed at the real file it wrote the fixtures' placeholder URL there
    permanently, after which every run filtered all its own jobs and the suite
    failed for good — while also mutating live pipeline state.
    """
    monkeypatch.setattr(_seen, "_path", lambda: tmp_path / "seen.json")


class FakeQueue:
    def __init__(self):
        self.messages = []

    def enqueue(self, url, body):
        self.messages.append((url, body))
        return "mid"


def _jobs(n, start=0):
    return [
        JobRecord(
            company="Acme", job_id=str(i), title="Engineer",
            jd_url=f"https://acme.example/jobs/{i}",
            jd_text="python", location="Remote", ats="greenhouse",
        )
        for i in range(start, start + n)
    ]


def _company():
    return CompanyConfig(name="Acme", ats="greenhouse", board="acme")


def test_first_run_applies_backfill_cap(applications_table, monkeypatch):
    monkeypatch.setattr(h, "ADAPTERS", {"greenhouse": type("A", (), {"fetch": lambda self, c, cl: _jobs(30)})()})
    tracking = TrackingStore(applications_table)
    queue = FakeQueue()
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))

    # max_new_per_run=0 disables the per-run top-N cap, so this exercises the
    # first-run BACKFILL cap on its own.
    prefs = Preferences(max_new_per_run=0)
    enqueued = h.discover_company(_company(), prefs, tracking, queue, client, "tailor-url")

    assert enqueued == h.BACKFILL_CAP == 25
    assert len(queue.messages) == 25


def test_top_n_cap_limits_what_one_run_tailors(applications_table, monkeypatch):
    """Tailoring is the expensive stage; a big board must not hand over dozens."""
    monkeypatch.setattr(h, "ADAPTERS", {"greenhouse": type("A", (), {"fetch": lambda self, c, cl: _jobs(30)})()})
    tracking = TrackingStore(applications_table)
    queue = FakeQueue()
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))

    enqueued = h.discover_company(_company(), Preferences(max_new_per_run=5),
                                  tracking, queue, client, "tailor-url")

    assert enqueued == 5
    assert len(queue.messages) == 5


def test_dedup_prevents_reenqueue_on_second_poll(applications_table, monkeypatch):
    adapter = type("A", (), {"fetch": lambda self, c, cl: _jobs(10)})()
    monkeypatch.setattr(h, "ADAPTERS", {"greenhouse": adapter})
    tracking = TrackingStore(applications_table)
    queue = FakeQueue()
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))

    # No top-N cap here — this test is about dedup across polls, not the cap.
    prefs = Preferences(max_new_per_run=0)
    first = h.discover_company(_company(), prefs, tracking, queue, client, "tailor-url")
    second = h.discover_company(_company(), prefs, tracking, queue, client, "tailor-url")

    assert first == 10
    assert second == 0  # all already in the table -> conditional writes block them
    assert len(queue.messages) == 10
