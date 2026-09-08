"""A final selection must never authorize older queued work or lose decisions."""
from types import SimpleNamespace

import fakeredis
import pytest
from fastapi import BackgroundTasks

import server
from core.apply_queue import ApplyQueue
from core.models import Status
from core.storage.local import RedisTracking


@pytest.fixture
def review(monkeypatch):
    r = fakeredis.FakeRedis(decode_responses=True)
    tracking = RedisTracking(r)
    for pk, company, status in [
        ("acme#1", "Acme", Status.TAILORED),
        ("acme#2", "Acme", Status.TAILORED),
        ("acme#old", "Acme", Status.TAILORED),
        ("beta#1", "Beta", Status.TAILORED),
        ("acme#sent", "Acme", Status.APPLIED),
    ]:
        tracking.set_status(pk, status, company=company)
    monkeypatch.setattr(server, "make_stores", lambda *a: SimpleNamespace(tracking=tracking))
    endpoints = {route.path: route.endpoint for route in server.create_app().routes
                 if hasattr(route, "endpoint")}
    return tracking, ApplyQueue(r), endpoints


def test_apply_selected_does_not_drain_older_or_newly_arriving_jobs(review, monkeypatch):
    tracking, q, endpoints = review
    q.put("acme#old", "Acme")
    background = BackgroundTasks()
    applied = []

    def run(item, queue):
        applied.append(item["pk"])
        tracking.set_status(item["pk"], Status.APPLIED)
        queue.put("acme#new", "Acme")
        queue.done(item)

    monkeypatch.setattr("agent.run.run_queued", run)
    result = endpoints["/actions/apply-selection"](
        {"company": "Acme", "pks": ["acme#1", "acme#2"]}, background)
    assert result["ok"]
    task = background.tasks[0]
    task.func(*task.args, **task.kwargs)
    assert applied == ["acme#1", "acme#2"]
    assert {r["pk"] for r in q.pending()} == {"acme#old", "acme#new"}
    assert not q.flushing()


@pytest.mark.parametrize("body", [{}, {"company": "__all__", "pks": ["acme#1"]},
    {"company": "Acme", "pks": []}, {"company": "Acme", "pks": ["beta#1"]},
    {"company": "Acme", "pks": ["acme#1", "acme#sent"]}])
def test_invalid_or_changed_selection_never_partially_authorizes_jobs(review, body):
    _, q, endpoints = review
    background = BackgroundTasks()
    assert not endpoints["/actions/apply-selection"](body, background)["ok"]
    assert not background.tasks
    assert not q.pending()


def test_stop_before_background_dispatch_prevents_even_the_first_application(review, monkeypatch):
    _, q, endpoints = review
    monkeypatch.setattr("agent.run.run_queued",
                        lambda *_: pytest.fail("Stopped work must not start"))
    background = BackgroundTasks()
    assert endpoints["/actions/apply-selection"](
        {"company": "Acme", "pks": ["acme#1"]}, background)["ok"]
    q.stop_flush("Acme")
    task = background.tasks[0]
    assert task.func(*task.args, **task.kwargs) == 0
    assert [r["pk"] for r in q.pending()] == ["acme#1"]


def test_skip_survives_new_storage_instance_and_restore_preserves_resume_date(review):
    tracking, q, endpoints = review
    decide = endpoints["/actions/review-decision"]
    date = tracking.get("acme#1")["tailored_at"]
    assert decide({"company": "Acme", "pks": ["acme#1"], "action": "skip"})["ok"]
    saved = RedisTracking(tracking.r).get("acme#1")
    assert saved["review_skipped"] and saved["status"] == "tailored"
    assert decide({"company": "Acme", "pks": ["acme#1"], "action": "restore"})["ok"]
    assert not tracking.get("acme#1")["review_skipped"]
    assert tracking.get("acme#1")["tailored_at"] == date
    assert not q.pending()


def test_partial_rejections_can_be_undone_without_approving_or_erasing_submissions(review):
    tracking, q, endpoints = review
    decide = endpoints["/actions/review-decision"]
    result = decide({"company": "Acme", "pks": ["acme#1", "beta#1", "acme#sent"],
                     "action": "reject"})
    assert [r["ok"] for r in result["results"]] == [True, False, False]
    assert tracking.get("acme#sent")["status"] == "applied"
    assert tracking.get("beta#1")["status"] == "tailored"
    assert decide({"company": "Acme", "pks": ["acme#1"], "action": "undo"})["ok"]
    assert tracking.get("acme#1")["status"] == "tailored"
    assert not q.pending(), "Undo restores review, never application authorization"


@pytest.mark.parametrize("action", ["reject", "skip"])
def test_waiting_roles_can_be_removed_before_submission(review, action):
    tracking, q, endpoints = review
    q.put("acme#1", "Acme")
    result = endpoints["/actions/review-decision"](
        {"company": "Acme", "pks": ["acme#1"], "action": action})
    assert result["ok"]
    assert not q.pending()
    assert q.next() is None, "A skipped or rejected selection must not be dispatched"
    row = tracking.get("acme#1")
    assert row["status"] == ("skipped" if action == "reject" else "tailored")
    assert row["review_skipped"] == (action == "skip")


@pytest.mark.parametrize("action", ["reject", "skip", "restore"])
def test_running_roles_are_protected_even_before_tracking_catches_up(review, action):
    tracking, q, endpoints = review
    q.put("acme#1", "Acme")
    item = q.next()
    result = endpoints["/actions/review-decision"](
        {"company": "Acme", "pks": ["acme#1"], "action": action})
    assert not result["ok"]
    assert tracking.get("acme#1")["status"] == "tailored"
    assert item["pk"] in q.in_flight()


def test_reject_losing_the_dispatch_race_does_not_overwrite_the_application(review, monkeypatch):
    tracking, q, endpoints = review
    q.put("acme#1", "Acme")
    original = ApplyQueue.remove

    def dispatch_first(queue, pk):
        q.next()
        return original(queue, pk)

    monkeypatch.setattr(ApplyQueue, "remove", dispatch_first)
    result = endpoints["/actions/review-decision"](
        {"company": "Acme", "pks": ["acme#1"], "action": "reject"})
    assert not result["ok"]
    assert tracking.get("acme#1")["status"] == "tailored"
    assert q.in_flight() == {"acme#1"}


def test_remove_reports_failure_if_worker_removed_the_entry_since_it_was_read(review, monkeypatch):
    _, q, _ = review
    q.put("acme#1", "Acme")
    original = q.r.lrem

    def worker_wins(*args, **kwargs):
        original(*args, **kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(q.r, "lrem", worker_wins)
    assert not q.remove("acme#1")


def test_queue_selection_empty_means_none_and_lease_still_excludes_same_company(review):
    _, q, _ = review
    q.put("acme#1", "Acme")
    q.put("acme#2", "Acme")
    assert q.next(only="Acme", pks=set()) is None
    item = q.next(only="Acme", pks={"acme#2"})
    assert item["pk"] == "acme#2"
    assert q.next(only="Acme", pks={"acme#1"}) is None
    q.done(item)


def test_two_dispatchers_cannot_lease_the_same_company(review, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    _, q, _ = review
    q.put("acme#1", "Acme")
    q.put("acme#2", "Acme")
    barrier, original = Barrier(2), q.r.lrange

    def together(*args, **kwargs):
        rows = original(*args, **kwargs)
        barrier.wait(timeout=5)
        return rows

    monkeypatch.setattr(q.r, "lrange", together)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(q.next) for _ in range(2)]
        results = [f.result() for f in futures]
    assert sum(r is not None for r in results) == 1
