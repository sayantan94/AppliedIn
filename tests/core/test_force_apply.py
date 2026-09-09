"""An explicit score override affects one role and still stops at final review."""
from types import SimpleNamespace

import fakeredis
import pytest
from fastapi import BackgroundTasks

import server
from agent import run
from core.apply_queue import ApplyQueue
from core.models import Status
from core.storage.local import RedisTracking


@pytest.fixture
def force(monkeypatch):
    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    tracking.set_status('meta#low', Status.SKIPPED, company='Meta', title='Engineer',
                        skip_reason='low_score', match_score=6)
    tracking.set_status('other#low', Status.SKIPPED, company='Other',
                        skip_reason='low_score', match_score=5)
    stores = SimpleNamespace(tracking=tracking)
    monkeypatch.setattr(server, 'make_stores', lambda *a: stores)
    monkeypatch.setattr('core.events.emit', lambda *a, **kw: None)
    monkeypatch.setattr(run, '_min_score', lambda: 7)
    endpoints = {r.path: r.endpoint for r in server.create_app().routes if hasattr(r, 'endpoint')}
    return stores, endpoints['/actions/force-apply/{pk:path}']


def test_force_prepares_exactly_one_role_and_never_queues_submission(force, monkeypatch):
    stores, endpoint = force
    calls = []
    monkeypatch.setattr(run, 'run_job', lambda pk, s, **kw: calls.append((pk, kw)))
    background = BackgroundTasks()
    result = endpoint('meta#low', background)
    assert result['ok'] and result['next'] == 'review'
    row = stores.tracking.get('meta#low')
    assert row['score_override'] is True and row['score_override_at']
    assert row['match_score'] == 6
    assert stores.tracking.get('other#low')['status'] == 'skipped'
    assert not endpoint('meta#low', BackgroundTasks())['ok'], 'A repeat click cannot start a second preparation'
    task = background.tasks[0]
    task.func(*task.args, **task.kwargs)
    assert calls == [('meta#low', {'prepare_only': True})]
    assert not ApplyQueue(stores.tracking.r).pending()


@pytest.mark.parametrize('status,reason,confirmation', [
    ('applied', 'low_score', ''), ('applied_manual', 'low_score', ''),
    ('submitting', 'low_score', ''), ('skipped', 'review_rejected', ''),
    ('failed', 'no_sponsorship', ''), ('skipped', 'low_score', 'confirmed'),
])
def test_force_cannot_override_other_decisions_or_a_submission(force, status, reason, confirmation):
    stores, endpoint = force
    stores.tracking.set_status('meta#low', Status(status), skip_reason=reason, confirmation_id=confirmation)
    background = BackgroundTasks()
    assert not endpoint('meta#low', background)['ok']
    assert not background.tasks
    assert not stores.tracking.get('meta#low').get('score_override')


def test_score_override_does_not_lower_the_bar_for_other_roles(force):
    stores, endpoint = force
    assert endpoint('meta#low', BackgroundTasks())['ok']
    assert run._score_gate('meta#low', '{"score":6}', stores) is None
    assert stores.tracking.get('meta#low')['match_score'] == 6
    assert run._score_gate('other#low', '{"score":6}', stores)['result'] == 'skipped'


def test_force_does_not_treat_a_model_refusal_as_a_score(force):
    stores, endpoint = force
    endpoint('meta#low', BackgroundTasks())
    result = run._score_gate('meta#low', '{"score":0,"reason":"I am unable to access the resume"}', stores)
    assert result['reason'] == 'scorer_refused'


def test_queued_role_cannot_be_forced_during_dispatch(force):
    stores, endpoint = force
    q = ApplyQueue(stores.tracking.r)
    q.put('meta#low', 'Meta')
    assert not endpoint('meta#low', BackgroundTasks())['ok']
    q.next()
    assert not endpoint('meta#low', BackgroundTasks())['ok']
