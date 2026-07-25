"""The pipeline must never be able to LOOK healthy while doing nothing.

Regression test for the two-day outage: `scripts/restart-server.sh` started
`python -m server` (uvicorn only) instead of `python -m daemon` (which spawns the
discovery/evaluate/apply threads). The board rendered, /stats returned 200, the
buttons responded — and nothing was ever discovered, scored, tailored or applied.
The queue silently grew to 10 undrained jobs.

The fix is a worker heartbeat: the daemon reports which loops are actually alive,
so a workerless process is detectable instead of indistinguishable from a healthy
one.
"""

from __future__ import annotations

from pathlib import Path

import fakeredis
import pytest
from core import flags


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    """Point the flag store at an in-memory Redis (flags are best-effort and
    swallow errors, so a real connection failure would silently pass tests)."""
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(flags, "_redis", lambda: client)
    return client


def test_no_heartbeat_means_the_daemon_is_down():
    """A bare `python -m server` never beats — that must read as DOWN, not healthy."""
    assert flags.workers_down() == ["daemon"]


def test_live_workers_are_not_reported_down():
    flags.beat_workers(["discovery", "evaluate", "apply"])
    assert flags.workers_down() == []


def test_missing_evaluate_worker_is_reported():
    """Loops that die individually must surface too — not just a dead process."""
    flags.beat_workers(["discovery", "apply"])
    assert flags.workers_down() == ["evaluate"]


def test_discovery_off_is_not_an_error():
    """Discovery is opt-out (APPLIEDIN_DISCOVERY=off); evaluate+apply are not."""
    flags.beat_workers(["evaluate", "apply"])
    assert flags.workers_down() == []


def test_stale_heartbeat_is_down():
    """A wedged/killed daemon stops beating; an old beat must not read as alive."""
    flags.beat_workers(["discovery", "evaluate", "apply"], now=1000.0)
    assert flags.workers_down(now=1000.0 + 30) == []
    assert flags.workers_down(now=1000.0 + 600) == ["daemon"]


def test_restart_script_starts_the_daemon_not_the_bare_server():
    """The actual root cause: the restart script launched the wrong module."""
    script = (Path(__file__).parents[2] / "scripts" / "restart-server.sh").read_text()
    assert "-m daemon" in script, "restart script must launch the daemon (workers + web)"
    assert "nohup .venv/bin/python -m server" not in script, (
        "restart script must NOT launch the bare server — it has no workers")


def test_orphan_recovery_frees_the_job_claim():
    """A run killed mid-flight never releases its claim.

    Recovery resets the job's status, and must clear the claim in the same
    breath. Otherwise the job looks runnable but every attempt is refused as
    "already being processed" until the TTL expires — silent, and it reads as a
    hang rather than a lock.
    """
    from agent.run import _claim, release_claim

    class _Tracking:
        def __init__(self, client):
            self.r = client

    client = fakeredis.FakeRedis(decode_responses=True)
    stores = type("S", (), {"tracking": _Tracking(client)})()

    assert _claim("acme#1", stores) is True          # a run starts
    assert _claim("acme#1", stores) is False         # ...and is killed; claim persists
    release_claim("acme#1", stores)                  # recovery frees it
    assert _claim("acme#1", stores) is True          # the retry can now run
