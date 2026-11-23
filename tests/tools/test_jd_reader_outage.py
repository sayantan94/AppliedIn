"""A Claude quota outage must not burn an entire browser-only job backlog."""
import asyncio
import json
from types import SimpleNamespace

import fakeredis
import pytest

from agent import run
from core.models import Status
from core.storage.local import RedisTracking
from tools import claude_chrome, jd

LIMIT = 'The Claude browser reader has reached its usage limit: resets 10:10pm. Retry after the limit resets.'
GOOD = 'Responsibilities: build reliable distributed systems. ' * 15


@pytest.fixture(autouse=True)
def isolate_reader(monkeypatch):
    monkeypatch.setattr(jd, '_browser_retry', (0, ''))
    monkeypatch.setattr(jd, 'available', lambda: (True, ''))
    monkeypatch.setattr(claude_chrome, 'available', lambda: (True, ''))
    monkeypatch.setattr(jd, '_from_ats', lambda url: None)
    monkeypatch.setattr(jd, '_get', lambda *a: None)


def test_session_limit_preserves_the_cli_reset_time_and_is_retryable():
    raw = json.dumps({'is_error': True, 'api_error_status': 429, 'num_turns': 1,
                      'result': "You've hit your session limit · resets 10:10pm (America/Los_Angeles)"})
    detail = claude_chrome._envelope_reason(raw, 1)
    assert 'resets 10:10pm (America/Los_Angeles)' in detail
    assert 'partway through' not in detail
    assert 'lower how many' not in detail
    assert claude_chrome.is_infrastructure(detail)


def test_one_outage_does_not_launch_a_browser_for_every_job(monkeypatch):
    calls = []
    async def unavailable(*args, **kwargs):
        calls.append(args)
        return {}, LIMIT
    monkeypatch.setattr(claude_chrome, 'run_task', unavailable)
    for i in range(4):
        with pytest.raises(jd.PostingReadUnavailable, match='usage limit'):
            jd.fetch_jd(f'https://example.com/jobs/{i}')
    assert len(calls) == 1
    monkeypatch.setattr(jd.time, 'monotonic', lambda: jd._browser_retry[0] + 1)
    async def recovered(*args, **kwargs):
        return {'title': 'Engineer', 'description': GOOD}, ''
    monkeypatch.setattr(claude_chrome, 'run_task', recovered)
    assert jd.fetch_jd('https://example.com/jobs/1') == GOOD


def test_batch_retains_successes_and_stops_on_shared_outage(monkeypatch):
    calls = []
    async def read(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return {'postings': [{'url': 'https://x/1', 'description': GOOD}]}, ''
        return {}, LIMIT
    monkeypatch.setattr(jd, 'run_task', read)
    descriptions, gone = jd.read_postings(['https://x/1', 'https://x/2', 'https://x/3'], batch=1, with_gone=True)
    assert descriptions == {'https://x/1': GOOD.strip()}
    assert not gone
    assert len(calls) == 2
    with pytest.raises(jd.PostingReadUnavailable):
        jd.fetch_jd('https://x/2')


def test_unavailable_reader_keeps_job_found_with_real_reason_and_never_tailors(monkeypatch):
    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    pk = "example-co#job-1"
    from core.models import JobRecord
    assert tracking.put_new(JobRecord(company="example-co", job_id="job-1",
                                      title="Test role", jd_url="https://example.test/jobs/1",
                                      jd_text="Test role"))
    stores = SimpleNamespace(tracking=tracking)
    def unavailable(*args, **kwargs):
        raise jd.PostingReadUnavailable(LIMIT)
    monkeypatch.setattr(jd, 'fetch_jd_meta', unavailable)
    monkeypatch.setattr('core.events.emit', lambda *a, **kw: None)
    monkeypatch.setattr(run, '_session_service', lambda: pytest.fail('No tailoring without a posting'))
    result = run.run_job(pk, stores, prepare_only=True)
    row = tracking.get(pk)
    assert result['result'] == 'deferred'
    assert row['status'] == 'found'
    assert LIMIT in row['jd_read_error']
    assert 'Attempt 1 of 3' in row['jd_read_error']
    assert 'UTC' in row['jd_read_error']
    assert not row.get('fail_kind')
    assert not tracking.r.exists(f"lock:job:{pk}")


def test_existing_usable_description_survives_reader_outage(monkeypatch):
    def unavailable(*args, **kwargs):
        raise jd.PostingReadUnavailable(LIMIT)
    monkeypatch.setattr(jd, 'fetch_jd_meta', unavailable)
    text = 'Build infrastructure. ' * 10
    assert asyncio.run(run._jd_text({'jd_url':'https://x/1', 'jd_text':text})).strip() == text.strip()


def test_missing_cli_and_empty_batch_do_not_break_with_gone_contract(monkeypatch):
    assert jd.read_postings([], with_gone=True) == ({}, set())
    monkeypatch.setattr(jd, 'available', lambda: (False, 'Claude Code is not installed'))
    with pytest.raises(jd.PostingReadUnavailable, match='not installed'):
        jd.read_postings(['https://x/1'], with_gone=True)


def _synthetic_tracking():
    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    pk = "example-co#job-1"
    from core.models import JobRecord
    assert tracking.put_new(JobRecord(company="example-co", job_id="job-1",
                                      title="Test role", jd_url="https://example.test/jobs/1",
                                      jd_text="Test role"))
    tracking.set_status(pk, Status.FOUND, attempts=7)
    return tracking, pk


def _guarded_meta(monkeypatch, reader):
    """Adapt an offline reader double to the current {text, guard} contract."""
    from inspect import isawaitable
    from tools import posting_guard as pg

    async def wrapped(row, **kw):
        value = reader(row, **kw)
        if isawaitable(value):
            value = await value
        return pg.analyze(value)

    monkeypatch.setattr(run, "_jd_meta", wrapped)


def _quiet(monkeypatch):
    monkeypatch.setattr(jd, "fetch_jd_meta",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            jd.PostingReadUnavailable("synthetic offline reader")))
    monkeypatch.setattr("core.events.emit", lambda *a, **kw: None)
    monkeypatch.setattr(run, "_session_service",
                        lambda: pytest.fail("No ADK session is permitted here"))


def test_persisted_budget_deadlines_and_real_ui_projection(monkeypatch):
    from datetime import UTC, datetime, timedelta
    from server import _to_ui

    _quiet(monkeypatch)
    clock = [datetime(2030, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr(run, "_utc_now", lambda: clock[0])
    calls = []

    async def unavailable(row, **kw):
        calls.append(row["pk"])
        raise jd.PostingReadUnavailable("synthetic reader outage")

    _guarded_meta(monkeypatch, unavailable)
    tracking, pk = _synthetic_tracking()
    other = "example-co#other"
    from core.models import JobRecord
    assert tracking.put_new(JobRecord(company="example-co", job_id="other",
                                      title="Test other", jd_url="", jd_text="other"))
    tracking.set_status(other, Status.FOUND, note="untouched",
                        jd_read_attempts=2, attempts=19)
    other_before = tracking.get(other)
    stores = SimpleNamespace(tracking=tracking)
    artifacts = SimpleNamespace(exists=lambda key: False, version=lambda key: "")

    first = run.run_job(pk, stores)
    row = tracking.get(pk)
    assert first["result"] == "deferred"
    assert row["status"] == "found"
    assert row["jd_read_attempts"] == 1
    assert row["jd_read_revision"] == 2
    assert row["jd_read_retry_at"] == (clock[0] + timedelta(seconds=60)).isoformat()
    assert "2030-01-01 00:01:00 UTC" in _to_ui(row, artifacts)["jd_read_error"]
    before_wait = dict(row)
    clock[0] += timedelta(seconds=59)
    assert run.run_job(pk, stores)["reason"] == "jd_read_not_eligible"
    assert tracking.get(pk) == before_wait
    assert len(calls) == 1

    clock[0] += timedelta(seconds=1)
    # Reconstruct the store instance over the persisted fake Redis backing.
    stores.tracking = tracking = RedisTracking(tracking.r)
    assert run.run_job(pk, stores)["result"] == "deferred"
    row = tracking.get(pk)
    assert row["jd_read_attempts"] == 2
    assert row["jd_read_revision"] == 4
    assert row["jd_read_retry_at"] == (clock[0] + timedelta(seconds=300)).isoformat()
    assert "2030-01-01 00:06:00 UTC" in _to_ui(row, artifacts)["jd_read_error"]
    clock[0] += timedelta(seconds=299)
    assert run.run_job(pk, stores)["reason"] == "jd_read_not_eligible"
    clock[0] += timedelta(seconds=1)
    assert run.run_job(pk, stores)["reason"] == "jd_reader_exhausted"
    row = tracking.get(pk)
    assert row["status"] == "error"
    assert row["jd_read_attempts"] == 3
    assert row["jd_read_revision"] == 6
    assert row["jd_read_retry_at"] is None
    assert row["jd_read_error"] == ""
    assert row["fail_kind"] == "jd_reader_exhausted"
    assert "Retry" in _to_ui(row, artifacts)["closed_reason"]
    assert "did not prepare or send" in row["error"]
    assert tracking.get(other) == other_before
    assert len(calls) == 3


@pytest.mark.parametrize("outcome", ["unavailable", "usable", "unreadable", "sponsorship"])
@pytest.mark.parametrize("terminal", [Status.APPLIED_MANUAL, Status.JOB_GONE])
def test_read_derived_terminal_results_never_overwrite_newer_outcome(
        monkeypatch, outcome, terminal):
    from tools import submit_hold

    _quiet(monkeypatch)
    tracking, pk = _synthetic_tracking()
    stores = SimpleNamespace(tracking=tracking)
    events = []
    monkeypatch.setattr("core.events.emit", lambda *a, **kw: events.append((a, kw)))
    terminal_row = []

    async def delayed(row, **kw):
        tracking.set_status(pk, terminal, note="terminal decision",
                            confirmation_id="synthetic-confirmed")
        submit_hold.mark(pk, tracking=tracking)
        terminal_row.append(tracking.get(pk))
        if outcome == "unavailable":
            raise jd.PostingReadUnavailable("synthetic outage")
        if outcome == "unreadable":
            return "Error page"
        if outcome == "sponsorship":
            return "This employer does not sponsor visas. " + GOOD
        return GOOD

    _guarded_meta(monkeypatch, delayed)
    result = run.run_job(pk, stores, prepare_only=True)
    assert result["result"] == "conflict"
    assert tracking.get(pk) == terminal_row[0]
    assert submit_hold.is_held(pk, tracking=tracking)
    assert not tracking.r.exists(f"lock:job:{pk}")
    assert all(event[0][0] not in ("failed", "applied", "error") for event in events)


@pytest.mark.parametrize("reason", ["durable_hold", "row_hold", "confirmation"])
def test_same_status_fresh_protection_during_read_blocks_preparation(monkeypatch, reason):
    from tools import submit_hold

    _quiet(monkeypatch)
    tracking, pk = _synthetic_tracking()
    stores = SimpleNamespace(tracking=tracking)

    async def read(row, **kw):
        if reason == "durable_hold":
            submit_hold.mark(pk, tracking=tracking)
        elif reason == "row_hold":
            tracking.set_status(pk, Status.TAILORING, possible_submission=True)
        else:
            tracking.set_status(pk, Status.TAILORING, confirmation_id="synthetic")
        return GOOD

    _guarded_meta(monkeypatch, read)
    result = run.run_job(pk, stores, prepare_only=True)
    assert result["result"] == "refused"
    assert result["reason"] == "submission_protected"
    assert "jd_read_attempts" not in tracking.get(pk)
    assert not tracking.r.exists(f"lock:job:{pk}")


@pytest.mark.parametrize("kind", ["unreadable", "sponsorship"])
def test_second_guard_after_successful_metadata_patch(monkeypatch, kind):
    _quiet(monkeypatch)
    tracking, pk = _synthetic_tracking()
    stores = SimpleNamespace(tracking=tracking)
    text = "Error page" if kind == "unreadable" else "We do not sponsor visas. " + GOOD

    # AC1: the posting's refusal alone is no longer applicant-side evidence.
    from core import steering
    from discovery.watchlist import Preferences
    monkeypatch.setattr(steering, "read", lambda: {"content": "sponsorship: required"})
    monkeypatch.setattr(run, "_effective_prefs", lambda co: Preferences())
    async def read(row, **kw):
        return text

    _guarded_meta(monkeypatch, read)
    predicate = run._unreadable if kind == "unreadable" else run._no_sponsorship
    snapshot = []

    def competing(result):
        # This decision runs AFTER the successful JD metadata CAS.
        tracking.set_status(pk, Status.APPLIED_MANUAL,
                            confirmation_id="synthetic", note="terminal")
        snapshot.append(tracking.get(pk))
        return predicate(result)

    monkeypatch.setattr(run, "_unreadable" if kind == "unreadable" else "_no_sponsorship",
                        competing)
    result = run.run_job(pk, stores, prepare_only=True)
    assert result["result"] == "conflict"
    assert tracking.get(pk) == snapshot[0]
    assert not tracking.r.exists(f"lock:job:{pk}")


def test_idle_sweep_filters_before_batch_cap(monkeypatch):
    from datetime import UTC, datetime, timedelta
    import daemon

    due = {"pk": "example-co#due", "status": "found"}
    pending = [
        {"pk": f"example-co#pending{i}", "status": "found",
         "jd_read_attempts": 1,
         "jd_read_retry_at": (datetime.now(UTC) + timedelta(days=5)).isoformat()}
        for i in range(3)]
    enqueued = []
    tracking = SimpleNamespace(query_status=lambda status: pending + [due])
    q = SimpleNamespace(enqueue=lambda name, body: enqueued.append(body["pk"]))
    daemon._sweep_found(SimpleNamespace(tracking=tracking, queue=q, tailor_queue="synthetic"))
    assert enqueued == ["example-co#due"]


def test_retry_owns_session_reset_without_a_found_gap(monkeypatch):
    _quiet(monkeypatch)
    tracking, pk = _synthetic_tracking()
    tracking.set_status(pk, Status.ERROR, jd_read_attempts=3,
                        fail_kind="jd_reader_exhausted", jd_read_revision=8,
                        jd_read_retry_at=None, error="Reader exhausted; Retry")
    stores = SimpleNamespace(tracking=tracking)
    observed = tracking.get(pk)
    calls = []

    async def reset(session_pk):
        calls.append("reset")
        row = tracking.get(pk)
        assert row["status"] == "tailoring"
        assert row["jd_read_revision"] == 9
        assert row["jd_read_attempts"] == 0
        assert row["jd_read_prepare_only"] is True
        assert run.run_job(pk, stores)["result"] == "already_done"
        assert run.retry_job(pk, stores)["result"] == "already_running"

    async def private_run(session_pk, row, fake_stores, *, prepare_only=False):
        calls.append("private")
        assert prepare_only is True
        return {"result": "prepared", "pk": pk}

    monkeypatch.setattr(run, "_reset_session", reset)
    monkeypatch.setattr(run, "_run_job_async", private_run)
    result = run.retry_job(pk, stores)
    assert result["result"] == "prepared"
    assert calls == ["reset", "private"]
    assert tracking.get(pk)["status"] == "tailoring"
    assert tracking.get(pk)["attempts"] == observed["attempts"]
    assert not tracking.r.exists(f"lock:job:{pk}")


def test_retry_refuses_in_flight_and_unknown_lease(monkeypatch):
    from core.apply_queue import ApplyQueue

    tracking, pk = _synthetic_tracking()
    tracking.set_status(pk, Status.ERROR, jd_read_attempts=3,
                        fail_kind="jd_reader_exhausted")
    stores = SimpleNamespace(tracking=tracking)
    original = tracking.get(pk)
    monkeypatch.setattr(ApplyQueue, "in_flight", lambda q: {pk})
    assert run.retry_job(pk, stores)["reason"] == "apply_in_flight"
    assert tracking.get(pk) == original
    monkeypatch.setattr(ApplyQueue, "in_flight",
                        lambda q: (_ for _ in ()).throw(RuntimeError("synthetic lease outage")))
    assert run.retry_job(pk, stores)["reason"] == "apply_lease_unknown"
    assert tracking.get(pk) == original


def test_reopen_refuses_active_and_exhausted_without_events_or_tasks(monkeypatch):
    from fastapi import BackgroundTasks
    from core.apply_queue import ApplyQueue
    import server

    tracking, pk = _synthetic_tracking()
    stores = SimpleNamespace(tracking=tracking)
    monkeypatch.setattr(server, "make_stores", lambda *a, **kw: stores)
    monkeypatch.setattr(server, "get_settings", lambda: SimpleNamespace())
    events = []
    monkeypatch.setattr("core.events.emit", lambda *a, **kw: events.append(a))
    app = server.create_app()
    callback = next(r.endpoint for r in app.routes
                    if getattr(r, "path", "") == "/actions/reopen/{pk:path}")
    for status in (Status.FOUND, Status.TAILORING, Status.TAILORED,
                   Status.NEEDS_HUMAN, Status.SUBMITTING,
                   Status.APPLIED_MANUAL):
        tracking.set_status(pk, status)
        before = tracking.get(pk)
        tasks = BackgroundTasks()
        assert callback(pk, tasks)["ok"] is False
        assert tracking.get(pk) == before
        assert not tasks.tasks
    tracking.set_status(pk, Status.ERROR, jd_read_attempts=3,
                        fail_kind="jd_reader_exhausted")
    before = tracking.get(pk)
    tasks = BackgroundTasks()
    assert "Retry" in callback(pk, tasks)["note"]
    assert tracking.get(pk) == before
    assert not tasks.tasks
    tracking.set_status(pk, Status.SKIPPED, jd_read_attempts=0,
                        fail_kind="", confirmation_id="")
    before = tracking.get(pk)
    monkeypatch.setattr(ApplyQueue, "in_flight", lambda q: {pk})
    assert callback(pk, BackgroundTasks())["ok"] is False
    assert tracking.get(pk) == before
    assert not events


def test_storage_fault_after_manual_interleaving_stays_inside_daemon(monkeypatch):
    import daemon
    from tools import submit_hold

    _quiet(monkeypatch)
    tracking, pk = _synthetic_tracking()
    stores = SimpleNamespace(tracking=tracking)
    monkeypatch.setattr(daemon, "make_stores", lambda: stores)
    from core import flags
    monkeypatch.setattr(flags, "paused", lambda: False)
    _guarded_meta(monkeypatch, lambda *a, **kw: _synthetic_good())
    original = tracking.update_if_status
    status_errors = []
    before = []
    counter = [0]

    def faulty(pk_value, expected_status, updates, *, expected_reader):
        counter[0] += 1
        if counter[0] == 2:
            tracking.set_status(pk_value, Status.APPLIED_MANUAL,
                                confirmation_id="synthetic", note="manual terminal")
            submit_hold.mark(pk_value, tracking=tracking)
            before.append(tracking.get(pk_value))
            raise RuntimeError("synthetic storage I/O failure")
        return original(pk_value, expected_status, updates,
                        expected_reader=expected_reader)

    def tracking_set(pk_value, status, **kw):
        if status == Status.ERROR:
            status_errors.append(kw)
        return original_set(pk_value, status, **kw)

    original_set = tracking.set_status
    monkeypatch.setattr(tracking, "update_if_status", faulty)
    monkeypatch.setattr(tracking, "set_status", tracking_set)
    daemon._evaluate_one(pk)
    assert counter[0] >= 3  # admission, fault, bounded same-witness attention
    assert not status_errors  # daemon's generic catch never wrote ERROR
    assert tracking.get(pk) == before[0]
    assert submit_hold.is_held(pk, tracking=tracking)
    assert not tracking.r.exists(f"lock:job:{pk}")


async def _synthetic_good():
    return GOOD


@pytest.mark.parametrize("mode", ["auto", "gated"])
@pytest.mark.parametrize("requested", ["", "2030-01-01T00:00:00+00:00"])
def test_review_only_generic_ready_gate_never_creates_submission_authority(
        monkeypatch, mode, requested):
    from core import flags
    from core.apply_queue import ApplyQueue

    tracking, pk = _synthetic_tracking()
    tracking.set_status(pk, Status.TAILORING,
                        resume_s3_key="resumes/example-co-job-1.pdf",
                        apply_requested_at=requested, match_score=9,
                        jd_read_prepare_only=True)
    stores = SimpleNamespace(tracking=tracking)
    emits = []
    monkeypatch.setattr("core.events.emit", lambda *a, **kw: emits.append(a))
    monkeypatch.setattr(flags, "apply_mode", lambda: mode)
    monkeypatch.setattr(run, "_auto_decision",
                        lambda *a: pytest.fail("prepare-only cannot auto-approve"))

    class ReadyEvent:
        author = "review"
        content = None

        def get_function_calls(self):
            return [SimpleNamespace(name="ask_human", id="synthetic-call",
                                    args={"question": "Ready to apply?"})]

        def get_function_responses(self):
            return []

    class ReviewRunner:
        async def run_async(self, **kwargs):
            yield ReadyEvent()

    result = run._run(run._drive_async(
        ReviewRunner(), pk, SimpleNamespace(), stores, prepare_only=True))
    # AC5: a generic ready claim with only an old PDF cannot authorize a gate.
    assert result["result"] == "failed" and result["reason"] == "no_resume"
    assert tracking.get(pk)["status"] == "failed"
    assert not tracking.get(pk)["apply_requested_at"]
    queue = ApplyQueue(tracking.r)
    pending = [it for it in queue.pending() if it["pk"] == pk]
    assert pending == []
    assert all(ev[0] != "applied" for ev in emits)


def test_recovered_reader_uses_review_agent_for_ordinary_posting(monkeypatch):
    from agent import graph

    tracking, pk = _synthetic_tracking()
    tracking.set_status(pk, Status.ERROR, jd_read_attempts=3,
                        fail_kind="jd_reader_exhausted", jd_read_revision=8,
                        apply_requested_at="")
    stores = SimpleNamespace(tracking=tracking)
    agents = []
    events = []
    monkeypatch.setattr("core.events.emit", lambda *a, **kw: events.append(a))
    monkeypatch.setattr(run, "_session_state",
                        lambda row, text: {"base_latex": "Test User synthetic seed"})
    monkeypatch.setattr(run, "_save_output", lambda *a, **kw: None)

    class Sessions:
        async def get_session(self, **kwargs):
            return None

        async def create_session(self, **kwargs):
            return None

        async def delete_session(self, **kwargs):
            return None

    monkeypatch.setattr(run, "_session_service", lambda: Sessions())

    async def recovered(row, **kwargs):
        return GOOD

    _guarded_meta(monkeypatch, recovered)

    class FakeRunner:
        def __init__(self, agent, **kw):
            agents.append(agent)

    monkeypatch.setattr(run, "Runner", FakeRunner)

    async def prepared(runner, pk, message, stores, *, prepare_only, observed):
        assert prepare_only
        stores.tracking.set_status(pk, Status.TAILORED,
                                   resume_s3_key="resumes/example-co-job-1.pdf")
        return {"result": "prepared", "pk": pk}

    monkeypatch.setattr(run, "_drive_async", prepared)
    assert run.retry_job(pk, stores)["result"] == "prepared"
    assert agents == [graph.review_agent]
    assert tracking.get(pk)["jd_read_attempts"] == 0
    assert tracking.get(pk)["jd_read_prepare_only"] is True
    assert tracking.get(pk)["jd_read_error"] == ""
    assert tracking.get(pk)["status"] == "tailored"
    assert not tracking.r.exists(f"lock:job:{pk}")


def test_preparation_only_enqueue_rechecks_revoked_request(monkeypatch):
    from core.apply_queue import ApplyQueue

    tracking, pk = _synthetic_tracking()
    stores = SimpleNamespace(tracking=tracking)
    tracking.set_status(pk, Status.TAILORED,
                        resume_s3_key="resumes/example-co-job-1.pdf",
                        jd_read_prepare_only=True, apply_requested_at="")
    assert run._enqueue_apply(pk, stores, require_request=True)["result"] == "refused"
    assert not any(item["pk"] == pk for item in ApplyQueue(tracking.r).pending())
    tracking.set_status(pk, Status.TAILORED,
                        apply_requested_at="2030-01-01T00:00:00+00:00",
                        confirmation_id="synthetic-confirmed")
    assert run._enqueue_apply(pk, stores, require_request=True)["result"] == "refused"
    assert not ApplyQueue(tracking.r).pending()


def test_reopen_safe_closed_row_schedules_once_and_conflicts_stop(monkeypatch):
    from fastapi import BackgroundTasks
    import server

    tracking, pk = _synthetic_tracking()
    tracking.set_status(pk, Status.SKIPPED, skip_reason="synthetic")
    queued = []
    stores = SimpleNamespace(tracking=tracking, tailor_queue="tailor",
                             queue=SimpleNamespace(enqueue=lambda q, item: queued.append(item)))
    monkeypatch.setattr(server, "make_stores", lambda *a, **kw: stores)
    monkeypatch.setattr(server, "get_settings", lambda: SimpleNamespace())
    events = []
    monkeypatch.setattr("core.events.emit", lambda *a, **kw: events.append(a))
    app = server.create_app()
    callback = next(r.endpoint for r in app.routes
                    if getattr(r, "path", "") == "/actions/reopen/{pk:path}")
    tasks = BackgroundTasks()
    result = callback(pk, tasks)
    assert result["ok"] is True
    assert tracking.get(pk)["status"] == "found"
    assert tracking.get(pk)["jd_read_revision"] == 1
    assert not tasks.tasks
    assert queued == [{"pk": pk}]
    assert tracking.get(pk)["rescore_requested"] is True
    assert len(events) == 1

    tracking.set_status(pk, Status.FAILED, jd_read_attempts=0,
                        jd_read_retry_at=None)
    before = tracking.get(pk)
    orig = tracking.update_if_status

    def manual_wins(*a, **kw):
        tracking.set_status(pk, Status.APPLIED_MANUAL,
                            confirmation_id="synthetic-confirmed")
        return orig(*a, **kw)

    monkeypatch.setattr(tracking, "update_if_status", manual_wins)
    events.clear()
    tasks = BackgroundTasks()
    assert callback(pk, tasks)["error"] == "conflict"
    assert tracking.get(pk)["status"] == "applied_manual"
    assert tracking.get(pk)["jd_read_attempts"] == before["jd_read_attempts"]
    assert not events
    assert not tasks.tasks


@pytest.mark.parametrize("hanging", [False, True], ids=["raising", "hanging"])
def test_many_reopens_share_evaluate_lanes_and_release_failed_runs(monkeypatch, hanging):
    """Exercise the real queue drain/pool, admission, error exit and claim release.

    The fake provider honors LiteLLM's configured request timeout without ever
    making a request. Synchronizing lane entry proves actual overlap, not just
    that some sequential invocation happened to stay below the limit.
    """
    import threading
    import litellm
    import daemon
    import server
    from core import flags
    from fastapi import BackgroundTasks

    tracking, pk = _synthetic_tracking()
    pending = []
    stores = SimpleNamespace(
        tracking=tracking, tailor_queue="tailor",
        queue=SimpleNamespace(
            enqueue=lambda queue, item: pending.append(item),
            drain=lambda queue: list(pending)))
    monkeypatch.setattr(server, "make_stores", lambda *a, **kw: stores)
    monkeypatch.setattr(server, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(daemon, "make_stores", lambda: stores)
    monkeypatch.setattr(flags, "paused", lambda: False)
    monkeypatch.setattr("core.events.emit", lambda *a, **kw: None)
    monkeypatch.setattr(daemon, "_EVAL_LANES", 2)
    monkeypatch.setattr(litellm, "request_timeout", 0.01)
    endpoint = next(r.endpoint for r in server.create_app().routes
                    if getattr(r, "path", "") == "/actions/reopen/{pk:path}")
    pks = [f"example-co#synthetic-{n}" for n in range(8)]
    for target in pks:
        tracking.set_status(target, Status.SKIPPED, company="example-co",
                            title="Synthetic role", jd_url="https://example.com/test")
        tasks = BackgroundTasks()
        assert endpoint(target, tasks)["status"] == "queued"
        assert not tasks.tasks
    assert len(pending) == len(pks)
    active = maximum = 0
    lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=5)
    reset = []

    async def reset_session(target, *, strict):
        assert strict
        reset.append(target)

    async def provider(target, admitted, current_stores, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            barrier.wait()
            if hanging:
                await asyncio.wait_for(asyncio.Event().wait(), litellm.request_timeout)
            raise RuntimeError("synthetic provider failure")
        finally:
            with lock:
                active -= 1

    class BatchFinished(Exception):
        pass

    def finish_batch(seconds):
        raise BatchFinished

    monkeypatch.setattr(run, "_reset_session", reset_session)
    monkeypatch.setattr(run, "_run_job_async", provider)
    monkeypatch.setattr(daemon.time, "sleep", finish_batch)
    with pytest.raises(BatchFinished):
        daemon._worker_loop()
    assert maximum == 2 and active == 0
    assert sorted(reset) == sorted(pks)
    for target in pks:
        assert tracking.get(target)["status"] == "error"
        assert not run._claimed(target, stores)


def test_process_backlog_contains_reader_storage_fault_after_manual_decision(
        monkeypatch):
    from contextlib import nullcontext
    from tools import submit_hold
    import daemon
    from core import flags, preparation

    _quiet(monkeypatch)
    tracking, pk = _synthetic_tracking()
    stores = SimpleNamespace(tracking=tracking)
    monkeypatch.setattr(daemon, "make_stores", lambda: stores)
    monkeypatch.setattr(flags, "skipped_companies", lambda: set())
    monkeypatch.setattr(flags, "stop_epoch", lambda: 0)
    monkeypatch.setattr(daemon, "_pass_cancelled", lambda *a, **kw: False)
    monkeypatch.setattr(run, "_browser_companies", lambda: set())
    _guarded_meta(monkeypatch, lambda *a, **kw: _synthetic_good())
    from types import SimpleNamespace as SN

    class Progress:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def start(self, row):
            pass

        def complete(self, status):
            pass

    monkeypatch.setattr(preparation, "track", lambda selected: Progress())
    original = tracking.update_if_status
    original_set = tracking.set_status
    writes = []
    terminal = []
    counter = [0]

    def set_status(pk, status, **attrs):
        if status == Status.ERROR:
            writes.append(attrs)
        return original_set(pk, status, **attrs)

    def faulty(pk, expected_status, updates, *, expected_reader):
        counter[0] += 1
        if counter[0] == 2:
            original_set(pk, Status.APPLIED_MANUAL, confirmation_id="synthetic")
            submit_hold.mark(pk, tracking=tracking)
            terminal.append(tracking.get(pk))
            raise RuntimeError("synthetic storage error")
        return original(pk, expected_status, updates, expected_reader=expected_reader)

    monkeypatch.setattr(tracking, "set_status", set_status)
    monkeypatch.setattr(tracking, "update_if_status", faulty)
    result = daemon.process_backlog_once(prepare_only=True)
    assert result["evaluated"] == 1
    assert not writes
    assert tracking.get(pk) == terminal[0]
    assert submit_hold.is_held(pk, tracking=tracking)
    assert not tracking.r.exists(f"lock:job:{pk}")


@pytest.mark.parametrize("skip", [False, True])
def test_manual_backlog_late_scorer_error_respects_admitted_generation(monkeypatch, skip):
    import daemon
    from agent.graph import ScorerModelError
    from core import flags

    _quiet(monkeypatch)
    tracking, pk = _synthetic_tracking()
    stores = SimpleNamespace(tracking=tracking)
    monkeypatch.setattr(daemon, "make_stores", lambda: stores)
    monkeypatch.setattr(flags, "skipped_companies", lambda: set())
    monkeypatch.setattr(flags, "stop_epoch", lambda: 0)
    monkeypatch.setattr(daemon, "_pass_cancelled", lambda *args: False)
    monkeypatch.setattr(run, "prefetch_browser_jds", lambda *args: None)

    async def fail(target, admitted, current_stores, **kwargs):
        assert admitted["status"] == "tailoring"
        if skip:
            tracking.set_status(target, Status.SKIPPED, skip_reason="user_skipped")
        raise ScorerModelError("synthetic provider failure")

    monkeypatch.setattr(run, "_run_job_async", fail)
    assert daemon.process_backlog_once(prepare_only=True)["evaluated"] == 0
    row = tracking.get(pk)
    assert row["status"] == ("skipped" if skip else "error")
    if skip:
        assert row["skip_reason"] == "user_skipped"
    else:
        # The manual wrapper must not overwrite run_job's useful failure reason.
        assert "Scorer model/request failed" in row["error"]
    assert not run._claimed(pk, stores)


def test_evaluate_worker_survives_pre_run_tracking_read_failure(monkeypatch):
    """A failed snapshot must not escape pool.map and kill subsequent sweeps."""
    import daemon
    from core import flags

    batches = iter([[{"pk": "example-co#unreadable"}], [{"pk": "example-co#next"}]])
    reads, evaluated = [], []

    def get(pk):
        reads.append(pk)
        if pk.endswith("unreadable"):
            raise RuntimeError("synthetic storage fault")
        return {"pk": pk, "status": "found"}

    stores = SimpleNamespace(tracking=SimpleNamespace(get=get), tailor_queue="tailor",
                             queue=SimpleNamespace(drain=lambda queue: next(batches)))
    monkeypatch.setattr(daemon, "make_stores", lambda: stores)
    monkeypatch.setattr(flags, "paused", lambda: False)
    monkeypatch.setattr(run, "run_job", lambda pk, stores: evaluated.append(pk))
    sleeps = []

    class SweepsFinished(Exception):
        pass

    def next_sweep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise SweepsFinished

    monkeypatch.setattr(daemon.time, "sleep", next_sweep)
    with pytest.raises(SweepsFinished):
        daemon._worker_loop()
    assert reads == ["example-co#unreadable", "example-co#next"]
    assert evaluated == ["example-co#next"]
    assert len(sleeps) == 2


def test_manual_process_does_not_add_a_third_reopen_lane(monkeypatch):
    """Hold two real admitted workers while manual Process sees queued reopens."""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import daemon
    from core import flags

    _quiet(monkeypatch)
    tracking, _ = _synthetic_tracking()
    pks = [f"example-co#reopened-{n}" for n in range(4)]
    for target in pks:
        tracking.set_status(target, Status.FOUND, company="example-co", rescore_requested=True)
    # The unrelated synthetic fixture row must not become manual work here.
    tracking.set_status("example-co#job-1", Status.SKIPPED)
    stores = SimpleNamespace(tracking=tracking)
    monkeypatch.setattr(daemon, "make_stores", lambda: stores)
    monkeypatch.setattr(flags, "paused", lambda: False)
    monkeypatch.setattr(flags, "skipped_companies", lambda: set())
    monkeypatch.setattr(flags, "stop_epoch", lambda: 0)
    monkeypatch.setattr(daemon, "_pass_cancelled", lambda *args: False)
    monkeypatch.setattr(run, "prefetch_browser_jds", lambda *args: None)

    async def reset(target, *, strict):
        assert strict

    barrier, release = threading.Barrier(3, timeout=5), threading.Event()
    started = []

    async def provider(target, admitted, current_stores, **kwargs):
        started.append(target)
        if target in pks[:2]:
            barrier.wait()
            assert release.wait(5)
        raise RuntimeError("synthetic provider failure")

    monkeypatch.setattr(run, "_reset_session", reset)
    monkeypatch.setattr(run, "_run_job_async", provider)
    with ThreadPoolExecutor(max_workers=2) as pool:
        workers = [pool.submit(daemon._evaluate_one, target) for target in pks[:2]]
        try:
            barrier.wait()
            assert daemon.process_backlog_once(prepare_only=True)["evaluated"] == 0
            assert sorted(started) == sorted(pks[:2])
            assert all(tracking.get(target)["status"] == "found" for target in pks[2:])
        finally:
            release.set()
        for worker in workers:
            worker.result()
    assert all(not run._claimed(target, stores) for target in pks)


def test_requested_queue_put_hold_race_preserves_manual_terminal(monkeypatch):
    """A refused queue insertion cannot turn a newer APPLIED_MANUAL into TAILORED."""
    from core.apply_queue import ApplyQueue
    from tools import submit_hold

    tracking, pk = _synthetic_tracking()
    tracking.set_status(pk, Status.TAILORING, jd_read_revision=2,
                        apply_requested_at="2030-01-01T00:00:00+00:00",
                        resume_s3_key="resumes/synthetic-new.pdf")
    stores = SimpleNamespace(tracking=tracking)
    queue = ApplyQueue(tracking.r)
    pending_before, leases_before = queue.pending(), queue.in_flight()
    snapshots = []
    original_put = ApplyQueue.put

    def manual_before_put(self, *args, **kwargs):
        tracking.set_status(pk, Status.APPLIED_MANUAL,
                            confirmation_id="synthetic-confirmed",
                            applied_at="2030-01-02T00:00:00+00:00")
        submit_hold.mark(pk, tracking=tracking)
        snapshots.append(tracking.get(pk))
        # The real put checks the durable hold and refuses without enqueuing.
        return original_put(self, *args, **kwargs)

    monkeypatch.setattr(ApplyQueue, "put", manual_before_put)
    result = run._enqueue_apply(pk, stores, require_request=True)
    assert result["result"] == "failed" and result["reason"] == "uncertain"
    assert tracking.get(pk) == snapshots[0]
    assert submit_hold.is_held(pk, tracking=tracking)
    assert tracking.status_counts().get("applied_manual") == 1
    assert tracking.status_counts().get("tailored", 0) == 0
    assert queue.pending() == pending_before
    assert queue.in_flight() == leases_before


def test_requested_queue_duplicate_is_not_a_protection_refusal(monkeypatch):
    from core.apply_queue import ApplyQueue

    tracking, pk = _synthetic_tracking()
    tracking.set_status(pk, Status.TAILORING, jd_read_revision=3,
                        apply_requested_at="2030-01-01T00:00:00+00:00")
    stores = SimpleNamespace(tracking=tracking)
    queue = ApplyQueue(tracking.r)
    assert queue.put(pk, "example-co", requested=True)
    original = tracking.get(pk)
    pending = queue.pending()

    result = run._enqueue_apply(pk, stores, require_request=True)
    assert result["result"] == "already_queued" and result["queued"] is False
    assert tracking.get(pk) == original
    assert queue.pending() == pending


def test_requested_queue_post_put_terminal_wins_atomic_status_cas(monkeypatch):
    """Even a successful new insertion cannot authorize a stale row write."""
    from core.apply_queue import ApplyQueue
    from tools import submit_hold

    tracking, pk = _synthetic_tracking()
    tracking.set_status(pk, Status.TAILORING,
                        apply_requested_at="2030-01-01T00:00:00+00:00")
    stores = SimpleNamespace(tracking=tracking)
    snapshots = []
    original_update = tracking.update_if_status

    def manual_before_status_cas(target, expected, patch, *, expected_reader):
        tracking.set_status(pk, Status.APPLIED_MANUAL, confirmation_id="synthetic")
        submit_hold.mark(pk, tracking=tracking)
        snapshots.append(tracking.get(pk))
        return original_update(target, expected, patch, expected_reader=expected_reader)

    monkeypatch.setattr(tracking, "update_if_status", manual_before_status_cas)
    outcome = run._enqueue_apply(pk, stores, require_request=True)
    assert outcome["result"] == "conflict"
    assert tracking.get(pk) == snapshots[0]
    assert submit_hold.is_held(pk, tracking=tracking)
    # The pending item is subject to the existing dispatch/final-submit guards.
    assert [x["pk"] for x in ApplyQueue(tracking.r).pending()] == [pk]


@pytest.mark.parametrize("discovery_source", ["", "career_ops"])
def test_prepared_authority_revoked_between_wrapper_and_enqueue_get(
        monkeypatch, discovery_source):
    """Both ordinary and career_ops prepared results require a CURRENT request."""
    from core.apply_queue import ApplyQueue

    _quiet(monkeypatch)
    tracking, pk = _synthetic_tracking()
    request = "2030-01-01T00:00:00+00:00"
    tracking.set_status(pk, Status.FOUND, discovery_source=discovery_source,
                        apply_requested_at=request,
                        resume_s3_key="resumes/synthetic-new.pdf")
    stores = SimpleNamespace(tracking=tracking)
    calls = []

    async def prepared(*args, **kwargs):
        return {"result": "prepared", "pk": pk}

    original_enqueue = run._enqueue_apply

    def revoke_before_second_read(target, store, **kwargs):
        calls.append(kwargs)
        tracking.set_status(target, Status.TAILORING, apply_requested_at="")
        return original_enqueue(target, store, **kwargs)

    monkeypatch.setattr(run, "_run_job_async", prepared)
    monkeypatch.setattr(run, "_enqueue_apply", revoke_before_second_read)
    result = run.run_job(pk, stores, prepare_only=True)
    assert len(calls) == 1
    assert result["result"] == "refused"
    assert result["reason"] == "apply_request_or_preparation_not_current"
    assert tracking.get(pk)["apply_requested_at"] == ""
    assert tracking.get(pk)["status"] == "tailoring"
    assert ApplyQueue(tracking.r).pending() == []
    assert not tracking.r.exists(f"lock:job:{pk}")


def test_jd_attention_retry_invalidates_old_pdf_without_new_save(monkeypatch):
    """A PDF from the previous JD cannot authorize the recovered preparation."""
    from core.apply_queue import ApplyQueue

    tracking, pk = _synthetic_tracking()
    request = "2030-01-01T00:00:00+00:00"
    tracking.set_status(pk, Status.ERROR, fail_kind="jd_reader_exhausted",
                        jd_read_attempts=3, jd_read_revision=6,
                        resume_s3_key="resumes/old-posting.pdf",
                        resume_seed="synthetic-existing-seed",
                        apply_requested_at=request)
    stores = SimpleNamespace(tracking=tracking)
    monkeypatch.setattr("core.events.emit", lambda *args, **kw: None)
    monkeypatch.setattr(run, "_session_state",
                        lambda row, text: {"base_latex": "Test User seed"})
    monkeypatch.setattr(run, "_save_output", lambda *args: None)

    async def recovered(row, **kwargs):
        return GOOD

    class EmptySession:
        async def delete_session(self, **kwargs):
            return None

        async def get_session(self, **kwargs):
            return None

        async def create_session(self, **kwargs):
            return None

    class EmptyReviewRunner:
        def __init__(self, agent, **kwargs):
            pass

        async def run_async(self, **kwargs):
            if False:
                yield None  # No current-run PDF save or other ADK output.

    monkeypatch.setattr(run, "_session_service", lambda: EmptySession())
    _guarded_meta(monkeypatch, recovered)
    monkeypatch.setattr(run, "Runner", EmptyReviewRunner)
    result = run.retry_job(pk, stores)
    row = tracking.get(pk)
    assert result["result"] == "failed" and result["reason"] == "no_resume"
    assert row["resume_s3_key"] == ""
    assert row["resume_seed"] == "synthetic-existing-seed"
    # AC5: failed preparation clears the old request instead of reviving dispatch.
    assert not row["apply_requested_at"]
    assert row["jd_read_prepare_only"] is True
    assert ApplyQueue(tracking.r).pending() == []
    assert row["status"] == "failed"
    assert not tracking.r.exists(f"lock:job:{pk}")


@pytest.mark.parametrize("mode", ["auto", "gated"])
@pytest.mark.parametrize("requested", [False, True])
def test_actual_skip_then_retry_remains_review_only(
        monkeypatch, mode, requested):
    """Skipping an exhausted ERROR must not re-enter the full apply graph."""
    from agent import graph
    from core import flags
    from core.apply_queue import ApplyQueue
    import server

    tracking, pk = _synthetic_tracking()
    authority = "2030-01-01T00:00:00+00:00" if requested else ""
    tracking.set_status(pk, Status.ERROR, fail_kind="jd_reader_exhausted",
                        jd_read_attempts=3, jd_read_revision=6,
                        jd_read_retry_at=None, apply_requested_at=authority)
    stores = SimpleNamespace(tracking=tracking)
    monkeypatch.setattr(server, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(server, "make_stores", lambda *a, **kw: stores)
    app = server.create_app()
    skip = next(r.endpoint for r in app.routes
                if getattr(r, "path", "") == "/actions/skip/{pk}")
    assert skip(pk)["ok"] is True
    assert tracking.get(pk)["status"] == "skipped"
    assert tracking.get(pk)["jd_read_attempts"] == 3
    monkeypatch.setattr(flags, "apply_mode", lambda: mode)
    monkeypatch.setattr("core.events.emit", lambda *args, **kw: None)
    monkeypatch.setattr(run, "_session_state",
                        lambda row, text: {"base_latex": "Test User seed"})
    monkeypatch.setattr(run, "_save_output", lambda *args: None)

    class NoNetworkSession:
        async def delete_session(self, **kw):
            return None

        async def get_session(self, **kw):
            return None

        async def create_session(self, **kw):
            return None

    used_agents = []

    class GraphCapture:
        def __init__(self, agent, **kwargs):
            used_agents.append(agent)

    async def recovered(row, **kw):
        return GOOD

    async def prepared(runner, current_pk, message, current_stores, *, prepare_only, observed):
        assert prepare_only is True
        current_stores.tracking.set_status(
            current_pk, Status.TAILORED,
            resume_s3_key="resumes/synthetic-new.pdf")
        return {"result": "prepared", "pk": current_pk}

    monkeypatch.setattr(run, "_session_service", lambda: NoNetworkSession())
    _guarded_meta(monkeypatch, recovered)
    monkeypatch.setattr(run, "Runner", GraphCapture)
    monkeypatch.setattr(run, "_drive_async", prepared)
    result = run.retry_job(pk, stores)
    assert used_agents == [graph.review_agent]
    row = tracking.get(pk)
    assert row["status"] == "tailored"
    assert row["jd_read_attempts"] == 0
    assert row["jd_read_prepare_only"] is True
    assert row["apply_requested_at"] == authority
    queued = [item for item in ApplyQueue(tracking.r).pending()
              if item["pk"] == pk]
    if requested:
        assert result["result"] == "queued" and len(queued) == 1
    else:
        assert result["result"] == "prepared" and queued == []
    assert not tracking.r.exists(f"lock:job:{pk}")


def test_actual_reopen_refuses_exhaustion_after_skip(monkeypatch):
    from fastapi import BackgroundTasks
    from core.apply_queue import ApplyQueue
    import server

    tracking, pk = _synthetic_tracking()
    tracking.set_status(pk, Status.ERROR, jd_read_attempts=3,
                        fail_kind="jd_reader_exhausted", jd_read_revision=6)
    stores = SimpleNamespace(tracking=tracking)
    monkeypatch.setattr(server, "make_stores", lambda *a, **kw: stores)
    monkeypatch.setattr(server, "get_settings", lambda: SimpleNamespace())
    events = []
    monkeypatch.setattr("core.events.emit", lambda *args, **kw: events.append(args))
    app = server.create_app()
    skip = next(r.endpoint for r in app.routes
                if getattr(r, "path", "") == "/actions/skip/{pk}")
    reopen = next(r.endpoint for r in app.routes
                  if getattr(r, "path", "") == "/actions/reopen/{pk:path}")
    assert skip(pk)["ok"] is True
    before = tracking.get(pk)
    counts = tracking.status_counts()
    queue = ApplyQueue(tracking.r)
    pending, leases = queue.pending(), queue.in_flight()
    tasks = BackgroundTasks()
    result = reopen(pk, tasks)
    assert result["ok"] is False
    assert "Retry" in result["note"]
    assert tracking.get(pk) == before
    assert tracking.status_counts() == counts
    assert queue.pending() == pending and queue.in_flight() == leases
    assert tasks.tasks == [] and events == []


def test_retry_session_reset_failure_is_visible_and_second_retry_works(monkeypatch):
    from core.apply_queue import ApplyQueue

    tracking, pk = _synthetic_tracking()
    tracking.set_status(pk, Status.ERROR, fail_kind="jd_reader_exhausted",
                        jd_read_attempts=3, jd_read_revision=6,
                        resume_s3_key="resumes/old.pdf", apply_requested_at="")
    stores = SimpleNamespace(tracking=tracking)
    messages = []
    monkeypatch.setattr("core.events.emit",
                        lambda kind, **kw: messages.append((kind, kw)))

    async def reset_failed(target):
        raise RuntimeError("synthetic session backend unavailable")

    async def no_reader(*args, **kw):
        pytest.fail("Reader must not run after session reset failure")

    monkeypatch.setattr(run, "_reset_session", reset_failed)
    monkeypatch.setattr(run, "_run_job_async", no_reader)
    result = run.retry_job(pk, stores)
    assert result["result"] == "error"
    assert result["reason"] == "jd_retry_session_error"
    row = tracking.get(pk)
    assert row["status"] == "error"
    assert row["jd_read_revision"] == 8  # admitted + guarded attention
    assert row["jd_read_prepare_only"] is True
    assert row["jd_read_attempts"] == 0
    assert row["resume_s3_key"] == ""
    assert "Retry" in row["error"]
    assert row["apply_requested_at"] == ""
    assert [event for event, _ in messages] == ["error"]
    assert not tracking.r.exists(f"lock:job:{pk}")

    async def reset_ok(target):
        return None

    async def private_prepared(target, observed, stores, *, prepare_only):
        assert prepare_only is True
        tracking.set_status(target, Status.TAILORED,
                            resume_s3_key="resumes/new.pdf")
        return {"result": "prepared", "pk": target}

    monkeypatch.setattr(run, "_reset_session", reset_ok)
    monkeypatch.setattr(run, "_run_job_async", private_prepared)
    second = run.retry_job(pk, stores)
    assert second["result"] == "prepared"
    assert tracking.get(pk)["status"] == "tailored"
    assert tracking.get(pk)["jd_read_prepare_only"] is True
    assert ApplyQueue(tracking.r).pending() == []
    assert not tracking.r.exists(f"lock:job:{pk}")


def test_retry_reset_failure_conflicts_with_new_manual_terminal(monkeypatch):
    from tools import submit_hold

    tracking, pk = _synthetic_tracking()
    tracking.set_status(pk, Status.ERROR, jd_read_attempts=3,
                        jd_read_revision=6, fail_kind="jd_reader_exhausted")
    stores = SimpleNamespace(tracking=tracking)
    monkeypatch.setattr("core.events.emit", lambda *a, **kw: None)
    snapshots = []
    original_update = tracking.update_if_status

    def manual_before_error_settlement(target, old_status, patch, *, expected_reader):
        if patch.get("fail_kind") == "jd_retry_session_error":
            tracking.set_status(pk, Status.APPLIED_MANUAL,
                                confirmation_id="synthetic-confirmed")
            submit_hold.mark(pk, tracking=tracking)
            snapshots.append(tracking.get(pk))
        return original_update(target, old_status, patch,
                               expected_reader=expected_reader)

    async def reset_failed(target):
        raise RuntimeError("synthetic session failure")

    monkeypatch.setattr(tracking, "update_if_status", manual_before_error_settlement)
    monkeypatch.setattr(run, "_reset_session", reset_failed)
    result = run.retry_job(pk, stores)
    assert result["result"] == "conflict"
    assert tracking.get(pk) == snapshots[0]
    assert submit_hold.is_held(pk, tracking=tracking)
    assert not tracking.r.exists(f"lock:job:{pk}")


def test_retry_session_setup_failure_settles_after_successful_jd_read(monkeypatch):
    """Session creation failures must not strand a freshly recovered reader."""
    from core.apply_queue import ApplyQueue

    tracking, pk = _synthetic_tracking()
    tracking.set_status(pk, Status.ERROR, jd_read_attempts=3,
                        jd_read_revision=6, fail_kind="jd_reader_exhausted",
                        apply_requested_at="")
    stores = SimpleNamespace(tracking=tracking)
    events = []
    monkeypatch.setattr("core.events.emit",
                        lambda kind, **kw: events.append((kind, kw)))
    monkeypatch.setattr(run, "_session_state",
                        lambda row, text: {"base_latex": "Test User synthetic seed"})

    async def reset_ok(target):
        return None

    async def recovered(row, **kw):
        return GOOD

    class BrokenSession:
        async def get_session(self, **kw):
            raise RuntimeError("synthetic ADK session backend unavailable")

    monkeypatch.setattr(run, "_reset_session", reset_ok)
    _guarded_meta(monkeypatch, recovered)
    monkeypatch.setattr(run, "_session_service", lambda: BrokenSession())
    result = run.retry_job(pk, stores)
    row = tracking.get(pk)
    assert result["result"] == "error"
    assert result["reason"] == "jd_retry_session_error"
    assert result["attention_persisted"] is True
    assert row["status"] == "error"
    assert row["jd_read_revision"] == 9  # reset, successful JD, session failure
    assert row["jd_read_attempts"] == 0
    assert row["jd_read_prepare_only"] is True
    assert row["jd_text"] == GOOD
    assert "Retry" in row["error"]
    assert [name for name, _ in events if name == "error"] == ["error"]
    assert ApplyQueue(tracking.r).pending() == []
    assert not tracking.r.exists(f"lock:job:{pk}")


def test_retry_session_attention_storage_fault_is_bounded(monkeypatch):
    """An unavailable attention write is diagnosed without replaying the CAS."""
    tracking, pk = _synthetic_tracking()
    tracking.set_status(pk, Status.ERROR, jd_read_attempts=3,
                        jd_read_revision=6, fail_kind="jd_reader_exhausted")
    stores = SimpleNamespace(tracking=tracking)
    monkeypatch.setattr("core.events.emit", lambda *a, **kw: None)
    attempts = [0]
    original = tracking.update_if_status

    def failure_once(pk_value, status, patch, *, expected_reader):
        if patch.get("fail_kind") == "jd_retry_session_error":
            attempts[0] += 1
            raise RuntimeError("synthetic persistence unavailable")
        return original(pk_value, status, patch,
                        expected_reader=expected_reader)

    async def reset_failed(target):
        raise RuntimeError("synthetic session unavailable")

    monkeypatch.setattr(run, "_reset_session", reset_failed)
    monkeypatch.setattr(tracking, "update_if_status", failure_once)
    result = run.retry_job(pk, stores)
    assert attempts == [1]
    assert result["result"] == "error"
    assert result["reason"] == "jd_tracking_storage_error"
    assert result["attention_persisted"] is False
    assert "could not be persisted" in result["detail"]
    assert tracking.get(pk)["status"] == "tailoring"
    assert not tracking.r.exists(f"lock:job:{pk}")



# Issue #299: historical hrefs remain persisted relative; only their operational
# read target is resolved from a unique, explicitly matching current source.
_WORKDAY_BOARD = "https://wd.example.invalid/wday/cxs/fake-tenant/fake-site/jobs"
_WORKDAY_CAREERS = "https://wd.example.invalid/en/fake-site"
_WORKDAY_PATH = "/job/Example/Platform_ABC-17"
_WORKDAY_URL = "https://wd.example.invalid/fake-site/job/Example/Platform_ABC-17"


def _source_watchlist(monkeypatch, *, ats="workday", careers=None, board=None,
                      extras=()):
    from discovery.watchlist import CompanyConfig
    entries = [CompanyConfig(
        name="example-co", ats=ats,
        careers_url=careers if careers is not None else (
            _WORKDAY_CAREERS if ats == "workday" else "https://example.test/careers/"),
        board=board if board is not None else (_WORKDAY_BOARD if ats == "workday" else ""),
    ), *extras]
    monkeypatch.setattr(run, "get_settings",
                        lambda: SimpleNamespace(config_dir="/synthetic-only"))
    monkeypatch.setattr("discovery.watchlist.load_watchlist", lambda _: entries)
    return entries


def _original_row(*, ats="workday", cached="Test role"):
    return {
        "pk": "example-co#Platform_ABC-17" if ats == "workday" else "example-co#42",
        "company": "example-co", "ats": ats,
        "job_id": "Platform_ABC-17" if ats == "workday" else "42",
        "jd_url": _WORKDAY_PATH if ats == "workday" else "/jobs/42?lang=en",
        "status": "found", "jd_text": cached,
        "discovery_source": "crawler",
    }


@pytest.mark.parametrize("ats,original_href,expected", [
    ("workday", _WORKDAY_PATH, _WORKDAY_URL),
    ("custom", "/jobs/42?lang=en", "https://example.test/jobs/42?lang=en"),
    ("custom", "jobs/42?lang=en", "https://example.test/careers/jobs/42?lang=en"),
])
def test_historic_reader_uses_exact_operational_target_before_cached_text(
    monkeypatch, ats, original_href, expected,
):
    from tools import posting_guard as pg

    _source_watchlist(monkeypatch, ats=ats)
    row = _original_row(ats=ats, cached="Cached text " * 75)
    row["jd_url"] = original_href
    original = dict(row)
    html = ('<html><head><link rel="canonical" href="' + expected +
            '"><script type="application/ld+json">' +
            json.dumps({"@type": "JobPosting", "identifier": {"value": row["job_id"]},
                        "url": expected}) + '</script></head><body><h1>Test role</h1>' +
            "<p>Build trustworthy offline systems. " * 35 + "</p></body></html>")
    meta = pg.analyze(html, html_variants=[html], source_required=True)
    meta.update(source_kind="http_html", source_url=expected,
                source_identity=jd._html_source_identity(html))
    requested = []

    def read(target, kind):
        requested.append(target)
        return meta

    monkeypatch.setattr(jd, "fetch_jd_meta", read)
    selected = asyncio.run(run._jd_meta(row))
    assert requested == [expected]
    assert selected["reader_effective_url"] == expected
    assert jd.matched_source_witness(row, selected)
    assert selected["posting_guard"]["source_verified"] is True
    assert row == original  # pk, URL, status and all reader witnesses unchanged


@pytest.mark.parametrize("case", [
    "missing", "duplicate", "board_absent", "board_foreign", "board_site",
    "careers_foreign", "careers_site", "id_mismatch", "userinfo", "backslash",
    "protocol_relative", "wrong_port", "traversal",
])
def test_workday_missing_or_unsafe_source_never_reaches_reader_or_cache(
    monkeypatch, case,
):
    from discovery.watchlist import CompanyConfig

    entries = _source_watchlist(monkeypatch)
    row = _original_row(cached="Previously cached text " * 40)
    if case == "missing":
        entries.clear()
    elif case == "duplicate":
        entries.append(entries[0].model_copy(deep=True))
    elif case == "board_absent":
        entries[0].board = ""
    elif case == "board_foreign":
        entries[0].board = _WORKDAY_BOARD.replace("wd.example.invalid", "foreign.example")
    elif case == "board_site":
        entries[0].board = _WORKDAY_BOARD.replace("fake-site", "other-site")
    elif case == "careers_foreign":
        entries[0].careers_url = "https://foreign.example/en/fake-site"
    elif case == "careers_site":
        entries[0].careers_url = "https://wd.example.invalid/en/other-site"
    elif case == "id_mismatch":
        row["job_id"] = "Other_ID-99"
    elif case == "userinfo":
        entries[0].careers_url = "https://user@wd.example.invalid/en/fake-site"
    elif case == "backslash":
        row["jd_url"] = "/job/Example\\Platform_ABC-17"
    elif case == "protocol_relative":
        row["jd_url"] = "//foreign.example/job/Example/Platform_ABC-17"
    elif case == "wrong_port":
        entries[0].careers_url = "https://wd.example.invalid:8443/en/fake-site"
    elif case == "traversal":
        row["jd_url"] = "/job/%2e%2e/Platform_ABC-17"
    monkeypatch.setattr(jd, "fetch_jd_meta",
                        lambda *args: pytest.fail("unsafe source reached JD reader"))
    with pytest.raises(run.JDURLUnresolvable):
        asyncio.run(run._jd_meta(row))


@pytest.mark.parametrize("conflict", [False, True])
def test_unresolvable_settles_without_reader_outage_budget_or_cache(
    monkeypatch, conflict,
):
    from core.models import JobRecord

    pk = "example-co#job-1"
    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    assert tracking.put_new(JobRecord(
        company="example-co", job_id="job-1", ats="custom",
        title="Test role", jd_url="/jobs/job-1",
        jd_text="Cached text " * 60))
    # JD/guard text is bound at creation; status writes may not overwrite it.
    tracking.set_status(pk, Status.FOUND, jd_read_attempts=1,
                        jd_read_revision=6, jd_read_retry_at=None)
    stores = SimpleNamespace(tracking=tracking)
    _source_watchlist(monkeypatch, ats="custom")[0].careers_url = ""
    monkeypatch.setattr("core.events.emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "_session_service",
                        lambda: pytest.fail("Unresolved URL cannot reach ADK/cache"))
    monkeypatch.setattr(jd, "fetch_jd_meta",
                        lambda *args: pytest.fail("Unresolved URL cannot reach HTTP/browser"))
    if conflict:
        original = tracking.update_if_status

        def race(target, status, patch, *, expected_reader):
            if patch.get("fail_kind") == "jd_url_unresolvable":
                tracking.set_status(pk, Status.TAILORING, jd_read_revision=8)
            return original(target, status, patch, expected_reader=expected_reader)

        monkeypatch.setattr(tracking, "update_if_status", race)
    outcome = run.run_job(pk, stores)
    row = tracking.get(pk)
    if conflict:
        assert outcome["result"] == "conflict"
        assert row["status"] == "tailoring"
    else:
        assert outcome["reason"] == "jd_url_unresolvable"
        assert row["status"] == "error"
        assert row["fail_kind"] == "jd_url_unresolvable"
        assert row["jd_read_revision"] == 8
        assert row["jd_read_retry_at"] is None
        assert row["jd_read_error"] == ""
    assert row["jd_url"] == "/jobs/job-1"
    assert row["jd_read_attempts"] == 1
    assert row["jd_text"] == "Cached text " * 60


def test_synthetic_corrected_base_retry_is_review_only_with_zero_apply_calls(
    monkeypatch,
):
    from core.apply_queue import ApplyQueue

    tracking, pk = _synthetic_tracking()
    tracking.set_status(pk, Status.ERROR, jd_url="/jobs/job-1",
                        fail_kind="jd_url_unresolvable", jd_read_attempts=0,
                        jd_read_revision=5,
                        apply_requested_at="synthetic-prior-request")
    stores = SimpleNamespace(tracking=tracking)
    _source_watchlist(monkeypatch, ats="custom")
    monkeypatch.setattr("core.events.emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "_reset_session", lambda _: asyncio.sleep(0))
    monkeypatch.setattr(run, "_enqueue_apply",
                        lambda *args, **kw: pytest.fail("Historical retry must not queue"))

    async def review_only(target, admitted, current_stores, *, prepare_only):
        assert prepare_only is True
        assert admitted["jd_read_prepare_only"] is True
        current_stores.tracking.set_status(target, Status.TAILORED)
        return {"result": "prepared", "pk": target}

    monkeypatch.setattr(run, "_run_job_async", review_only)
    outcome = run.retry_job(pk, stores)
    row = tracking.get(pk)
    assert outcome["result"] == "prepared"
    assert row["jd_read_prepare_only"] is True
    assert row["apply_requested_at"] == "synthetic-prior-request"
    assert row["jd_url"] == "/jobs/job-1"
    assert ApplyQueue(tracking.r).pending() == []


@pytest.mark.parametrize("entry", ["ordinary", "review_retry"])
def test_positive_workday_html_and_prior_request_never_authorize_application(
    monkeypatch, entry,
):
    from agent import graph
    from core.models import JobRecord
    from tools import posting_guard as pg
    from core.apply_queue import ApplyQueue

    _source_watchlist(monkeypatch)
    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    row = _original_row()
    pk = row["pk"]
    assert tracking.put_new(JobRecord(
        company=row["company"], job_id=row["job_id"], ats=row["ats"],
        title="Test role", jd_url=row["jd_url"], jd_text="Test role"))
    tracking.set_status(pk, Status.FOUND if entry == "ordinary" else Status.ERROR,
                        fail_kind="" if entry == "ordinary" else "jd_url_unresolvable",
                        jd_read_attempts=0, apply_requested_at="synthetic-request")
    stores = SimpleNamespace(tracking=tracking)
    html = ('<html><head><link rel="canonical" href="' + _WORKDAY_URL +
            '"><script type="application/ld+json">' +
            json.dumps({"@type": "JobPosting", "url": _WORKDAY_URL,
                        "identifier": {"value": row["job_id"]}}) +
            '</script></head><body><h1>Test role</h1><p>' +
            ("Build reliable products and resilient infrastructure. " * 16) +
            '</p></body></html>')
    meta = pg.analyze(html, html_variants=[html], source_required=True)
    meta.update(source_kind="http_html", source_url=_WORKDAY_URL,
                source_identity=jd._html_source_identity(html))
    asks = []
    monkeypatch.setattr(jd, "fetch_jd_meta",
                        lambda url, kind: (asks.append(url) or meta))
    monkeypatch.setattr("core.events.emit", lambda *args, **kw: None)
    monkeypatch.setattr(run, "_session_state",
                        lambda *args: {"base_latex": "Test User seed"})
    monkeypatch.setattr(run, "_save_output", lambda *args: None)
    monkeypatch.setattr(run, "_reset_session", lambda _: asyncio.sleep(0))
    monkeypatch.setattr(run, "_enqueue_apply",
                        lambda *args, **kwargs: pytest.fail("Apply queue forbidden"))
    monkeypatch.setattr(run, "_apply_direct",
                        lambda *args, **kwargs: pytest.fail("Direct apply forbidden"))

    class EmptySessions:
        async def get_session(self, **kwargs):
            return None

        async def create_session(self, **kwargs):
            return None

    class ReviewRunner:
        def __init__(self, agent, **kwargs):
            assert agent is graph.review_agent, "root_agent would be apply-capable"
            self.agent = agent

        async def run_async(self, **kwargs):
            if False:
                yield None

    monkeypatch.setattr(run, "_session_service", lambda: EmptySessions())
    monkeypatch.setattr(run, "Runner", ReviewRunner)
    outcome = (run.run_job(pk, stores) if entry == "ordinary"
               else run.retry_job(pk, stores))
    final = tracking.get(pk)
    assert outcome["result"] == "gated"
    assert outcome["reason"] == "posting_guard_review_required"
    assert asks == [_WORKDAY_URL]
    assert final["jd_url"] == _WORKDAY_PATH
    assert final["apply_requested_at"] == "synthetic-request"
    assert final["status"] == "needs_human"
    assert ApplyQueue(tracking.r).pending() == []


@pytest.mark.parametrize("bad_href", [
    "jobs/../42?lang=en",
    "jobs/%2e%2e/42?lang=en",
    "jobs/%2felse",
    "//foreign.example/jobs/42",
    "jobs/42#fragment",
    "foreign:jobs/42",
    "jobs\\42",
])
def test_custom_historical_path_relative_rejects_unsafe_source_before_cache(
    monkeypatch, bad_href,
):
    _source_watchlist(monkeypatch, ats="custom")
    row = _original_row(ats="custom", cached="Previously cached text " * 50)
    row["jd_url"] = bad_href
    monkeypatch.setattr(jd, "fetch_jd_meta",
                        lambda *args: pytest.fail("Unsafe href reached JD reader"))
    with pytest.raises(run.JDURLUnresolvable):
        asyncio.run(run._jd_meta(row))


@pytest.mark.parametrize("entry", ["direct", "queued", "resume", "root_agent"])
def test_historical_relative_prior_request_cannot_enter_application_handoff(
    monkeypatch, entry,
):
    """A persisted relative URL is not Apply authority even after preparation."""
    from core.models import JobRecord
    from tools import browser_apply

    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    row = _original_row()
    pk = row["pk"]
    assert tracking.put_new(JobRecord(
        company=row["company"], job_id=row["job_id"], ats=row["ats"],
        title="Test role", jd_url=row["jd_url"], jd_text="Test role"))
    tracking.set_status(pk, Status.TAILORED,
                        apply_requested_at="synthetic-prior-request")
    stores = SimpleNamespace(tracking=tracking)
    before = tracking.get(pk)
    monkeypatch.setattr(browser_apply, "apply",
                        lambda *args, **kw: pytest.fail("Application browser reached"))
    monkeypatch.setattr(run, "_enqueue_apply",
                        lambda *args, **kw: pytest.fail("Application queue reached"))

    class NoSession:
        async def get_session(self, **kwargs):
            pytest.fail("Historical relative resume reached an agent session")

    monkeypatch.setattr(run, "_session_service", lambda: NoSession())
    monkeypatch.setattr(run, "Runner",
                        lambda *args, **kw: pytest.fail("Apply-capable agent created"))
    if entry == "direct":
        outcome = asyncio.run(run._apply_direct(pk, stores))
    elif entry == "queued":
        monkeypatch.setattr(run, "make_stores", lambda: stores)

        class FakeLease:
            def __init__(self):
                self.finished = []

            def done(self, item):
                self.finished.append(item)

            def retry(self, *args):
                pytest.fail("Historical relative dispatch retried")

        lease = FakeLease()
        item = {"pk": pk}
        outcome = run.run_queued(item, lease)
        assert lease.finished == [item]
    elif entry == "resume":
        outcome = asyncio.run(run._resume_job_async(
            pk, "synthetic-answer", "synthetic-call", stores))
    else:
        class ApplyCapableRunner:
            agent = object()

            def run_async(self, **kwargs):
                pytest.fail("Apply-capable agent invoked")

        outcome = asyncio.run(run._drive_async(
            ApplyCapableRunner(), pk, object(), stores))

    assert outcome["result"] == "gated"
    assert outcome["reason"] == "posting_guard_review_required"
    assert tracking.get(pk) == before
