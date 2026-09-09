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
    tracking.set_status('meta#1', Status.FOUND, company='Meta', jd_url='https://x/1', jd_text='Engineer')
    stores = SimpleNamespace(tracking=tracking)
    def unavailable(*args, **kwargs):
        raise jd.PostingReadUnavailable(LIMIT)
    monkeypatch.setattr(jd, 'fetch_jd', unavailable)
    monkeypatch.setattr('core.events.emit', lambda *a, **kw: None)
    monkeypatch.setattr(run, '_session_service', lambda: pytest.fail('No tailoring without a posting'))
    result = run.run_job('meta#1', stores, prepare_only=True)
    row = tracking.get('meta#1')
    assert result['result'] == 'deferred'
    assert row['status'] == 'found'
    assert row['jd_read_error'] == LIMIT
    assert not row.get('fail_kind')
    assert not tracking.r.exists('lock:job:meta#1')


def test_existing_usable_description_survives_reader_outage(monkeypatch):
    def unavailable(*args, **kwargs):
        raise jd.PostingReadUnavailable(LIMIT)
    monkeypatch.setattr(jd, 'fetch_jd', unavailable)
    text = 'Build infrastructure. ' * 10
    assert asyncio.run(run._jd_text({'jd_url':'https://x/1', 'jd_text':text})).strip() == text.strip()


def test_missing_cli_and_empty_batch_do_not_break_with_gone_contract(monkeypatch):
    assert jd.read_postings([], with_gone=True) == ({}, set())
    monkeypatch.setattr(jd, 'available', lambda: (False, 'Claude Code is not installed'))
    with pytest.raises(jd.PostingReadUnavailable, match='not installed'):
        jd.read_postings(['https://x/1'], with_gone=True)
