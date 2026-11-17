"""Offline regressions for submitted today/total statistics (synthetic only)."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import server


_BEFORE_MIDNIGHT = "2030-01-01T20:59:00+00:00"
_AFTER_MIDNIGHT = "2030-01-01T21:01:00+00:00"
_BATCH = server._SUBMITTED_BATCH_SIZE


class _Pipeline:
    def __init__(self, redis):
        self.redis = redis
        self.commands = []

    def smembers(self, key):
        self.commands.append(("smembers", key))
        return self

    def get(self, key):
        self.commands.append(("get", key))
        return self

    def execute(self):
        self.redis.round_trips.append(tuple(self.commands))
        answer = []
        for command, key in self.commands:
            if command == "smembers":
                answer.append(set(self.redis.sets.get(key, ())))
            else:
                answer.append(self.redis.rows.get(key))
        return answer


class _Redis:
    """A pipeline-only Redis fake: forbidden full scans and direct GETs fail."""

    def __init__(self):
        self.rows = {}
        self.sets = {}
        self.round_trips = []

    def pipeline(self):
        return _Pipeline(self)

    def get(self, key):
        raise AssertionError("Sequential Redis GET must not be used")

    def smembers(self, key):
        raise AssertionError("Unbatched status membership reads must not be used")

    def scan_iter(self, *args, **kwargs):
        raise AssertionError("Full-store scan must not be used")


class _Tracking:
    def __init__(self):
        self.r = _Redis()
        self.status_counts_calls = 0

    def add(self, pk, status, applied_at=None, *, include_timestamp=True, bytes_pk=False):
        row = {"pk": pk, "company": "example-co", "name": "Test User",
               "status": status}
        if include_timestamp:
            row["applied_at"] = applied_at
        self.r.rows[f"app:{pk}"] = json.dumps(row).encode("utf-8")
        member = pk.encode("utf-8") if bytes_pk else pk
        self.r.sets.setdefault(f"status:{status}", set()).add(member)

    def status_counts(self):
        self.status_counts_calls += 1
        return {key.removeprefix("status:"): len(members)
                for key, members in self.r.sets.items()}

    def all(self):
        raise AssertionError("Full tracking enumeration is forbidden")

    def query_status(self, status):
        raise AssertionError("Per-member storage GETs are forbidden")

    def get(self, pk):
        raise AssertionError("Sequential tracking GETs are forbidden")


@pytest.fixture
def fixed_local_time(monkeypatch):
    """Set and always restore both TZ and the process-local timezone."""
    if not hasattr(time, "tzset"):
        pytest.skip("POSIX time.tzset is required for the fixed-offset boundary case")
    absent = object()
    prior = os.environ.get("TZ", absent)
    os.environ["TZ"] = "TST-03"  # POSIX sign: UTC+03, with no city zone.
    time.tzset()
    frozen = datetime(2030, 1, 1, 21, 15, tzinfo=timezone.utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen.astimezone().replace(tzinfo=None)
            return frozen.astimezone(tz)

    monkeypatch.setattr(server, "datetime", FrozenDatetime)
    try:
        yield frozen
    finally:
        if prior is absent:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prior
        time.tzset()


@pytest.fixture
def stats_client(monkeypatch):
    tracking = _Tracking()
    monkeypatch.setattr(server, "make_stores",
                        lambda settings: SimpleNamespace(tracking=tracking))
    from tools import submit_review
    monkeypatch.setattr(submit_review, "recover_orphans", lambda: None)
    monkeypatch.setattr(server, "_discovery_running", lambda: False)
    monkeypatch.setattr(server, "_applying_now", lambda: 0)
    monkeypatch.setattr(server, "_throttle_state", lambda: {})
    from core import flags, preparation
    from discovery import handler
    from tools import browser_runtime
    import core.apply_queue as apply_queue

    monkeypatch.setattr(preparation, "snapshot", lambda: [])
    monkeypatch.setattr(handler, "scanning", lambda: [])
    monkeypatch.setattr(handler, "active", lambda: False)
    monkeypatch.setattr(browser_runtime, "browser_status", lambda settings: {})
    for name, value in {
        "llm_error": None, "paused": False, "apply_mode": "manual",
        "submit_review_enabled": False, "browser_headless": False,
        "workers_down": [],
    }.items():
        monkeypatch.setattr(flags, name, lambda value=value: value)

    class _Queue:
        def __init__(self, redis):
            assert redis is tracking.r

        def pending(self):
            return []

    monkeypatch.setattr(apply_queue, "ApplyQueue", _Queue)
    with TestClient(server.create_app()) as client:
        yield client, tracking


def test_all_time_counts_both_statuses_regardless_of_date(
        stats_client, fixed_local_time):
    client, tracking = stats_client
    tracking.add("example-co#today", "applied", _AFTER_MIDNIGHT)
    tracking.add("example-co#older", "applied_manual", _BEFORE_MIDNIGHT)
    tracking.add("example-co#legacy", "applied", include_timestamp=False)
    tracking.add("example-co#waiting", "found", _AFTER_MIDNIGHT)
    payload = client.get("/stats")
    assert payload.status_code == 200
    stats = payload.json()
    assert stats["total_submitted"] == 3
    assert stats["today_submitted"] == 1
    assert stats["counts_by_status"]["found"] == 1
    assert tracking.status_counts_calls == 1


def test_fixed_offset_local_midnight_selects_only_new_day(
        stats_client, fixed_local_time):
    # At 21:15 UTC the daemon-local day is already January 2.
    assert fixed_local_time.astimezone().date().isoformat() == "2030-01-02"
    earlier = datetime.fromisoformat(_BEFORE_MIDNIGHT).astimezone()
    later = datetime.fromisoformat(_AFTER_MIDNIGHT).astimezone()
    assert earlier.date().isoformat() == "2030-01-01"
    assert later.date().isoformat() == "2030-01-02"
    client, tracking = stats_client
    tracking.add("example-co#before", "applied", _BEFORE_MIDNIGHT)
    tracking.add("example-co#after", "applied_manual", _AFTER_MIDNIGHT)
    assert client.get("/stats").json()["today_submitted"] == 1


@pytest.mark.parametrize("stamp", [None, "", "not-a-timestamp",
                                    "2030-01-02T00:02:00"])
def test_missing_empty_invalid_or_naive_timestamp_is_not_today(
        stats_client, fixed_local_time, stamp):
    client, tracking = stats_client
    tracking.add("example-co#invalid", "applied_manual", stamp,
                 include_timestamp=stamp is not None)
    payload = client.get("/stats").json()
    assert payload["total_submitted"] == 1
    assert payload["today_submitted"] == 0


def test_high_count_reads_only_submitted_sets_in_bounded_pipelines(
        stats_client, fixed_local_time):
    client, tracking = stats_client
    submitted = _BATCH * 2 + 129  # Synthetic count, deliberately > one batch.
    expected_today = 0
    for i in range(submitted):
        status = "applied" if i % 2 == 0 else "applied_manual"
        stamp = _BEFORE_MIDNIGHT if i % 3 == 0 else _AFTER_MIDNIGHT
        expected_today += stamp == _AFTER_MIDNIGHT
        tracking.add(f"example-co#synthetic-{i}", status, stamp,
                     bytes_pk=i % 11 == 0)
    for i in range(15):
        tracking.add(f"example-co#waiting-{i}", "found", _AFTER_MIDNIGHT)

    result = client.get("/stats")
    assert result.status_code == 200
    assert result.json()["total_submitted"] == submitted
    assert result.json()["today_submitted"] == expected_today
    assert tracking.status_counts_calls == 1
    trips = tracking.r.round_trips
    assert trips[0] == (("smembers", "status:applied"),
                        ("smembers", "status:applied_manual"))
    row_batches = trips[1:]
    assert len(row_batches) == ceil(submitted / _BATCH)
    assert sum(len(batch) for batch in row_batches) == submitted
    assert all(0 < len(batch) <= _BATCH for batch in row_batches)
    assert all(op == "get" and key.startswith("app:example-co#synthetic-")
               for batch in row_batches for op, key in batch)
    assert len(trips) == 1 + ceil(submitted / _BATCH)


def test_dashboard_and_demo_have_independent_submitted_vitals():
    # Read-only UI contract check; the PR owner also runs the offline render smoke.
    root = Path(__file__).resolve().parents[1]
    html = (root / "web/dashboard.html").read_text()
    script = (root / "web/app.js").read_text()
    demo = (root / "web/demo-data.js").read_text()
    assert 'id="v-cap"' in html and 'id="v-total"' in html
    assert '<span class="vital-l">Submitted today</span>' in html
    assert '<span class="vital-l">Submitted total</span>' in html
    assert 'tick($("#v-cap"), s.today_submitted ?? 0);' in script
    assert 'tick($("#v-total"), s.total_submitted ??' in script
    assert 'today_submitted: 2,' in demo
    assert 'total_submitted: 3,' in demo
