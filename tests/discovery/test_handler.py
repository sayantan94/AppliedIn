"""Handler unit tests: watermark, backfill cap, and dedup-aware enqueue.

The adapter and Queue are faked so the test exercises discovery logic, not HTTP.
"""

from __future__ import annotations

import httpx
from core.models import JobRecord
from core.storage.tracking import TrackingStore
from discovery import handler as h
from discovery.watchlist import CompanyConfig, Preferences


class FakeQueue:
    def __init__(self):
        self.messages = []

    def enqueue(self, url, body):
        self.messages.append((url, body))
        return "mid"


def _jobs(n, start=0):
    return [
        JobRecord(
            company="Acme", job_id=str(i), title="Engineer", jd_url="u",
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

    enqueued = h.discover_company(_company(), Preferences(), tracking, queue, client, "tailor-url")

    assert enqueued == h.BACKFILL_CAP == 25
    assert len(queue.messages) == 25


def test_dedup_prevents_reenqueue_on_second_poll(applications_table, monkeypatch):
    adapter = type("A", (), {"fetch": lambda self, c, cl: _jobs(10)})()
    monkeypatch.setattr(h, "ADAPTERS", {"greenhouse": adapter})
    tracking = TrackingStore(applications_table)
    queue = FakeQueue()
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))

    first = h.discover_company(_company(), Preferences(), tracking, queue, client, "tailor-url")
    second = h.discover_company(_company(), Preferences(), tracking, queue, client, "tailor-url")

    assert first == 10
    assert second == 0  # all already in the table -> conditional writes block them
    assert len(queue.messages) == 10
