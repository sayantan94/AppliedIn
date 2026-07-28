"""Discovery yields to an application.

Two sessions reading pages coexist, which is why there is no blanket lock. A
crawl is not a reader though: it opens tabs, types into a search box and
navigates, in the same Chrome where a form is being filled. It takes the active
tab and the apply is left acting on the wrong page.

The two are not equal. Delaying a crawl costs minutes. Disturbing an application
can leave a form part submitted under a real name.
"""

import asyncio

import pytest

import tools.claude_chrome as cc


@pytest.fixture(autouse=True)
def _clean():
    cc._LIVE.clear()
    yield
    cc._LIVE.clear()


def _run(coro):
    return asyncio.run(coro)


def test_a_crawl_waits_while_an_application_is_being_filled(monkeypatch):
    monkeypatch.setattr(cc, "_YIELD_POLL_S", 0.01)
    started = []

    async def fake(*_a, **kw):
        started.append(kw.get("kind"))
        return {}, ""

    monkeypatch.setattr(cc, "_run_task_impl", fake)
    monkeypatch.setattr(cc, "available", lambda: (True, ""))

    cc._LIVE[999001] = "apply"          # an application is mid form

    async def scenario():
        crawl = asyncio.create_task(cc.run_task("t", report_key="jobs", kind="crawl"))
        await asyncio.sleep(0.05)
        assert not started, "the crawl started while an application was running"
        cc._LIVE.pop(999001)             # the application finishes
        await crawl

    _run(scenario())
    assert started == ["crawl"], "the crawl should run once the application is done"


def test_a_posting_read_does_not_wait(monkeypatch):
    """A jd read belongs to an apply's OWN flow. Making it wait for other
    applications would serialise applies and undo the concurrency we want."""
    monkeypatch.setattr(cc, "_YIELD_POLL_S", 0.01)
    started = []

    async def fake(*_a, **kw):
        started.append(kw.get("kind"))
        return {}, ""

    monkeypatch.setattr(cc, "_run_task_impl", fake)
    monkeypatch.setattr(cc, "available", lambda: (True, ""))
    cc._LIVE[999001] = "apply"

    _run(cc.run_task("t", report_key="description", kind="jd"))
    assert started == ["jd"], "a posting read must not be blocked by an application"


def test_an_application_never_waits_for_anything(monkeypatch):
    """Applications are the priority, so nothing defers them."""
    monkeypatch.setattr(cc, "_YIELD_POLL_S", 0.01)
    started = []

    async def fake(*_a, **kw):
        started.append(kw.get("kind"))
        return {}, ""

    monkeypatch.setattr(cc, "_run_task_impl", fake)
    monkeypatch.setattr(cc, "available", lambda: (True, ""))
    cc._LIVE[999002] = "crawl"          # a scan is in progress

    _run(cc.run_task("t", report_key="outcome", kind="apply"))
    assert started == ["apply"]


def test_a_crawl_gives_up_waiting_rather_than_never_running(monkeypatch):
    """Half an hour of applications means something is wedged. A scan that
    silently never happens is worse than one that risks a collision."""
    monkeypatch.setattr(cc, "_YIELD_POLL_S", 0.01)
    monkeypatch.setattr(cc, "_YIELD_MAX_S", 0.03)
    started = []

    async def fake(*_a, **kw):
        started.append(kw.get("kind"))
        return {}, ""

    monkeypatch.setattr(cc, "_run_task_impl", fake)
    monkeypatch.setattr(cc, "available", lambda: (True, ""))
    cc._LIVE[999001] = "apply"          # never finishes

    _run(cc.run_task("t", report_key="jobs", kind="crawl"))
    assert started == ["crawl"], "it must eventually run rather than be dropped"


def test_applies_running_counts_only_applications():
    cc._LIVE.update({1: "apply", 2: "crawl", 3: "jd", 4: "apply"})
    assert cc.applies_running() == 2
