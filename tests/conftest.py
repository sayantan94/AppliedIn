"""Repo-wide test isolation.

Tests exercise real pipeline functions, and those functions are wired to the
owner's live state: the activity feed, the seen-URL ledger, the memory diary.
Without this, running the suite writes fixture data into the running product —
"Engineer @ Acme" appeared 435 times in the live event feed, and an earlier
version recorded the fixtures' placeholder URL in the real seen ledger, which
then made two tests fail forever.

Nothing here is about making tests pass. It is about a test run never being
visible in, or destructive to, the thing the owner is using.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_owner_state(tmp_path, monkeypatch):
    # 1) Activity feed — emit() publishes to Redis and appends to the history the
    #    dashboard renders. Point it at an in-memory server instead.
    try:
        import fakeredis
        from core import events

        fake = fakeredis.FakeRedis(decode_responses=True)
        monkeypatch.setattr(events, "_redis", lambda: fake)
    except Exception:  # noqa: BLE001 — never block a run on the isolation itself
        pass

    # 2) Memory diary — a durable markdown log of real outcomes.
    try:
        from core import memory

        monkeypatch.setattr(memory, "_path", lambda: tmp_path / "memory.md")
    except Exception:  # noqa: BLE001
        pass

    # 3) Seen-URL ledger — discovery marks every enqueued URL here, and a fixture
    #    URL written into it silently filters real jobs on later runs.
    try:
        from tools import seen

        monkeypatch.setattr(seen, "_path", lambda: tmp_path / "seen.json")
    except Exception:  # noqa: BLE001
        pass

    yield
