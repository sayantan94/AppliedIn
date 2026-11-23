"""The daemon must use the selected profile, not the external worker's habits.

All browser/model calls are fakes. In particular, a failed submit transport must
stay uncertain even when its error looks like a retryable browser outage.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools import browser_runtime as runtime
from tools import browser_skill as bsk
from tools import browser_skill_apply as forms
from tools import claude_chrome, jd

GOOD = 'Responsibilities and qualifications for the engineering program. ' * 20


def _seed_clean(tracking, company, job_id, url, text=GOOD):
    """Persist an owned, inspected synthetic JD before exercising form/lease gates."""
    from core.models import JobRecord
    from tools import posting_guard as pg

    checked = pg.analyze(text)
    assert tracking.put_new(JobRecord(
        company=company, job_id=job_id, title='Test role', jd_url=url,
        jd_text=checked['text'], posting_guard=checked['posting_guard']))
    return checked


def enable(tmp_path):
    (tmp_path / 'browser.local.yaml').write_text('engine: browser_skill\nbrowser: dedicated\n')


def target(**values):
    result = {'selector': '#field', 'label': 'First name', 'question': '', 'type': 'text',
            'tag': 'input', 'disabled': False, 'has_value': False, 'value': '',
            'required': False, 'in_form': True, 'submit': False,
            'form_selector': '#application',
            'form_action': 'https://employer.test/job/1', 'formaction': '',
            **values}
    result.setdefault('native_form_owner_selector',
                      result['form_selector'] if result['in_form'] else '')
    return result


def grant(field, value, *, selector='#field',
          url='https://employer.test/job/1', question=''):
    return {'value': value, 'selector': selector, 'url': url,
            'label': field, 'question': question, 'tag': 'input',
            'type': 'text', 'form_selector': '#application',
            'form_action': 'https://employer.test/job/1'}


def page(*controls, text=GOOD, application_marker=False):
    # Fill/select disclosure tests must supply a real structural native marker.
    # Keep unrelated pages (discovery/confirmation/submit refusal) unchanged.
    observed = list(controls)
    if application_marker:
        observed.append(target(selector='#submit-application', tag='button',
                               type='submit', label='Submit application',
                               submit=True, required=False))
    return {'url': 'https://employer.test/job/1', 'title': 'Program manager',
            'text': text, 'description': GOOD, 'controls': observed,
            'truncated': False, 'unsupported_frames': [], 'shadow_roots': False,
            'inventory_verified': True, 'opaque_controls': False}




@pytest.fixture(autouse=True)
def _legacy_guard_tests_use_a_separate_observation_stub(monkeypatch, request):
    """Most legacy guards isolate transport; apply behavior needs both real readers."""
    if request.node.name == 'test_required_owner_gate_binds_one_live_unanswered_control_per_resume':
        return
    async def observed(session, current, **kwargs):
        return current, {c['selector']: {
            'visible': True, 'readonly': False, 'disabled': bool(c.get('disabled')),
            'inert': False,
            'visible_text': c['label'] if c.get('type') in {'checkbox', 'radio'} else ''}
            for c in current['controls']}
    monkeypatch.setattr(forms, '_observe_controls', observed)

def test_private_config_preserves_original_engine_and_rejects_unknown_values(tmp_path):
    assert runtime.configuration()['engine'] == 'chrome'
    enable(tmp_path)
    cfg = runtime.configuration()
    assert cfg['engine'] == 'browser_skill' and cfg['browser'] == 'dedicated'
    assert cfg['user_data_dir'].endswith('/.local/share/appliedin/chrome')
    (tmp_path / 'browser.local.yaml').write_text('engine: imaginary\n')
    ready, reason = runtime.available()
    assert not ready
    assert 'Unsupported browser engine' in reason


def test_real_cli_browser_list_format_and_errors_do_not_leak_page_data(monkeypatch, tmp_path):
    enable(tmp_path)
    payload = [{'instance_id': 'dedicated', 'unresponsive': False, 'version_skew': False}]
    monkeypatch.setattr(bsk, '_executable', lambda: '/fake/bsk')
    monkeypatch.setattr(bsk.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        stdout=json.dumps(payload), returncode=0))
    assert bsk.available() == (True, '')
    with pytest.raises(bsk.Unavailable) as exc:
        bsk._result(json.dumps({'ok': False, 'code': 'offline', 'message': 'PRIVATE ANSWER'}), 2)
    assert 'offline' in str(exc.value)
    assert 'PRIVATE' not in str(exc.value)


@pytest.mark.parametrize('browsers', [[], [{'instance_id': 'another'}],
                                     [{'instance_id': 'dedicated', 'unresponsive': True}]])
def test_missing_selected_profile_never_falls_back(monkeypatch, tmp_path, browsers):
    enable(tmp_path)
    monkeypatch.setattr(bsk, '_sync', lambda *a, **kw: {'browsers': browsers})
    monkeypatch.setattr('tools.browser_profile.profile_pids', lambda directory: [123])
    monkeypatch.setattr(claude_chrome, 'available', lambda: pytest.fail('No Claude fallback'))
    ready, reason = runtime.available()
    assert not ready and 'not connected' in reason


async def test_session_pins_profile_and_stops_only_its_own_window(monkeypatch, tmp_path):
    enable(tmp_path)
    calls = []
    monkeypatch.setattr(bsk, 'available', lambda: (True, ''))
    monkeypatch.setattr('tools.browser_profile.ensure_browser', lambda: (True, ''))
    async def command(*args, **kw):
        calls.append(args)
        if args[:2] == ('session', 'start'):
            return {'session_id': 'ours', 'browser_instance_id': 'dedicated'}
        return {'ok': True}
    monkeypatch.setattr(bsk, 'command', command)
    with pytest.raises(ValueError):
        async with bsk.Session('jd'):
            raise ValueError('posting failure')
    assert calls[0][2:4] == ('--browser', 'dedicated')
    assert calls[-1] == ('session', 'stop', 'ours')
    assert not bsk._LIVE


async def test_mismatched_profile_is_closed_before_navigation(monkeypatch, tmp_path):
    enable(tmp_path)
    monkeypatch.setattr(bsk, 'available', lambda: (True, ''))
    monkeypatch.setattr('tools.browser_profile.ensure_browser', lambda: (True, ''))
    command = AsyncMock(side_effect=[{'session_id': 'wrong', 'browser_instance_id': 'other'}, {}])
    monkeypatch.setattr(bsk, 'command', command)
    with pytest.raises(bsk.Unavailable, match='mismatch'):
        async with bsk.Session('jd'):
            pytest.fail('Must not act in another profile')
    assert command.call_args_list[-1].args == ('session', 'stop', 'wrong')


async def test_selected_jd_and_apply_dispatch_never_call_claude(monkeypatch, tmp_path):
    enable(tmp_path)
    monkeypatch.setattr(claude_chrome, 'run_task', AsyncMock(side_effect=AssertionError('Claude')))
    monkeypatch.setattr(claude_chrome, 'apply_chrome',
                        AsyncMock(side_effect=AssertionError('Claude')))
    read = AsyncMock(return_value=({'title': 'Program manager', 'description': GOOD}, ''))
    apply = AsyncMock(return_value={'status': 'gate', 'question': 'Review'})
    monkeypatch.setattr(bsk, 'run_task', read)
    monkeypatch.setattr(bsk, 'apply_chrome', apply)
    assert (await runtime.run_task('read', report_key='description'))[0]['description'] == GOOD
    assert (await runtime.apply('url', 'Company', {}, 'chatgpt/model'))['status'] == 'gate'
    assert read.await_count == apply.await_count == 1


def test_pipeline_reader_uses_browser_skill_and_outage_remains_retryable(monkeypatch, tmp_path):
    enable(tmp_path)
    monkeypatch.setattr(jd, '_browser_retry', (0, ''))
    monkeypatch.setattr(jd, '_from_ats', lambda url: None)
    monkeypatch.setattr(jd, '_get', lambda *args: None)
    monkeypatch.setattr(bsk, 'available', lambda: (True, ''))
    monkeypatch.setattr(bsk, 'run_task', AsyncMock(return_value=(
        {'title': 'Program manager', 'description': GOOD}, '')))
    monkeypatch.setattr(claude_chrome, 'run_task', AsyncMock(side_effect=AssertionError('Claude')))
    assert jd.fetch_jd('https://employer.test/job/1') == GOOD.strip()
    assert runtime.is_infrastructure('BrowserSkill unavailable: profile disconnected')
    assert runtime.is_disconnected('BrowserSkill unavailable: profile disconnected')


@pytest.mark.parametrize(('question', 'value'), [
    ('Gender', 'Male'), ('Disability', 'Yes'), ('Race', 'Asian'),
    ('Veteran status', 'I am a veteran'), ('Are you in a restricted country?', 'Yes'),
    ('First name', 'TODO'),
])
async def test_unsafe_values_are_refused_before_a_browser_write(question, value):
    # ARCH R1 explicitly accepts ungated preparation; item 3/5 is enforced
    # by check_pre_submit on fresh form readback, not by a write-time refusal.
    session = SimpleNamespace(call=AsyncMock())
    await forms.execute(session, page(target(label=question)),
                        {'action': 'fill', 'selector': '#field', 'fact': 'Answer'},
                        facts={'Answer': value}, filled={}, resume_path='',
                        company='Company', jd_text='', resume_tex='', github='')
    session.call.assert_awaited_once_with('fill', '#field', '--value', value)

@pytest.mark.parametrize(('question', 'value'), [
    ('Disability', 'No'), ('Gender', 'Prefer not to say'),
    ('Restricted country', 'None of the above'),
])
def test_safe_declines_and_negative_sanctions_answers_are_not_left_blank(question, value):
    assert forms.guarded_value(target(label=question), value) == value


async def test_model_text_cannot_replace_approved_fact(monkeypatch):
    session = SimpleNamespace(call=AsyncMock())
    monkeypatch.setattr(forms, '_row', lambda pk: {
        'pk': pk, 'status': 'submitting',
        'human_approved_answers': {'First name': grant('First name', 'Approved')}})
    filled = {}
    await forms.execute(session, page(target(), application_marker=True),
                        {'action': 'fill', 'selector': '#field', 'fact': 'First name',
                         'value': 'Invented'},
                        facts={'First name': 'Approved'}, filled=filled, resume_path='',
                        company='Company', jd_text='', resume_tex='', github='',
                        pk='Company#1')
    session.call.assert_awaited_once_with('fill', '#field', '--value', 'Approved')
    assert filled == {'#field': 'Approved'}


async def test_click_cannot_bypass_guarded_choice_or_submit():
    for control in [target(type='radio'), target(type='checkbox'),
                    target(tag='button', type='submit', label='Submit application', submit=True)]:
        session = SimpleNamespace(call=AsyncMock())
        with pytest.raises(forms.Gate):
            await forms.execute(session, page(control), {'action': 'click', 'selector': '#field'},
                                facts={}, filled={}, resume_path='', company='Company',
                                jd_text='', resume_tex='', github='')
        session.call.assert_not_awaited()


def test_native_option_must_match_both_the_dom_and_the_approved_answer():
    control = target(tag='select', type='select', label='Gender',
                     options=[{'value': '1', 'label': 'Male'},
                              {'value': '2', 'label': 'Prefer not to say'}])
    with pytest.raises(forms.Gate):
        forms.choose_value(control, 'Male', '1')
    assert forms.choose_value(control, 'Prefer not to say', '2') == '2'
    with pytest.raises(forms.Gate):
        forms.choose_value(control, 'Prefer not to say', '1')



def test_final_form_requires_staged_attachment_and_current_required_readback():
    with pytest.raises(forms.Gate, match='staged résumé'):
        forms.check_form(page(target()), {}, False)
    attachment = target(selector='#resume', type='file', label='Resume',
                        files=['Resume.pdf'])
    with pytest.raises(forms.Gate, match='required application field is empty'):
        forms.check_form(page(target(required=True), attachment),
                         {'#field': 'Model-only answer'}, True, 'Resume.pdf')


def test_success_requires_new_page_evidence_and_no_remaining_submit_control():
    before = page(text='Fill the application')
    success = page(text='Thank you for applying')
    assert forms.confirmation(before, success)
    assert not forms.confirmation(success, success)
    assert not forms.confirmation(before, page(target(label='Submit', submit=True),
                                             text='Thank you for applying'))
    assert not forms.confirmation(before, page(text='The site can’t be reached'))


def test_stale_resume_and_duplicates_are_rechecked_before_submit(monkeypatch, tmp_path):
    import fakeredis
    # Faking the row alone leaves the independent-key guard querying localhost.
    # Keep this résumé/duplicate regression offline, even without a Redis server.
    from core.apply_queue import ApplyQueue
    from tools import posting_guard as pg
    checked = pg.analyze(GOOD)
    row = {'pk': 'company#1', 'company': 'Company', 'status': 'submitting',
           'resume_tex_key': 'r.tex', 'resume_seed': 'old',
           'jd_text': checked['text'], 'posting_guard': checked['posting_guard']}
    tracking = SimpleNamespace(r=fakeredis.FakeRedis(decode_responses=True),
                               get_current=lambda pk: dict(row))
    monkeypatch.setattr('core.stores.make_stores', lambda: SimpleNamespace(tracking=tracking))
    pdf = tmp_path / 'Resume.pdf'
    pdf.write_bytes(b'pdf')
    monkeypatch.setattr(forms, '_row', lambda pk: row)
    monkeypatch.setattr('agent.run.seed_fingerprint', lambda: 'current')
    monkeypatch.setattr('tools.browser_apply._duplicate_refusal', lambda pk: None)
    queue = ApplyQueue(tracking.r)
    assert queue.put('company#1', 'Company')
    item = queue.next(only='Company')
    assert item is not None
    try:
        with pytest.raises(forms.Gate, match='base changed'):
            forms.check_dispatch('company#1', str(pdf))
        row['resume_seed'] = 'current'
        forms.check_dispatch('company#1', str(pdf))
        monkeypatch.setattr('tools.browser_apply._duplicate_refusal', lambda pk: {'status': 'failed'})
        with pytest.raises(forms.Gate, match='duplicate'):
            forms.check_dispatch('company#1', str(pdf))
    finally:
        queue.done(item)


@pytest.mark.parametrize('state', [
    'no_lease', 'pk_only', 'company_only', 'unreadable_lease',
    'wrong_company', 'stale_status', 'held', 'stale_seed', 'duplicate',
])
async def test_production_dispatch_refuses_unproven_worker_before_session(
        monkeypatch, tmp_path, state):
    """Lease/status/hold/duplicate/seed failures never reach BrowserSkill IPC."""
    import fakeredis
    from core.apply_queue import ApplyQueue
    from core.models import Status
    from core.storage.local import RedisTracking
    from tools import submit_hold

    pk, company, url = 'example-co#job-1', 'example-co', 'https://example.test/jobs/1'
    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    tracking.set_status(pk, Status.SUBMITTING,
                        company='other-example-co' if state == 'wrong_company' else company,
                        jd_url=url, resume_tex_key='resumes/synthetic.tex',
                        resume_seed='old-seed' if state == 'stale_seed' else 'current-seed')
    monkeypatch.setattr('core.stores.make_stores',
                        lambda *a, **kw: SimpleNamespace(tracking=tracking))
    monkeypatch.setattr('agent.run.seed_fingerprint', lambda: 'current-seed')
    monkeypatch.setattr('tools.browser_apply._duplicate_refusal',
                        lambda pk: {'reason': 'duplicate'} if state == 'duplicate' else None)
    monkeypatch.setattr('tools.claude_chrome._stage_resume',
                        lambda *a: pytest.fail('resume staged before dispatch proof'))
    monkeypatch.setattr(bsk, 'Session',
                        lambda *a: pytest.fail('BrowserSkill Session/IPC before dispatch proof'))
    pdf = tmp_path / 'Test-User.pdf'
    pdf.write_bytes(b'%PDF-1.4 synthetic offline document')
    queue = ApplyQueue(tracking.r)
    item = None
    if state not in {'no_lease', 'pk_only', 'company_only'}:
        assert queue.put(pk, company)
        item = queue.next(only=company)
        assert item
    elif state == 'pk_only':
        tracking.r.sadd('applyq:inflight:pks', pk)
    elif state == 'company_only':
        tracking.r.sadd('applyq:inflight', company)
    if state == 'stale_status':
        tracking.set_status(pk, Status.TAILORED)
    if state == 'held':
        submit_hold.mark(pk, tracking=tracking)

    original_smembers = tracking.r.smembers
    before = tracking.get(pk)
    try:
        if state == 'unreadable_lease':
            monkeypatch.setattr(tracking.r, 'smembers',
                                lambda *a: (_ for _ in ()).throw(
                                    OSError('Synthetic lease read unavailable')))
        result = await forms.apply(url, company, {'Full name': 'Test User'},
                                   'offline-model', pk=pk, resume_path=str(pdf))
        assert result['status'] == 'gate'
        assert tracking.get(pk) == before
    finally:
        monkeypatch.setattr(tracking.r, 'smembers', original_smembers)
        if item is not None:
            queue.done(item)



async def test_two_live_company_leases_cannot_splice_a_drifted_row_before_session(
        monkeypatch, tmp_path):
    """Independent busy-pk and busy-company sets are not a paired worker lease."""
    import fakeredis
    from core.apply_queue import ApplyQueue
    from core.models import Status
    from core.storage.local import RedisTracking

    a, b = 'example-co-a', 'example-co-b'
    pk_a, pk_b = f'{a}#job-1', f'{b}#job-2'
    url = 'https://example.test/jobs/job-1'
    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    for company, pk in ((a, pk_a), (b, pk_b)):
        _seed_clean(tracking, company, pk.split('#')[1], url)
        tracking.set_status(pk, Status.SUBMITTING,
                            resume_tex_key='resumes/Test-User.tex',
                            resume_seed='synthetic-seed')
    monkeypatch.setattr('core.stores.make_stores',
                        lambda *args, **kw: SimpleNamespace(tracking=tracking))
    monkeypatch.setattr('agent.run.seed_fingerprint',
                        lambda: 'synthetic-seed')
    monkeypatch.setattr('tools.browser_apply._duplicate_refusal', lambda pk: None)
    pdf = tmp_path / 'Test-User.pdf'
    pdf.write_bytes(b'%PDF-1.4 synthetic offline document')
    queue = ApplyQueue(tracking.r)
    assert queue.put(pk_a, a)
    assert queue.put(pk_b, b)
    item_a, item_b = queue.next(only=a), queue.next(only=b)
    assert item_a and item_b
    try:
        # Each independent set membership is true, even with B incorrectly
        # written onto A's tracked row. Neither worker granted this cross-pair.
        tracking.set_status(pk_a, Status.SUBMITTING, company=b)
        assert queue.in_flight() == {pk_a, pk_b}
        assert queue.depth()['running'] == [a, b]
        before = tracking.get(pk_a)
        with pytest.raises(forms.Gate, match='leased queue dispatch'):
            forms.check_dispatch(pk_a, str(pdf))

        ipc = []
        def session_attempt(*args, **kw):
            ipc.append('Session')
            pytest.fail('drifted job opened BrowserSkill Session')
        monkeypatch.setattr(bsk, 'Session', session_attempt)
        monkeypatch.setattr(bsk, 'decision', AsyncMock(
            side_effect=AssertionError('drifted job requested browser/model IPC')))
        monkeypatch.setattr('tools.claude_chrome._stage_resume',
                            lambda *args: pytest.fail('drifted job staged a resume'))
        result = await forms.apply(url, a, {'Full name': 'Test User'},
                                   'offline-model', pk=pk_a, resume_path=str(pdf))
        assert result['status'] == 'gate'
        assert ipc == []  # no Session, navigation, value, click, or submit
        assert tracking.get(pk_a) == before
        assert queue.in_flight() == {pk_a, pk_b}
        assert queue.depth()['running'] == [a, b]

        # Undo only the synthetic drift. Both genuine queue-owned attempts
        # retain the original production admission and can proceed.
        tracking.set_status(pk_a, Status.SUBMITTING, company=a)
        forms.check_dispatch(pk_a, str(pdf))
        forms.check_dispatch(pk_b, str(pdf))
    finally:
        queue.done(item_a)
        queue.done(item_b)
    assert not queue.in_flight()

@pytest.mark.parametrize(('verb', 'button'),
                         [('submit', 'Submit application'), ('click', 'Continue')])
async def test_submit_ipc_failure_is_terminal_uncertain_not_infrastructure_retry(
        monkeypatch, tmp_path, verb, button):
    # AC1/AC8: exercise the raw producer and real own-hold readback, not a
    # facade whose incomplete dispatch prevents reaching the failing IPC.
    from tests.tools.test_browser_integration_regressions import _world, URL, PK

    row, current, tracking, raw, effects, pdf = _world(monkeypatch, tmp_path)
    next(c for c in current['controls'] if c['selector'] == '#submit')['label'] = button
    evidence = []
    real_set, real_get = tracking.r.set, tracking.r.get
    real_update = tracking.update_if_status
    def held_set(*args, **kwargs):
        evidence.append('hold_set')
        return real_set(*args, **kwargs)
    def held_get(*args, **kwargs):
        evidence.append('hold_read')
        return real_get(*args, **kwargs)
    def update_if_status(*args, **kwargs):
        # The real durable hold uses a conditional WATCH-backed transition,
        # not the old unconditional set_status API.
        updated = real_update(*args, **kwargs)
        if updated:
            evidence.append('write')
        return updated
    monkeypatch.setattr(tracking.r, 'set', held_set)
    monkeypatch.setattr(tracking.r, 'get', held_get)
    monkeypatch.setattr(tracking, 'update_if_status', update_if_status)
    command = bsk.command
    async def fail_submit(*args, **kwargs):
        if args[0] == 'click':
            evidence.append('click')
            assert forms.submit_hold.is_held(PK, tracking=tracking)
            assert row['status'] == 'needs_human'
            assert row['gate_reason'] == 'submit_uncertain'
            assert row['fail_kind'] == 'uncertain'
            assert row['last_button'] == button
            assert row['last_url'] == current['url']
            assert 'possible_submission' not in row
            raise bsk.Unavailable('BrowserSkill unavailable: offline')
        return await command(*args, **kwargs)
    monkeypatch.setattr(bsk, 'command', fail_submit)
    # The application loop stages/uploads the unique PDF receiver itself.
    # A second model upload is correctly rejected; it is not the Submit path
    # whose one-shot uncertain outcome this regression exercises.
    monkeypatch.setattr(bsk, 'decision', AsyncMock(side_effect=[
        {'action': 'fill', 'selector': '#email', 'fact': 'Email'},
        {'action': verb, 'selector': '#submit'},
    ]))
    result = await forms.apply(URL, 'example-co', {'Email': 'test@example.test'},
                               'synthetic', pk=PK, resume_path=pdf)
    assert result['status'] == 'uncertain', result
    assert not runtime.is_disconnected(result['detail'])
    assert not runtime.is_infrastructure(result['detail'])
    assert result['step'] == {'last_button': button, 'url': current['url']}
    assert button in result['detail'] and current['url'] in result['detail']
    assert evidence.count('click') == 1
    hold_set = evidence.index('hold_set')
    write = evidence.index('write')
    # The durable hold precedes its WATCH-conditional status transition; the
    # click can happen only after both and a fresh hold readback.
    assert hold_set < write < evidence.index('click')
    # Preparation checks for prior holds too; require a fresh read after this set.
    assert hold_set < evidence.index('hold_read', hold_set + 1) < evidence.index('click')
    assert forms.submit_hold.is_held(PK, tracking=tracking)


async def test_discovery_admits_only_links_seen_in_browser(monkeypatch):
    link = target(selector='#job', tag='a', type='a', href='https://employer.test/job/1')
    current = page(link)
    session = SimpleNamespace(call=AsyncMock(), page=AsyncMock(return_value=current))
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', AsyncMock(return_value={'action': 'finish', 'report': {
        'jobs': [{'url': link['href'], 'title': 'Real'},
                 {'url': 'https://invented.test/job/2', 'title': 'Invented'}]}}))
    result = await bsk._crawl(session, 'Find postings', 'https://employer.test/careers')
    assert result['jobs'] == [{'url': link['href'], 'title': 'Real'}]


@pytest.mark.parametrize('unsafe', [
    'https://foreign.test/job/2',
    'https://employer.test/jobs/1/quick-apply?send=1',
    'https://employer.test/jobs/1/confirm',
    'https://employer.test/jobs/1/one-click',
    'https://employer.test/jobs/1/instantApply',
    'https://employer.test/jobs/1?source=submitApplication',
    'https://employer.test/jobs/1?save=1',
    'https://employer.test/jobs/1?submit=true',
    'https://employer.test/jobs/%61pply',
    'https://employer.test/jobs?source=%61pply',
    'https://employer.test/jobs/%2561pply',
    'https://employer.test/jobs#%61pply',
    'https://employer.test/account/delete',
    'https://employer.test/account/remove',
    'https://employer.test/account/cancel',
    'https://employer.test/account/unsubscribe',
    'https://employer.test/account/logout',
    'https://employer.test/account/signout',
    'https://employer.test/jobs/%ZZ',
    'https://employer.test/jobs/%25',
])
async def test_discovery_never_navigates_foreign_or_action_links(monkeypatch, unsafe):
    seed = 'https://employer.test/careers'
    current = {**page(target(tag='a', type='a', href=unsafe)), 'url': seed}
    session = SimpleNamespace(call=AsyncMock(), page=AsyncMock(return_value=current))
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', AsyncMock(side_effect=[
        {'action': 'navigate', 'url': unsafe},
        {'action': 'finish', 'report': {'jobs': []}}]))
    assert (await bsk._crawl(session, 'Find postings', seed))['jobs'] == []
    session.call.assert_awaited_once_with('navigate', seed)  # refused href had no IPC


@pytest.mark.parametrize('host', [
    'job-boards.greenhouse.io', 'job-boards.eu.greenhouse.io',
    'boards.greenhouse.io', 'jobs.lever.co', 'jobs.eu.lever.co',
    'jobs.ashbyhq.com', 'jobs.smartrecruiters.com',
    'careers.smartrecruiters.com', 'apply.workable.com', 'jobs.jobvite.com',
])
def test_discovery_enforces_shared_ats_tenant(host):
    seed = f'https://{host}/example-co/jobs'
    assert bsk.discovery_url_allowed(f'https://{host}/example-co/jobs/42', seed)
    assert not bsk.discovery_url_allowed(
        f'https://{host}/another-tenant/jobs/42', seed)
    assert not bsk.discovery_url_allowed(f'https://{host}/', f'https://{host}/')


@pytest.mark.parametrize('unsafe', [
    'https://employer.test/jobs/%61pply',
    'https://employer.test/jobs?source=%2561pply',
    'https://employer.test/account/delete',
])
async def test_discovery_never_publishes_decoded_action_routes(monkeypatch, unsafe):
    seed = 'https://employer.test/careers'
    current = {**page(target(tag='a', type='a', href=unsafe)), 'url': seed}
    session = SimpleNamespace(call=AsyncMock(), page=AsyncMock(return_value=current))
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', AsyncMock(return_value={
        'action': 'finish', 'report': {'jobs': [{'url': unsafe, 'title': 'Unsafe'}]}}))
    report = await bsk._crawl(session, 'Find postings', seed)
    assert report['jobs'] == []
    session.call.assert_awaited_once_with('navigate', seed)  # zero unsafe navigate IPC


@pytest.mark.parametrize('host', [
    'apply.workable.com', 'jobs.jobvite.com', 'jobs.eu.lever.co',
    'job-boards.eu.greenhouse.io', 'careers.smartrecruiters.com',
])
async def test_discovery_foreign_shared_tenant_has_no_ipc_or_published_job(monkeypatch, host):
    seed = f'https://{host}/example-co/jobs'
    foreign = f'https://{host}/other-tenant/jobs/999'
    current = {**page(target(tag='a', type='a', href=foreign)), 'url': seed}
    session = SimpleNamespace(call=AsyncMock(), page=AsyncMock(return_value=current))
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', AsyncMock(return_value={
        'action': 'finish', 'report': {'jobs': [{'url': foreign, 'title': 'Foreign'}]}}))
    report = await bsk._crawl(session, 'Find postings', seed)
    assert report['jobs'] == []
    session.call.assert_awaited_once_with('navigate', seed)


@pytest.mark.parametrize('unsafe', [
    'https://employer.test/jobs/%61pply',
    'https://employer.test/jobs?source=%61pply',
    'https://employer.test/account/delete',
])
async def test_discovery_action_seed_gates_before_any_browser_ipc(monkeypatch, unsafe):
    monkeypatch.setattr(bsk, 'Session', lambda kind: pytest.fail(
        'The unsafe discovery seed must not reach the browser session'))
    result, error = await bsk.run_task('Find roles', report_key='jobs', urls=[unsafe])
    assert result == {} and error


async def test_discovery_never_publishes_foreign_tenant(monkeypatch):
    seed = 'https://employer.test/careers'
    good = 'https://employer.test/jobs/123'
    foreign = 'https://job-boards.greenhouse.io/other-tenant/jobs/999'
    current = {**page(
        target(selector='#one', tag='a', type='a', href=good),
        target(selector='#two', tag='a', type='a', href=foreign)), 'url': seed}
    session = SimpleNamespace(call=AsyncMock(), page=AsyncMock(return_value=current))
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', AsyncMock(return_value={
        'action': 'finish', 'report': {'jobs': [
            {'url': good, 'title': 'Same employer'},
            {'url': foreign, 'title': 'Foreign tenant'}]}}))
    report = await bsk._crawl(session, 'Find postings', seed)
    assert report['jobs'] == [{'url': good, 'title': 'Same employer'}]
    session.call.assert_awaited_once_with('navigate', seed)


def test_startup_reuses_shared_daemon_and_never_restarts_it(monkeypatch):
    monkeypatch.setattr(bsk, '_sync', lambda *args: {'ok': True})
    monkeypatch.setattr(bsk.subprocess, 'Popen', lambda *args, **kw: pytest.fail('Shared daemon'))
    assert bsk.ensure_daemon() == (True, '')


async def test_typed_posting_url_is_not_reparsed_with_prompt_punctuation(monkeypatch):
    url = 'https://employer.test/job/R123?value=a,b'
    seen = []
    class Reader:
        def __init__(self, kind):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def navigate(self, value):
            seen.append(value)
            return page()
    monkeypatch.setattr(bsk, 'Session', Reader)
    report, problem = await bsk.run_task(f'Open {url}, wait for it to load.',
                                         report_key='description', urls=[url])
    assert not problem and report['description'] == GOOD.strip()
    assert seen == [url]


async def test_untyped_role_button_cannot_bypass_self_id_guards():
    session = SimpleNamespace(call=AsyncMock())
    with pytest.raises(forms.Gate):
        await forms.execute(
            session, page(target(type='button', tag='div', label='Male', in_form=False)),
            {'action': 'click', 'selector': '#field'}, facts={}, filled={},
            resume_path='', company='Company', jd_text='', resume_tex='', github='')
    session.call.assert_not_awaited()


async def test_required_acknowledgement_does_not_authorize_a_citizenship_fact(monkeypatch):
    session = SimpleNamespace(call=AsyncMock())
    # An offline tracked row is enough to exercise the actual protected-field
    # refusal. No live Redis lookup is permitted by this isolated regression.
    monkeypatch.setattr(forms, '_row', lambda pk: {
        'pk': pk, 'status': 'submitting', 'human_approved_answers': {}})
    control = target(type='checkbox', label='I certify I am a US citizen', required=True)
    # A new legal/citizenship attestation routes directly to the owner even
    # without an application marker; it cannot mint an unapproved fact answer.
    with pytest.raises(forms.Gate) as refusal:
        await forms.execute(session, page(control),
                            {'action': 'choose', 'selector': '#field'}, facts={}, filled={},
                            resume_path='', company='Company', jd_text='',
                            resume_tex='', github='')
    assert refusal.value.reason == 'owner_question'
    session.call.assert_not_awaited()



def test_removed_attachment_still_blocks_and_ordinary_preselected_radio_is_not_a_grant():
    with pytest.raises(forms.Gate, match='Exactly one current résumé input'):
        forms.check_form(page(target()), {}, True, 'Resume.pdf')
    radio = target(type='radio', label='Yes', question='Authorized to work?',
                   checked=True, name='work_auth', value='yes',
                   has_value=True, required=True)
    attachment = target(selector='#resume', type='file', label='Resume',
                        files=['Resume.pdf'])
    # Non-protected, current native choices no longer require a prior widget grant.
    forms.check_form(page(radio, attachment), {}, True, 'Resume.pdf')


def test_native_select_can_derive_the_option_without_a_model_guessed_value():
    control = target(tag='select', label='Gender',
                     options=[{'value': '1', 'label': 'Male'},
                              {'value': '2', 'label': 'Prefer not to say'}])
    assert forms.choose_value(control, 'Prefer not to say', '') == '2'


async def test_workday_shell_is_not_a_job_description():
    url = 'https://tenant.wd1.myworkdayjobs.com/careers/job/Remote/Lead_R1'
    shell = {**page(text='Cookies and navigation. ' * 30), 'description': ''}
    session = SimpleNamespace(navigate=AsyncMock(return_value=shell))
    with pytest.raises(bsk.Unavailable, match='container is empty'):
        await bsk._read(session, url)


async def test_invalid_legacy_url_does_not_open_an_agent_window(monkeypatch):
    monkeypatch.setattr(bsk, 'Session', lambda *a: pytest.fail('Invalid URLs stay outside Chrome'))
    report, problem = await bsk.run_task('Open /jobs/1', report_key='description', urls=['/jobs/1'])
    assert not report and 'HTTP(S)' in problem


async def test_manual_confirmation_during_fill_keeps_the_applied_ledger(monkeypatch):
    import fakeredis

    from agent import run
    from core.models import Status
    from core.storage.local import RedisTracking

    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    pk = 'acme#1'
    _seed_clean(tracking, 'Acme', '1', 'https://employer.test/job/1')
    tracking.set_status(pk, Status.TAILORED, gate_reason='approval')
    stores = SimpleNamespace(tracking=tracking, secrets=None,
                             answer_bank=SimpleNamespace(all_facts=lambda company: {}))
    from tools import posting_guard as pg
    monkeypatch.setattr(run, '_jd_meta', AsyncMock(return_value=pg.analyze(GOOD)))
    monkeypatch.setattr(run, '_github_context', lambda: '')
    monkeypatch.setattr(run, '_resume_pdf_path', lambda row: '')
    monkeypatch.setattr('tools.credentials.get_login', lambda *args: None)
    monkeypatch.setattr('core.rotation.ensure', lambda *args: None)
    monkeypatch.setattr('core.profiles.resolve_for', lambda row: None)

    async def manual_confirmation(*args, **kwargs):
        # The queue dispatcher consumed the approval marker before handing off.
        assert tracking.get(pk)['gate_reason'] == ''
        tracking.set_status(pk, Status.APPLIED_MANUAL, confirmation_id='owner-confirmed')
        return {'status': 'gate', 'question': 'Already recorded during this browser session.'}
    monkeypatch.setattr('tools.browser_apply.apply', manual_confirmation)
    result = await run._apply_direct(pk, stores)
    assert result['reason'] == 'already_applied'
    assert tracking.get(pk)['status'] == 'applied_manual'
    assert tracking.get(pk)['confirmation_id'] == 'owner-confirmed'


@pytest.mark.parametrize('skip', [False, True])
def test_background_prepare_failure_does_not_leave_an_unclaimed_working_card(monkeypatch, skip):
    import fakeredis
    from fastapi import BackgroundTasks

    import server
    from agent import run
    from core.models import Status
    from core.storage.local import RedisTracking

    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    tracking.set_status('acme#1', Status.FOUND, company='Acme')
    stores = SimpleNamespace(tracking=tracking)
    monkeypatch.setattr(server, 'make_stores', lambda *args: stores)
    async def bad_score(pk, admitted, stores, *, prepare_only):
        assert prepare_only
        # Admission and its generation belong to run_job, not the server's
        # pre-run snapshot. Exercise that owner instead of bypassing its catch.
        assert admitted['status'] == 'tailoring'
        if skip:
            tracking.set_status(pk, Status.SKIPPED, skip_reason='user_skipped')
        raise ValueError('Malformed structured score')
    monkeypatch.setattr(run, '_run_job_async', bad_score)
    endpoints = {r.path: r.endpoint for r in server.create_app().routes
                 if hasattr(r, 'endpoint')}
    background = BackgroundTasks()
    endpoints['/actions/run-job/{pk}']('acme#1', background)
    background.tasks[0].func()
    row = tracking.get('acme#1')
    assert row['status'] == ('skipped' if skip else 'error')
    if skip:
        assert row['skip_reason'] == 'user_skipped'
    else:
        assert row['error'] == 'Preparation failed — see Logs and retry.'
    assert not run._claimed('acme#1', stores)


@pytest.mark.parametrize('destination', [
    'https://employer.test/job/1',
    'https://employer.test/job/1/apply',
    'https://employer.test/job/1/confirmation',
])
def test_navigation_stays_within_explicit_job_route(destination):
    assert forms.navigation_allowed(destination, 'https://employer.test/job/1')


@pytest.mark.parametrize('destination', [
    'https://employer.test/job/2', 'https://employer.test/next',
    'https://job-boards.greenhouse.io/other/jobs/1',
    'https://jobs.lever.co/other/job/2',
    'https://tenant.wd1.myworkdayjobs.com/other/job/2',
    'https://jobs.smartrecruiters.com/other/1',
])
def test_host_and_ats_suffix_never_authorize_an_unrelated_job(destination):
    assert not forms.navigation_allowed(destination, 'https://employer.test/job/1')


@pytest.mark.parametrize('destination', [
    'https://external.test/next', 'https://greenhouse.io.external.test/job/1',
    'https://notmyworkdayjobs.com/job/1', 'https://ashbyhq.com@external.test/job/1',
    'javascript:alert(1)',
 ])
def test_lookalike_and_unrelated_hosts_are_not_navigation_permission(destination):
    assert not forms.navigation_allowed(destination, 'https://employer.test/job/1')


def test_direct_board_host_requires_a_job_route_not_domain_wide_access():
    direct = 'https://eeho.fa.us2.oraclecloud.com/tenant/jobs/role-1'
    assert forms.navigation_allowed(direct + '/apply',
                                    'https://careers.oracle.com/job/1', direct)
    assert not forms.navigation_allowed(
        'https://eeho.fa.us2.oraclecloud.com/tenant/jobs/role-2',
        'https://careers.oracle.com/job/1', direct)
    assert not forms.navigation_allowed(
        'https://another.oraclecloud.com/tenant/jobs/role-1',
        'https://careers.oracle.com/job/1', direct)


@pytest.mark.parametrize('verb', ['navigate', 'click'])
async def test_application_gates_external_link_before_the_browser_follows_it(monkeypatch, verb):
    outside = 'https://external.test/next'
    current = page(target(selector='#outside', tag='a', type='a', in_form=False,
                          label='View details', href=outside))
    calls = []
    class FakeSession:
        def __init__(self, kind):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def navigate(self, url):
            calls.append(url)
            return current
        async def page(self):
            return current
        async def call(self, *args, **kwargs):
            pytest.fail('A forbidden link must never reach browser click')
    monkeypatch.setattr(bsk, 'Session', FakeSession)
    monkeypatch.setattr(forms, 'check_dispatch', lambda *args: None)
    # A coherent tracked row is required to reach the origin guard under test.
    monkeypatch.setattr(forms, '_row', lambda pk: dict(
        pk=pk, company='Company', job_id='1', jd_url=current['url'], status='submitting'))
    monkeypatch.setattr('tools.claude_chrome._stage_resume', lambda *args: 'Resume.pdf')
    monkeypatch.setattr(bsk, 'decision', AsyncMock(return_value={
        'action': verb, 'selector': '#outside' if verb == 'click' else '', 'url': outside}))
    result = await forms.apply(current['url'], 'Company', {}, 'chatgpt/model',
                               pk='company#1', resume_path='Resume.pdf')
    assert result['status'] == 'gate'
    assert 'tracked job and tenant' in result['question']
    assert result['status'] == 'gate'
    assert result['step']['url'] == current['url']
    assert calls == [current['url']]


def test_handoff_preserves_step_but_never_reproduces_oauth_tickets():
    result = forms.handoff('Check the portal.', {
        'last_button': 'Continue',
        'url': 'https://employer.test/review?job=123&access_token=SECRET#id_token=SECRET'})
    assert result['step'] == {'last_button': 'Continue',
                              'url': 'https://employer.test/review?job=123'}
    assert 'SECRET' not in result['text']


async def test_owner_fact_textarea_is_not_replaced_with_a_generated_essay(monkeypatch):
    # A fact explicitly selected for the textarea is an ordinary preparation
    # answer. No retired per-field grant is required or inferred from the bank.
    session = SimpleNamespace(call=AsyncMock())
    monkeypatch.setattr('tools.narrative.draft_answer',
                        lambda *a, **kw: pytest.fail('The supplied owner fact must prevail'))
    await forms.execute(
        session, page(target(tag='textarea', type='textarea', label='Motivation'),
                      application_marker=True),
        {'action': 'fill', 'selector': '#field', 'fact': 'Motivation', 'essay': True},
        facts={'Motivation': 'Approved explanation'}, filled={}, resume_path='',
        company='Company', jd_text='', resume_tex='', github='', pk='Company#1')
    session.call.assert_awaited_once_with('fill', '#field', '--value', 'Approved explanation')



async def test_file_url_permission_error_propagates_once_for_terminal_upload_handoff(tmp_path):
    raw = json.dumps({'code': 'cdp_failed', 'message': 'PRIVATE CONTENT', 'exit_code': 3,
                      'hint': "check Chrome's Allow access to file URLs permission",
                      'data': {'effect_state': 'unknown'}})
    with pytest.raises(bsk.Unavailable) as caught:
        bsk._result(raw, 3)
    error = caught.value
    assert error.file_access_required and 'PRIVATE CONTENT' not in str(error)
    document = tmp_path / 'Synthetic-Resume.pdf'
    document.write_bytes(b'%PDF-1.4\nsynthetic-only\n%%EOF')
    session = SimpleNamespace(call=AsyncMock(side_effect=error))
    with pytest.raises(bsk.Unavailable) as escaped:
        await forms.execute(
            session, page(target(type='file', label='Resume')),
            {'action': 'upload', 'selector': '#field'}, facts={}, filled={},
            resume_path=str(document), company='example-co', jd_text='',
            resume_tex='', github='')
    assert escaped.value is error
    session.call.assert_awaited_once()



async def test_unknown_upload_effect_is_not_retried_inside_preparation(tmp_path):
    error = bsk.Unavailable('BrowserSkill operation unavailable: transport')
    error.effect_state = 'unknown'
    document = tmp_path / 'Synthetic-Resume.pdf'
    document.write_bytes(b'%PDF-1.4\nsynthetic-only\n%%EOF')
    session = SimpleNamespace(call=AsyncMock(side_effect=error))
    with pytest.raises(bsk.Unavailable) as escaped:
        await forms.execute(
            session, page(target(type='file', label='Resume')),
            {'action': 'upload', 'selector': '#field'}, facts={}, filled={},
            resume_path=str(document), company='example-co', jd_text='',
            resume_tex='', github='')
    assert escaped.value is error
    session.call.assert_awaited_once()


def test_verified_file_input_is_not_mistaken_for_unapproved_text():
    resume = target(type='file', label='Resume', required=True, has_value=True,
                    value='C:\\fakepath\\Resume.pdf', files=['Resume.pdf'])
    forms.check_form(page(resume), {}, True, 'Resume.pdf')
    resume['files'] = []
    with pytest.raises(forms.Gate, match='Exactly one current résumé input'):
        forms.check_form(page(resume), {}, True, 'Resume.pdf')


@pytest.mark.parametrize(('field', 'action', 'facts'), [
    (target(label='Years of experience'), {'action': 'fill', 'selector': '#field',
                                            'fact': 'Years'}, {'Years': '10 years'}),
    (target(tag='textarea', type='textarea', label='Why this role?'),
     {'action': 'fill', 'selector': '#field', 'essay': True}, {}),
    (target(tag='textarea', type='textarea', label='Why this role?'),
     {'action': 'fill', 'selector': '#field', 'fact': 'Motivation'},
     {'Motivation': 'Invented but plausible narrative'}),
    (target(type='checkbox', label='I consent to share my personal data', required=True),
     {'action': 'choose', 'selector': '#field'}, {}),
    (target(type='checkbox', label='I accept marketing messages', required=True),
     {'action': 'choose', 'selector': '#field'}, {}),
])
async def test_unapproved_fact_essay_and_consequential_consent_have_zero_writes(
        monkeypatch, field, action, facts):
    # A supplied fact is ordinary preparation, not a per-field approval.
    # Missing facts still use the existing owner-question path.
    monkeypatch.setattr(forms, '_row', lambda pk: {
        'pk': pk, 'status': 'submitting', 'human_approved_answers': {}})
    session = SimpleNamespace(call=AsyncMock())
    if action.get('fact') in facts:
        await forms.execute(
            session, page(field, application_marker=True), action, facts=facts,
            filled={}, resume_path='', company='example-co', jd_text='',
            resume_tex='', github='', pk='example-co#1')
        session.call.assert_awaited_once_with(
            'fill', '#field', '--value', facts[action['fact']])
    else:
        with pytest.raises(forms.Gate) as refusal:
            await forms.execute(
                session, page(field, application_marker=True), action, facts=facts,
                filled={}, resume_path='', company='example-co', jd_text='',
                resume_tex='', github='', pk='example-co#1')
        assert refusal.value.reason == 'owner_question'
        assert refusal.value.form_question['control_label'] == field['label']
        session.call.assert_not_awaited()

def test_opaque_and_hidden_form_values_block_final_click():
    attachment = target(selector='#resume', type='file', label='Resume',
                        files=['Resume.pdf'])
    with pytest.raises(forms.Gate, match='incomplete or unreadable'):
        forms.check_form({**page(attachment), 'opaque_controls': True}, {}, True, 'Resume.pdf')
    with pytest.raises(forms.Gate, match='incomplete'):
        forms.check_form({**page(attachment), 'inventory_verified': False},
                         {}, True, 'Resume.pdf')
    # Hidden machine inputs are not protected-characteristic answers by mere
    # existence; do not restore the removed blanket per-field witness Gate.
    forms.check_form(page(attachment, target(selector='#private', type='hidden',
                                             value='synthetic', has_value=True)),
                     {}, True, 'Resume.pdf')


def test_stale_page_text_and_fakepath_never_prove_uploaded_pdf():
    attachment = target(selector='#resume', type='file', label='Resume',
                        value='C:\\fakepath\\Resume.pdf', has_value=True, files=[])
    with pytest.raises(forms.Gate, match='Exactly one current résumé input'):
        forms.check_form(page(attachment, text='Resume.pdf upload completed'),
                         {}, True, 'Resume.pdf')


@pytest.mark.parametrize('error', [
    TimeoutError('synthetic upload timeout'),
    bsk.Unavailable('BrowserSkill operation unavailable: synthetic transport'),
])

async def test_unknown_or_timeout_upload_propagates_without_retry(error, tmp_path):
    document = tmp_path / 'Synthetic-Resume.pdf'
    document.write_bytes(b'%PDF-1.4\nsynthetic-only\n%%EOF')
    session = SimpleNamespace(call=AsyncMock(side_effect=error))
    with pytest.raises(type(error)) as escaped:
        await forms.execute(
            session, page(target(type='file', label='Resume')),
            {'action': 'upload', 'selector': '#field'}, facts={}, filled={},
            resume_path=str(document), company='example-co', jd_text='',
            resume_tex='', github='')
    assert escaped.value is error
    session.call.assert_awaited_once()


async def test_unclassified_js_proceed_or_one_click_apply_never_reaches_ipc(monkeypatch):
    # A synthetic tracked row makes this denial independent of the owner's Redis.
    url = 'https://employer.test/job/1'
    monkeypatch.setattr(forms, '_row', lambda pk: {
        'pk': pk, 'company': 'example-co', 'job_id': '1',
        'jd_url': url, 'status': 'submitting'})
    for wording in ['View', 'Proceed', 'Apply now', 'Edit']:
        session = SimpleNamespace(call=AsyncMock())
        button = target(tag='button', type='button', label=wording)
        with pytest.raises(forms.Gate):
            await forms.execute(
                session, page(button), {'action': 'click', 'selector': '#field'},
                facts={}, filled={}, resume_path='', company='example-co',
                jd_text='', resume_tex='', github='', pk='example-co#1',
                job_url=url)
        session.call.assert_not_awaited()


def test_job_id_query_cannot_change_on_same_ats_host():
    origin = 'https://tenant.ats.test/company/apply?job_id=one'
    assert forms.navigation_allowed(
        'https://tenant.ats.test/company/apply?job_id=one', origin)
    assert not forms.navigation_allowed(
        'https://tenant.ats.test/company/apply?job_id=two', origin)


@pytest.mark.parametrize('outcome', ['submitted', 'not_submitted'])
def test_held_outcome_requires_explicit_portal_verification_and_never_autorequeues(
        monkeypatch, outcome):
    import fakeredis
    import server
    from core.models import Status
    from core.storage.local import RedisTracking
    from core.apply_queue import ApplyQueue

    redis = fakeredis.FakeRedis(decode_responses=True)
    tracking = RedisTracking(redis)
    pk = 'example-co#synthetic-role'
    tracking.set_status(pk, Status.NEEDS_HUMAN, company='example-co',
                        possible_submission=True, gate_reason='submit_uncertain',
                        fail_kind='uncertain', last_button='Continue',
                        last_url='https://example.test/job/1')
    forms.submit_hold.mark(pk, tracking=tracking)
    stores = SimpleNamespace(tracking=tracking)
    monkeypatch.setattr(server, 'make_stores', lambda *args: stores)
    monkeypatch.setattr('tools.browser_skill.applies_running', lambda: 0)
    endpoint = next(r.endpoint for r in server.create_app().routes
                    if getattr(r, 'path', '') == '/actions/resolve-uncertain/{pk:path}')

    assert not endpoint(pk, {'outcome': outcome})['ok']
    assert not endpoint(pk, {'portal_checked': True,
                             'outcome': outcome})['ok']
    assert forms.submit_hold.is_held(pk, tracking=tracking)
    assert not ApplyQueue(redis).pending()

    if outcome == 'submitted':
        result = endpoint(pk, {'portal_checked': True, 'outcome': outcome,
                               'confirmation': 'Synthetic portal confirmation'})
        assert result == {'ok': True, 'status': 'applied_manual'}
        assert tracking.get(pk)['possible_submission'] is False
        assert tracking.get(pk)['confirmation_id'] == 'Synthetic portal confirmation'
    else:
        result = endpoint(pk, {'portal_checked': True, 'outcome': outcome,
                               'new_apply_decision': True})
        assert result == {'ok': True, 'status': 'tailored', 'queued': False}
        assert tracking.get(pk)['possible_submission'] is False
        assert tracking.get(pk)['gate_reason'] == 'approval'
    assert not ApplyQueue(redis).pending()
    assert not forms.submit_hold.is_held(pk, tracking=tracking)


def test_destructive_reset_refuses_existing_hold_before_any_live_side_effect(monkeypatch):
    import fakeredis
    import server
    from core.models import Status
    from core.storage.local import RedisTracking

    client = fakeredis.FakeRedis(decode_responses=True)
    tracking = RedisTracking(client)
    tracking.set_status('example-co#hold', Status.NEEDS_HUMAN,
                        possible_submission=True, fail_kind='uncertain')
    monkeypatch.setattr(server, 'make_stores',
                        lambda *args: SimpleNamespace(tracking=tracking))
    monkeypatch.setattr('tools.browser_skill.applies_running', lambda: 0)
    monkeypatch.setattr('tools.browser_runtime.kill_live_sessions',
                        lambda: pytest.fail('Reset must refuse before browser effects'))
    endpoint = next(r.endpoint for r in server.create_app().routes
                    if getattr(r, 'path', '') == '/actions/reset')
    refusal = endpoint()
    assert not refusal['ok']
    assert tracking.get('example-co#hold')['possible_submission']


def test_independent_hold_survives_existing_tracking_full_row_writer(monkeypatch):
    """R05 replaces the erasable row marker without changing shared storage."""
    import fakeredis
    from core.models import Status
    from core.storage.local import RedisTracking

    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    pk = 'example-co#interleaved-stale-write'
    # A legitimate new hold starts from an actually persisted, independently
    # inspected posting. Missing posting evidence is not submission authority.
    _seed_clean(tracking, 'example-co', 'interleaved-stale-write',
                'https://example.test/job/1')
    tracking.set_status(pk, Status.SUBMITTING, company='example-co')
    stale_pre_hold_row = tracking.get(pk)
    monkeypatch.setattr('core.stores.make_stores', lambda: SimpleNamespace(tracking=tracking))
    forms.hold_possible_submission(pk, {'last_button': 'Submit',
                                       'url': 'https://example.test/job/1'})
    tracking._write(pk, stale_pre_hold_row, prev_status='submitting')
    assert tracking.get(pk)['status'] == 'submitting'
    assert forms.submit_hold.is_held(pk, tracking=tracking)
    assert forms.submit_hold.blocked(pk, tracking.get(pk), tracking=tracking)


@pytest.mark.parametrize('when', ['preexisting', 'after-hold-mark'])
def test_manual_outcome_is_never_replaced_by_own_possible_submit_hold(monkeypatch, when):
    """The existing Redis WATCH transition must preserve manual reconciliation."""
    import fakeredis
    from core.models import Status
    from core.storage.local import RedisTracking

    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    pk = 'example-co#manual-outcome'
    _seed_clean(tracking, 'example-co', 'manual-outcome',
                'https://example.test/job/1')
    tracking.set_status(pk, Status.SUBMITTING)
    monkeypatch.setattr('core.stores.make_stores',
                        lambda: SimpleNamespace(tracking=tracking))

    if when == 'preexisting':
        tracking.set_status(pk, Status.APPLIED_MANUAL,
                            confirmation_id='Synthetic manual confirmation')
    else:
        original_update = tracking.update_if_status

        def human_between_mark_and_status(*args, **kwargs):
            assert forms.submit_hold.is_held(pk, tracking=tracking)
            tracking.set_status(pk, Status.APPLIED_MANUAL,
                                confirmation_id='Synthetic manual confirmation')
            return original_update(*args, **kwargs)

        monkeypatch.setattr(tracking, 'update_if_status',
                            human_between_mark_and_status)

    with pytest.raises(forms.Gate, match='no click is safe'):
        forms.hold_possible_submission(
            pk, {'last_button': 'Submit',
                 'url': 'https://example.test/job/1'})

    current = tracking.get_current(pk)
    assert current['status'] == 'applied_manual'
    assert current['confirmation_id'] == 'Synthetic manual confirmation'
    if when == 'preexisting':
        assert not forms.submit_hold.is_held(pk, tracking=tracking)
    else:
        # A concurrent manual outcome must survive, but never erase the
        # independently recorded possible-submit key without reconciliation.
        assert forms.submit_hold.is_held(pk, tracking=tracking)


@pytest.mark.parametrize('kind', ['hidden', 'custom', 'contenteditable', 'combobox'])
def test_actual_dom_inventory_refuses_hidden_and_opaque_controls(kind):
    """Exercise PAGE itself, not an invented inventory with flags already set."""
    import subprocess
    from tools.browser_skill_dom import PAGE
    script = r'''
const kind = process.argv[1];
const form = {querySelector: () => null, innerText: ''};
const field = {id: 'field', tagName: kind === 'custom' ? 'X-FOO' : 'INPUT',
  type: kind === 'hidden' ? 'hidden' : 'text', value: 'unapproved',
  labels: [], form, name: '', required: true, disabled: false,
  getClientRects: () => kind === 'hidden' ? [] : [1],
  closest: q => q === 'form' ? form : null,
  hasAttribute: () => false,
  getAttribute: k => k === 'type' ? field.type :
    (k === 'role' && kind === 'combobox' ? 'combobox' : null),
  matches: q => (kind === 'contenteditable' && q.includes('[contenteditable]')) ||
    (kind === 'combobox' && q.includes('[role="combobox"]'))};
global.document = {readyState: 'complete', title: 'Synthetic form', body: {innerText: ''},
  querySelector: () => null, getElementById: () => null,
  querySelectorAll: q => q === '.iti' ? [] : q === '[role="combobox"]' ?
    (kind === 'combobox' ? [field] : []) : [field]};
global.CSS = {escape: s => s};
global.location = {href: 'https://example.test/job/1'};
global.getComputedStyle = () => ({visibility: 'visible'});
console.log(JSON.stringify(eval(process.argv[2])));
'''
    observed = json.loads(subprocess.check_output(['node', '-e', script, kind, PAGE], text=True))
    if kind == 'hidden':
        assert observed['controls'][0]['type'] == 'hidden'
        resume = target(selector='#resume', type='file', label='Resume', files=['Resume.pdf'])
        observed['controls'].append(resume)
        # A hidden machine input does not prove a protected-characteristic
        # response; final readback still requires the exact staged PDF.
        forms.check_form(observed, {}, True, 'Resume.pdf')
    else:
        assert observed['opaque_controls']
        assert not observed['inventory_verified']

@pytest.mark.parametrize('scenario', [
    'input', 'div', 'provisional', 'orphan-listbox', 'orphan-option',
    'shared', 'other-form', 'nested', 'wrong-role', 'duplicate-id',
    'contenteditable', 'canvas', 'custom', 'submit-option', 'navigate-option',
])
def test_production_page_and_witness_execute_linkage_and_opacity_js(scenario):
    """Run both production JS readers against the same synthetic DOM graph."""
    import subprocess
    from tools.browser_skill_dom import PAGE
    script = r'''
const scenario = process.argv[1];
const all = [];
const url = 'http://127.0.0.1/fixture/example-co/job/1';
function make(tag, id, attrs={}, parent=null, extra={}) {
  const n = {id, tagName:tag.toUpperCase(), nodeType:1, parentElement:parent,
    children:[], form:null, type:extra.type || (tag === 'input' ? 'text' : ''),
    value:extra.value || '', name:extra.name || '', required:true, disabled:false,
    readOnly:false, inert:false, labels:[], innerText:extra.text || '',
    href:extra.href || '', style:{}, options:undefined,
    getClientRects:()=>[{width:120,height:24,right:120,bottom:24}], getAttribute:k=>Object.hasOwn(attrs,k) ? attrs[k] : null,
    hasAttribute:k=>Object.hasOwn(attrs,k),
    matches:q=>q.split(',').some(s=>s.trim()===':disabled' ? !!n.disabled :
      s.trim()==='[inert]' ? !!n.inert :
      s.trim()==='[contenteditable]' ? n.hasAttribute('contenteditable') :
      s.trim()==='[contenteditable="true"]' ? n.getAttribute('contenteditable')==='true' :
      s.trim()==='canvas' ? n.tagName==='CANVAS' :
      s.trim()==='object' ? n.tagName==='OBJECT' :
      s.trim()==='embed' ? n.tagName==='EMBED' :
      s.trim()==='[aria-controls]' ? !!n.getAttribute('aria-controls') :
      s.trim()==='[role="combobox"]' ? n.getAttribute('role')==='combobox' :
      s.trim()==='[role="listbox"]' ? n.getAttribute('role')==='listbox' :
      s.trim()==='[role="option"]' ? n.getAttribute('role')==='option' : false),
    closest:q=>{ for(let p=n;p;p=p.parentElement) {
      if(q==='form' && p.tagName==='FORM') return p;
      if(q==='[role="listbox"]' && p.getAttribute('role')==='listbox') return p;
      if(q==='[inert]' && p.inert) return p;
    } return null; },
    contains:x=>{for(let p=x;p;p=p.parentElement) if(p===n) return true; return false;},
    querySelectorAll:q=>descendants(n).filter(x=>matches(x,q)),
    querySelector:()=>null};
  if(parent) parent.children.push(n); all.push(n); return n;
}
function descendants(n){return n.children.flatMap(c=>[c,...descendants(c)]);}
function matches(n,q){return q.split(',').some(s=>{s=s.trim();
  if(s.startsWith('#')) return n.id===s.slice(1);
  if(s==='input[type="hidden"]') return n.tagName==='INPUT' && n.type==='hidden';
  if(s==='*') return true;
  if(s==='input') return n.tagName==='INPUT';
  if(s==='textarea') return n.tagName==='TEXTAREA';
  if(s==='select') return n.tagName==='SELECT';
  if(s==='button') return n.tagName==='BUTTON';
  if(s==='a[href]') return n.tagName==='A' && !!n.href;
  if(s==='[role="button"]') return n.getAttribute('role')==='button';
  if(s==='[role="combobox"]') return n.getAttribute('role')==='combobox';
  if(s==='[role="option"]') return n.getAttribute('role')==='option';
  if(s==='[role="radio"]') return n.getAttribute('role')==='radio';
  if(s==='[role="checkbox"]') return n.getAttribute('role')==='checkbox';
  if(s==='[contenteditable="true"]') return n.getAttribute('contenteditable')==='true';
  return false;});}
const form=make('form','application',{},null); form.action=url;
const other=make('form','other-form',{},null); other.action=url;
const linked=scenario!=='orphan-listbox' && scenario!=='orphan-option';
let combo=null, list=null;
if(linked){
  const tag=scenario==='div'?'div':'input';
  combo=make(tag,'country',{'role':'combobox','aria-controls':scenario==='provisional'?'missing':'choices',
    'aria-valuetext':'Example Country'},form,{value:'Example Country'}); combo.form=form;
  if(scenario==='shared') make('input','country-2',{'role':'combobox','aria-controls':'choices'},form);
}
if(!['provisional','orphan-option','orphan-listbox'].includes(scenario)){
  const listParent=scenario==='other-form'?other:form;
  list=make('div','choices',{'role':scenario==='wrong-role'?'grid':'listbox'},listParent);
  const optionParent=scenario==='nested'?make('div','nested',{'role':'listbox'},list):list;
  const optionTag=scenario==='submit-option'?'button':scenario==='navigate-option'?'a':'div';
  const optionType=scenario==='submit-option'?'submit':'';
  const option=make(optionTag,'country-example',{'role':'option','data-value':'EX',
    'aria-selected':'true'},optionParent,{text:'Example Country',type:optionType,
    href:scenario==='navigate-option'?'https://example.test/away':''});
  option.value='EX'; if(scenario==='submit-option') option.form=form;
}
if(scenario==='orphan-option') make('div','orphan',{'role':'option'},form,{text:'Orphan'});
if(scenario==='orphan-listbox') make('div','orphan-list',{'role':'listbox'},form);
if(scenario==='duplicate-id') make('div','choices',{'role':'listbox'},form);
if(scenario==='contenteditable') make('div','editor',{'contenteditable':'true'},form);
if(scenario==='canvas') make('canvas','canvas',{},form);
if(scenario==='custom') make('x-widget','custom',{},form);
const combos=()=>all.filter(n=>n.getAttribute('role')==='combobox');
global.document={readyState:'complete',title:'Synthetic',body:{innerText:''},
  querySelector:()=>null,getElementById:id=>all.find(n=>n.id===id)||null,
  querySelectorAll:q=>q.startsWith('#')?all.filter(n=>n.id===q.slice(1)):
    q==='*'?all: q==='[role="combobox"]'?combos():all.filter(n=>matches(n,q))};
global.CSS={escape:s=>s}; global.location={href:url}; global.scrollX=0; global.scrollY=0;
global.getComputedStyle=n=>({visibility:'visible',display:'block',opacity:'1',position:'static'});
const page=eval(process.argv[2]); const witness=eval(process.argv[3]);
console.log(JSON.stringify({page,witness}));
'''
    from tools.browser_skill_apply import _FIXED_CONTROL_WITNESS
    result = json.loads(subprocess.check_output(
        ['node', '-e', script, scenario, PAGE, _FIXED_CONTROL_WITNESS], text=True))
    page, witness = result['page'], result['witness']
    if scenario in {'input', 'div', 'submit-option', 'navigate-option'}:
        # Regression: the ID #country alone must not disable ordinary ARIA
        # combobox linkage outside the Greenhouse phone-country fieldset.
        assert page['inventory_verified'] and not page['opaque_controls']
        assert page['opaque_count'] == 0 and page['opaque_locators'] == []
        assert all(source['controls'][0]['combobox']['link_id'] == 'choices'
                   for source in (page, witness))
        assert [c['selector'] for c in page['controls']] == ['#country']
        assert [c['selector'] for c in witness['controls']] == ['#country']
        assert page['controls'][0]['combobox'] == witness['controls'][0]['combobox'], (
            page['controls'][0]['combobox'], witness['controls'][0]['combobox'])
        option = witness['controls'][0]['combobox']['options'][0]
        assert option['submit'] is (scenario == 'submit-option')
        assert option['navigates'] is (scenario == 'navigate-option')
        assert witness['controls'][0]['combobox']['displayed_text'] == 'Example Country'
        assert witness['controls'][0]['combobox']['displayed_text'] == page['controls'][0]['combobox']['selected_text']
    elif scenario == 'provisional':
        # A uniquely owned but absent ARIA target remains provisional; the
        # Greenhouse receiverless C2 outside-fieldset negative has no linkId.
        assert page['inventory_verified'] and not page['opaque_controls']
        assert page['opaque_count'] == 0 and page['opaque_locators'] == []
        assert all(source['controls'][0]['combobox']['link_id'] == 'missing'
                   for source in (page, witness))
        assert page['controls'][0]['combobox']['linked'] is False
        assert witness['controls'][0]['combobox']['linked'] is False
    else:
        assert page['opaque_controls'] and not page['inventory_verified']
        combos = [c for c in witness['controls'] if c.get('type') == 'combobox']
        if scenario in {'shared', 'wrong-role', 'duplicate-id'}:
            assert combos and all(not c['combobox']['linked'] for c in combos)
        elif scenario == 'other-form':
            # Cross-form ARIA ID references are not owned by this application
            # receiver. Keep the unresolved claim visible but never lend its
            # foreign option to an approved choice or final Submit.
            for source in (page, witness):
                owners = [c for c in source['controls'] if c.get('type') == 'combobox']
                assert owners and all(
                    c['combobox']['linked'] is False
                    and c['combobox']['link_id'] == 'choices'
                    and c['combobox']['options'] == [] for c in owners)
        elif scenario == 'nested':
            assert combos and combos[0]['combobox']['linked']
        elif scenario == 'orphan-option':
            assert any(c['type'] == 'option' for c in witness['controls'])
        elif scenario == 'orphan-listbox':
            assert witness['controls'] == []


@pytest.mark.parametrize('query', ['?job_id=other', '?job_id=one&job_id=other', '?tenant=other'])
def test_added_or_ambiguous_query_identity_never_authorizes_navigation(query):
    assert not forms.navigation_allowed('https://example.test/job/1' + query,
                                        'https://example.test/job/1')


async def test_approved_field_still_gates_when_the_form_is_opaque(monkeypatch, tmp_path):
    from tests.tools.test_browser_integration_regressions import _world, URL, COMPANY, PK
    row, current, tracking, raw, effects, pdf = _world(monkeypatch, tmp_path)
    current['opaque_controls'] = True
    monkeypatch.setattr(bsk, 'decision', AsyncMock(side_effect=[
        dict(action='fill', selector='#email', fact='Email'),
        dict(action='upload', selector='#resume'), dict(action='submit', selector='#submit')]))
    # ARCH21:18/ARCH01:58, AC2/AC5: opacity is final Submit evidence, not a
    # reason to block an exact approved answer or the row's protected PDF.
    result = await forms.apply(URL, COMPANY, {'Email': 'test@example.test'},
                               'synthetic', pk=PK, resume_path=pdf)
    assert result['status'] == 'gate', result
    assert result.get('item') == 2 and result.get('reason') == 'defect_pre_submit_item_2'
    assert effects == [('navigate', URL), ('upload', '#resume'), ('fill', '#email')]
    assert not raw._application_controller.committed


async def test_human_approved_fact_is_not_authorization_for_another_question(monkeypatch):
    monkeypatch.setattr(forms, '_row', lambda pk: {
        'pk': pk, 'status': 'submitting', 'human_approved_answers': {'Name': 'Test User'}})
    session = SimpleNamespace(call=AsyncMock())
    # Model-selected facts are no longer checked against a per-control grant.
    # The raw final check remains responsible for the current form state.
    await forms.execute(session, page(target(label='Years of experience'),
                                      application_marker=True),
                        {'action': 'fill', 'selector': '#field', 'fact': 'Name'},
                        facts={'Name': 'Test User'}, filled={}, resume_path='',
                        company='example-co', jd_text='', resume_tex='', github='', pk='example-co#1')
    session.call.assert_awaited_once_with('fill', '#field', '--value', 'Test User')


async def test_writer_overwrite_does_not_reuse_old_human_bank_provenance(tmp_path, monkeypatch):
    from core.models import AnswerScope
    from core.storage.local import MarkdownAnswerBank
    bank = MarkdownAnswerBank(tmp_path / 'synthetic-facts.md')
    bank.put('Years of experience', '2 years', AnswerScope.GLOBAL, source='dashboard')
    bank.put('Years of experience', '10 years', AnswerScope.GLOBAL, source='writer')
    facts = bank.all_facts('example-co')
    assert facts['Years of experience'] == '10 years'
    monkeypatch.setattr(forms, '_row', lambda pk: {
        'pk': pk, 'status': 'submitting',
        'human_approved_answers': {'Years of experience': '2 years'}})
    session = SimpleNamespace(call=AsyncMock())
    # The last recorded bank fact is used during ungated preparation;
    # the obsolete historical grant does not authorize or veto this write.
    await forms.execute(session, page(target(label='Years of experience'),
                                      application_marker=True),
                        {'action': 'fill', 'selector': '#field', 'fact': 'Years of experience'},
                        facts=facts, filled={}, resume_path='', company='example-co',
                        jd_text='', resume_tex='', github='', pk='example-co#1')
    session.call.assert_awaited_once_with(
        'fill', '#field', '--value', '10 years')


@pytest.mark.parametrize('route', ['/apply', '/confirm?send=1'])
async def test_observed_same_job_action_link_cannot_commit_by_navigation(monkeypatch, route):
    url = 'https://employer.test/job/1'
    href = url + route
    current = page(target(tag='a', type='a', label='Apply now', href=href, in_form=False))
    navigations = []
    class Session:
        def __init__(self, kind):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def navigate(self, destination):
            navigations.append(destination)
            return current
        async def page(self):
            return current
        async def call(self, *args, **kwargs):
            if args[0] == 'navigate':
                navigations.append(args[1])
                current['url'] = args[1]
                return {}
            pytest.fail('An observed link must not click or write private values')
    monkeypatch.setattr(bsk, 'Session', Session)
    monkeypatch.setattr(forms, 'check_dispatch', lambda *a: None)
    monkeypatch.setattr(forms, '_row', lambda pk: dict(
        pk=pk, company='example-co', job_id='1', jd_url=url, status='submitting'))
    monkeypatch.setattr('tools.claude_chrome._stage_resume', lambda *a: 'Resume.pdf')
    monkeypatch.setattr(bsk, 'decision', AsyncMock(side_effect=[
        {'action': 'navigate', 'url': href}, {'action': 'gate', 'question': 'Stop'}]))
    result = await forms.apply(url, 'example-co', {}, 'synthetic',
                               pk='example-co#1', resume_path='Resume.pdf')
    assert result['status'] == 'gate'
    # ARCH21:18 / binding 2(i): an observed same-job read-only Apply GET is
    # preparation. An unapproved send/query identity still has zero effect.
    assert navigations == ([url, href] if route == '/apply' else [url])


@pytest.mark.parametrize('typ,label,in_form', [
    ('submit', 'Continue', True), ('button', 'Continue', True),
    ('button', 'View', False), ('button', 'Proceed', False),
    ('button', 'Search jobs', True), ('button', 'Next page', False),
])
async def test_discovery_ambiguous_buttons_never_issue_click(monkeypatch, typ, label, in_form):
    seed = 'https://employer.test/careers'
    current = {**page(target(tag='button', type=typ, label=label,
                             in_form=in_form, submit=typ == 'submit')), 'url': seed}
    session = SimpleNamespace(call=AsyncMock(), page=AsyncMock(return_value=current))
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', AsyncMock(side_effect=[
        {'action': 'click', 'selector': '#field'},
        {'action': 'finish', 'report': {'jobs': []}}]))
    if typ == 'submit':
        with pytest.raises(ValueError, match='submit'):
            await bsk._crawl(session, 'Find postings', seed)
    else:
        assert (await bsk._crawl(session, 'Find postings', seed))['jobs'] == []
    session.call.assert_awaited_once_with('navigate', seed)

@pytest.mark.parametrize('before,after', [
    ('#/jobs/one', '#/jobs/two'), ('#/role/A', '#/role/B'),
    ('?role=one', '?role=two'), ('', '?role=other'),
    ('?role=one', '?role=one&role=two'),
])
def test_unproven_fragment_and_query_routes_never_authorize_a_different_job(before, after):
    url = 'https://employer.test/careers'
    assert not forms.navigation_allowed(url + after, url + before)


@pytest.fixture
def form_gate_world(monkeypatch, tmp_path):
    import fakeredis
    from agent import run
    from core.models import Status
    from core.storage.local import MarkdownAnswerBank, RedisTracking
    pk = 'example-co#1'
    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    _seed_clean(tracking, 'example-co', '1', page()['url'])
    tracking.set_status(pk, Status.TAILORED)
    bank = MarkdownAnswerBank(tmp_path / 'synthetic-facts.md')
    stores = SimpleNamespace(tracking=tracking, answer_bank=bank, secrets=None)
    monkeypatch.setattr('core.stores.make_stores', lambda *a, **kw: stores)
    from tools import posting_guard as pg
    monkeypatch.setattr(run, '_jd_meta', AsyncMock(return_value=pg.analyze(GOOD)))
    monkeypatch.setattr(run, '_github_context', lambda: '')
    monkeypatch.setattr(run, '_resume_pdf_path', lambda row: 'Resume.pdf')
    monkeypatch.setattr('tools.credentials.get_login', lambda *a: None)
    monkeypatch.setattr('core.rotation.ensure', lambda *a: None)
    monkeypatch.setattr('core.profiles.resolve_for', lambda row: None)
    monkeypatch.setattr(runtime, 'configuration', lambda: {'engine': 'browser_skill'})
    monkeypatch.setattr('tools.browser_apply.apply', forms.apply)
    monkeypatch.setattr(forms, 'check_dispatch', lambda *a: None)
    monkeypatch.setattr('tools.claude_chrome._stage_resume', lambda *a: 'Resume.pdf')
    def enqueue(pk, stores, **kw):
        tracking.set_status(pk, Status.TAILORED, gate_reason='')
        return {'result': 'queued'}
    monkeypatch.setattr(run, '_enqueue_apply', enqueue)
    def resume(coro):
        coro.close()
        return {'result': 'resumed'}
    monkeypatch.setattr(run, '_run', resume)
    return run, stores, pk


@pytest.mark.parametrize('field,value', [('Email', 'test@example.test'),
    ('Name', 'Test User'), ('Phone', '555-0100')])
@pytest.mark.parametrize('banked', [False, True])
async def test_direct_observed_single_word_question_uses_existing_owner_fact(
        monkeypatch, form_gate_world, field, value, banked):
    """Missing facts ask the owner; available ones fill without field grants."""
    from core.models import AnswerScope
    run, stores, pk = form_gate_world
    if banked:
        stores.answer_bank.put(field, value, AnswerScope.GLOBAL, source='writer')
    current = page(target(label=field), application_marker=True)
    writes = AsyncMock()

    class Session:
        def __init__(self, kind):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def navigate(self, url):
            return current
        async def page(self):
            return current
        call = writes

    monkeypatch.setattr(bsk, 'Session', Session)
    action = {'action': 'fill', 'selector': '#field', 'fact': field}
    stop = {'action': 'gate', 'question': 'Stop after the supplied-fact fill'}
    monkeypatch.setattr(bsk, 'decision',
                        AsyncMock(side_effect=[action, action, stop]))
    first = await run._apply_direct(pk, stores)
    assert first['result'] == 'gated'
    if not banked:
        writes.assert_not_awaited()
        pending = stores.tracking.get(pk)
        question = pending['gate_pending']['form_question']
        assert question['selector'] == '#field'
        assert pending['gate_pending']['review_route'] == 'owner'
        assert pending['gate_call_id'] == 'direct'
        # The existing Luna/owner correction path consumes the exact current
        # question and banks a fact, not a per-control approval.
        from tools.browser_skill_apply import owner_question_identity
        replied = run.resume_job(
            pk, value, stores, disposition='answer',
            expected_gate_generation=pending['gate_pending']['generation'],
            expected_question_identity=owner_question_identity(pk, question))
        assert replied['result'] == 'queued'
        assert stores.answer_bank.all_facts('example-co')[field] == value
        monkeypatch.setattr(bsk, 'decision',
                            AsyncMock(side_effect=[action, action, stop]))
        second = await run._apply_direct(pk, stores)
        assert second['result'] == 'gated'
    assert [entry.args for entry in writes.await_args_list] == [
        ('fill', '#field', '--value', value)] * 2
    assert not stores.tracking.get(pk).get('human_approved_answers')


@pytest.mark.parametrize('source,call_id', [('tailor', 'adk'), ('critic', 'adk'),
    ('applier', 'direct')])
async def test_unrelated_question_banked_fact_is_ordinary_ungated_preparation(
        monkeypatch, form_gate_world, source, call_id):
    from core.models import Status
    run, stores, pk = form_gate_world
    field, value = 'Desired salary range', 'Synthetic range'
    stores.tracking.set_status(pk, Status.NEEDS_HUMAN, gate_source=source, gate_call_id=call_id,
        gate_pending={'question': f'For internal planning, answer "{field}".'})
    run.resume_job(pk, value, stores)
    assert stores.answer_bank.all_facts('example-co')[field] == value
    stores.tracking.set_status(pk, Status.SUBMITTING)
    session = SimpleNamespace(call=AsyncMock())
    receipt = await forms.execute(session, page(target(label=field)),
        {'action': 'fill', 'selector': '#field', 'fact': field},
        facts=stores.answer_bank.all_facts('example-co'), filled={}, resume_path='',
        company='example-co', jd_text='', resume_tex='', github='', pk=pk)
    assert receipt['action'] == 'fill'
    session.call.assert_awaited_once_with('fill', '#field', '--value', value)
    # A bank fact does not recreate any retired per-field grant.
    assert not stores.tracking.get(pk).get('human_approved_answers')


@pytest.mark.parametrize('stage', ['initial', 'jit'])
@pytest.mark.parametrize('before,after', [('#/jobs/one', '#/jobs/two'),
    ('?role=one', '?role=two'), ('?role=one', '?role=one&role=two')])
async def test_changed_hash_or_query_redirect_stops_before_any_later_form_write(
        monkeypatch, form_gate_world, stage, before, after):
    _, stores, pk = form_gate_world
    url = 'https://employer.test/careers'
    approved, different = url + before, url + after
    current = {**page(target(label='Name')), 'url': approved}
    redirected = {**current, 'url': different}
    stores.tracking.set_status(pk, 'submitting', jd_url=approved,
                               human_approved_answers={'Name': 'Test User'})
    writes = AsyncMock()
    class Session:
        def __init__(self, kind):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def navigate(self, url):
            return redirected if stage == 'initial' else current
        async def page(self):
            return redirected
        call = writes
    monkeypatch.setattr(bsk, 'Session', Session)
    decide = AsyncMock(side_effect=[{'action': 'fill', 'selector': '#field', 'fact': 'Name'},
                                   {'action': 'gate', 'question': 'Stop'}])
    monkeypatch.setattr(bsk, 'decision', decide)
    result = await forms.apply(approved, 'example-co', {'Name': 'Test User'}, 'synthetic',
                               pk=pk, resume_path='Resume.pdf')
    assert result['status'] == 'gate'
    decide.assert_not_awaited()  # Route drift stops before a new model write decision.
    writes.assert_not_awaited()


@pytest.mark.parametrize('url', ['https://employer.test/careers#/jobs/one',
    'https://employer.test/careers?role=one',
    'https://employer.test/careers?role=one&department=engineering'])
def test_unchanged_fragment_and_full_query_are_still_proven_job_context(url):
    assert forms.navigation_allowed(url, url)


@pytest.mark.parametrize('verb,control,value', [
    ('fill', target(type='search', label='Search jobs', in_form=False), 'engineering'),
    ('select', target(tag='select', type='select', label='Location filter',
                      in_form=False, options=[{'value': 'remote', 'label': 'Remote'}]), 'remote'),
])
async def test_discovery_keeps_reading_and_native_search_filter_writes(monkeypatch, verb, control, value):
    current = page(control)
    session = SimpleNamespace(page=AsyncMock(return_value=current), call=AsyncMock())
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', AsyncMock(side_effect=[
        {'action': verb, 'selector': '#field', 'value': value},
        {'action': 'finish', 'report': {'jobs': []}}]))
    assert (await bsk._crawl(session, 'Find postings', current['url']))['jobs'] == []
    assert session.call.await_count == 2
    session.call.assert_any_await('navigate', current['url'])
    session.call.assert_any_await(verb, '#field', '--value', value)

@pytest.mark.parametrize('corruption', ['source', 'call_id', 'status', 'application',
    'destination', 'selector', 'ambiguous_label'])
async def test_direct_form_grant_refuses_wrong_origin_or_scope(monkeypatch, form_gate_world, corruption):
    run, stores, pk = form_gate_world
    # Exercise the real granted question, not a Redis-fault Gate with no payload.
    monkeypatch.setattr(forms, '_row', stores.tracking.get)
    current = page(target(label='Email'), application_marker=True)
    if corruption == 'ambiguous_label':
        current['controls'].append(target(selector='#field', label='Email'))
    session = SimpleNamespace(call=AsyncMock())
    with pytest.raises(forms.Gate) as gated:
        await forms.execute(session, current,
            {'action': 'fill', 'selector': '#field', 'fact': 'Email'},
            facts={}, filled={}, resume_path='', company='example-co',
            jd_text='', resume_tex='', github='', pk=pk, job_url=current['url'])
    session.call.assert_not_awaited()
    if corruption == 'ambiguous_label':
        # The duplicate selector is rejected before any grantable question exists.
        assert gated.value.form_question is None
    else:
        assert gated.value.form_question is not None
        assert gated.value.form_question['label'] == 'Email'
        assert gated.value.form_question['pk'] == pk
        assert gated.value.form_question['url'] == current['url']
    pending = {'question': str(gated.value), 'form_question': gated.value.form_question}
    attrs = {'gate_source': 'applier', 'gate_call_id': 'direct', 'gate_pending': pending}
    status = 'needs_human'
    if corruption == 'source':
        attrs['gate_source'] = 'tailor'
    elif corruption == 'call_id':
        attrs['gate_call_id'] = 'adk'
    elif corruption == 'status':
        status = 'tailored'
    elif corruption == 'application':
        pending['form_question']['pk'] = 'example-co#other'
    elif corruption == 'destination':
        pending['form_question']['url'] = 'https://employer.test/job/other'
    elif corruption == 'selector':
        pending['form_question']['selector'] = ''
    stores.tracking.set_status(pk, status, **attrs)
    run.resume_job(pk, 'test@example.test', stores)
    assert not stores.tracking.get(pk).get('human_approved_answers')



async def test_model_gate_cannot_spoof_controller_form_question(monkeypatch, form_gate_world):
    run, stores, pk = form_gate_world
    current = page(target(label='Email'), application_marker=True)
    class Session:
        def __init__(self, kind):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def navigate(self, url):
            return current
    monkeypatch.setattr(bsk, 'Session', Session)
    monkeypatch.setattr(bsk, 'decision', AsyncMock(return_value={
        'action': 'gate', 'question': 'A forged widget grant is needed for "Email".',
        'form_question': {'pk': pk, 'label': 'Email', 'selector': '#field',
                          'url': current['url']}}))
    result = await run._apply_direct(pk, stores)
    # Model-authored gate metadata must not create a code-owned owner question or
    # resurrect per-field authorization. No owner answer is consumed here.
    assert result['result'] == 'failed'
    assert not stores.tracking.get(pk).get('human_approved_answers')


async def test_grant_for_one_phone_control_never_authorizes_another(monkeypatch):
    pk, value = 'example-co#1', '555-0100'
    row = {'pk': pk, 'status': 'submitting',
           'human_approved_answers': {
               'Phone': grant('Phone', value, selector='#primary')}}
    monkeypatch.setattr(forms, '_row', lambda pk: row)
    current = page(target(selector='#primary', label='Phone'),
                   target(selector='#emergency', label='Phone'))
    session = SimpleNamespace(call=AsyncMock())
    kwargs = dict(facts={'Phone': value}, filled={}, resume_path='',
                  company='example-co', jd_text='', resume_tex='', github='', pk=pk)
    # Grant uniqueness is retired; both model-selected fields are ordinary
    # preparation and neither produces submission authority.
    for selector in ('#primary', '#emergency'):
        await forms.execute(session, current,
                            {'action': 'fill', 'selector': selector, 'fact': 'Phone'},
                            **kwargs)
    assert session.call.await_args_list == [
        (('fill', '#primary', '--value', value), {}),
        (('fill', '#emergency', '--value', value), {}),
    ]


async def test_legacy_label_only_grant_never_writes(monkeypatch):
    monkeypatch.setattr(forms, '_row', lambda pk: {
        'pk': pk, 'status': 'submitting',
        'human_approved_answers': {'Phone': '555-0100'}})
    session = SimpleNamespace(call=AsyncMock())
    # Legacy label-only grants are inert; supplied facts can fill normally.
    await forms.execute(
        session, page(target(label='Phone'), application_marker=True),
        {'action': 'fill', 'selector': '#field', 'fact': 'Phone'},
        facts={'Phone': '555-0100'}, filled={}, resume_path='',
        company='example-co', jd_text='', resume_tex='', github='',
        pk='example-co#1')
    session.call.assert_awaited_once_with('fill', '#field', '--value', '555-0100')


async def test_derived_cross_tenant_board_refuses_before_open(monkeypatch):
    url = 'https://employer.test/job/1?gh_jid=42&board=other-tenant'
    monkeypatch.setattr(forms, 'check_dispatch', lambda *a: None)
    monkeypatch.setattr(bsk, 'Session', lambda *a: pytest.fail(
        'Cross-tenant direct board must be rejected before browser open'))
    result = await forms.apply(url, 'example-co', {}, 'synthetic',
                               pk='example-co#1', resume_path='Resume.pdf')
    assert result['status'] == 'gate'
    # This is a malformed tenant-bearing URL, not an absent board pin (AC14).
    assert 'Malformed Greenhouse job identity' in result['question']


@pytest.mark.parametrize('bad_receiver', ['form_action', 'formaction'])
async def test_external_form_receiver_gates_before_upload_or_submit_click(
        monkeypatch, bad_receiver):
    resume = target(selector='#resume', type='file', label='Resume',
                    files=['Resume.pdf'])
    submit = target(selector='#submit', tag='button', type='submit',
                    label='Submit application', submit=True)
    if bad_receiver == 'form_action':
        resume['form_action'] = 'https://foreign.test/receive'
        submit['form_action'] = 'https://foreign.test/receive'
    else:
        submit['formaction'] = 'https://foreign.test/receive'
    current = page(resume, submit)
    calls = []
    class FakeSession:
        def __init__(self, kind):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def navigate(self, url):
            return current
        async def page(self):
            return current
        async def call(self, *args, **kwargs):
            calls.append(args[0])
            if args[0] == 'click':
                pytest.fail('External form receiver reached committing click IPC')
    monkeypatch.setattr(forms, 'check_dispatch', lambda *a: None)
    # Isolate the receiver boundary rather than failing on a missing dispatch.
    monkeypatch.setattr(forms, '_row', lambda pk: dict(
        pk=pk, company='example-co', job_id='1', jd_url=current['url'], status='submitting'))
    monkeypatch.setattr('tools.claude_chrome._stage_resume', lambda *a: 'Resume.pdf')
    monkeypatch.setattr(bsk, 'Session', FakeSession)
    monkeypatch.setattr(bsk, 'decision', AsyncMock(side_effect=[
        {'action': 'upload', 'selector': '#resume'},
        {'action': 'submit', 'selector': '#submit'}]))
    result = await forms.apply(current['url'], 'example-co', {}, 'synthetic',
                               pk='example-co#1', resume_path='Resume.pdf')
    assert result['status'] == 'gate'
    assert 'form destination' in result['question']
    assert 'click' not in calls
    assert calls == []  # even upload is forbidden when any same-form receiver is foreign


@pytest.mark.parametrize('declared_submit', [False, True])
async def test_submit_label_on_js_button_cannot_prove_native_click(
        monkeypatch, tmp_path, declared_submit):
    from tests.tools.test_browser_integration_regressions import _world, URL, COMPANY, PK
    row, current, tracking, raw, effects, pdf = _world(monkeypatch, tmp_path)
    button = next(c for c in current['controls'] if c['selector'] == '#submit')
    button.update(type='button', submit=declared_submit)
    actions = iter([dict(action='fill', selector='#email', fact='Email'),
                    dict(action='submit', selector='#submit')])
    async def decide(*args, **kwargs):
        action = next(actions)
        if action['action'] == 'submit':
            # The PDF was uploaded before the first model decision, but the
            # live input lost its attachment. A label-only JS Submit must
            # encounter the same sole final raw-IPC PDF check.
            next(c for c in current['controls'] if c['selector'] == '#resume')['files'] = []
        return action
    monkeypatch.setattr(bsk, 'decision', decide)
    final_calls = []
    original = forms.ApplicationIPCController.check_pre_submit
    async def final(controller, *args):
        final_calls.append(controller)
        return await original(controller, *args)
    monkeypatch.setattr(forms.ApplicationIPCController, 'check_pre_submit', final)
    result = await forms.apply(URL, COMPANY, {'Email': 'test@example.test'},
                               'synthetic', pk=PK, resume_path=pdf)
    assert result['status'] == 'gate' and result['item'] == 2, result
    assert result['reason'] == 'defect_pre_submit_item_2'
    assert final_calls == [raw._application_controller]
    assert effects == [('navigate', URL), ('upload', '#resume'), ('fill', '#email')]


def test_form_receiver_preflight_scopes_distinct_native_forms():
    upload = target(selector='#resume', type='file', label='Resume',
                    form_selector='#application')
    submit = target(selector='#submit', tag='button', type='submit',
                    label='Submit application', submit=True,
                    form_selector='#application')
    unrelated = target(selector='#other', tag='button', type='submit',
                       label='Unrelated', submit=True, form_selector='#other-form',
                       form_action='https://foreign.test/receive')
    current = page(upload, submit, unrelated)
    forms.check_form_destination(current, upload, current['url'])
    submit['formaction'] = 'https://foreign.test/receive'
    with pytest.raises(forms.Gate, match='form destination'):
        forms.check_form_destination(current, upload, current['url'])


def test_native_same_job_form_receiver_is_authorized():
    current = page(target(selector='#submit', tag='button', type='submit',
                          label='Submit application', submit=True))
    forms.check_form_destination(current, current['controls'][0], current['url'])


def test_dom_inventory_exposes_effective_native_action_and_formaction():
    import subprocess
    from tools.browser_skill_dom import PAGE
    script = r"""
const form = {id:'application',action:'https://employer.test/job/1/submit', querySelector:()=>null};
const button = {id:'submit',tagName:'BUTTON',type:'submit',form,formAction:'https://foreign.test/receive',
  labels:[],name:'',innerText:'Submit application',required:false,disabled:false,value:'',
  getClientRects:()=>[1],closest:q=>q==='form'?form:null,matches:()=>false,
  getAttribute:k=>k==='formaction'?'/receive':null};
global.document={readyState:'complete',title:'Synthetic application',body:{innerText:''},
  querySelector:()=>null,getElementById:()=>null,
  querySelectorAll:q=>q === '.iti' || q === '[role="combobox"]' ? [] : [button]};
global.CSS={escape:s=>s};
global.location={href:'https://employer.test/job/1'};
global.getComputedStyle=()=>({visibility:'visible'});
console.log(JSON.stringify(eval(process.argv[1])));
"""
    observed = json.loads(subprocess.check_output(['node', '-e', script, PAGE], text=True))
    control = observed['controls'][0]
    assert control['form_action'] == 'https://employer.test/job/1/submit'
    assert control['formaction'] == 'https://foreign.test/receive'
    assert control['type'] == 'submit'  # default native BUTTON.type without type attribute
    assert forms.native_submit(control)
    with pytest.raises(forms.Gate, match='form destination'):
        forms.check_form_destination(observed, control, observed['url'])


async def test_owner_fact_fill_uses_current_target_without_grant(
        monkeypatch):
    pk, value = 'example-co#synthetic', 'Test User'
    observed = page(target(label='Name', question=''),
                    application_marker=True)
    row = {'pk': pk, 'status': 'submitting',
           'human_approved_answers': {'Name': grant('Name', value)}}
    monkeypatch.setattr(forms, '_row', lambda pk: row)
    session = SimpleNamespace(call=AsyncMock())
    receipt = await forms.execute(
        session, observed, {'action': 'fill', 'selector': '#field', 'fact': 'Name'},
        facts={'Name': value}, filled={}, resume_path='', company='example-co',
        jd_text='', resume_tex='', github='', pk=pk)
    assert receipt['action'] == 'fill'
    session.call.assert_awaited_once_with('fill', '#field', '--value', value)


_DISCOVERY_UNCHANGED = object()


class DiscoveryFakeSession:
    """Isolated DOM inventories; no browser, daemon, network or owner state."""
    def __init__(self, current, *, nav_results=None, write_result=_DISCOVERY_UNCHANGED):
        self.current = current
        self.nav_results = nav_results or {}
        self.write_result = write_result
        self.calls = []
        self.reads = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def call(self, *args):
        self.calls.append(args)
        if args[0] == 'navigate':
            self.current = self.nav_results.get(args[1], self.current)
        elif args[0] in {'fill', 'select'}:
            if self.write_result is not _DISCOVERY_UNCHANGED:
                self.current = self.write_result
        else:
            pytest.fail('Unexpected discovery browser IPC')
        return {}

    async def page(self):
        self.reads.append(self.current.get('url', '<missing>')
                          if isinstance(self.current, dict) else '<invalid>')
        return self.current


def discovery_listing(*links, more=()):
    seed = 'https://employer.test/careers'
    return {**page(*links, *more), 'url': seed}


def discovery_posting():
    return target(selector='#job', tag='a', type='a', in_form=False,
                  label='Senior Data Engineer', href='https://employer.test/jobs/42')


@pytest.mark.parametrize('proposed,reason', [
    ({'action': 'click', 'selector': '#block'}, 'cannot prove this click'),
    ({'action': 'dance'}, 'Unsupported discovery action'),
    ({'action': 'navigate', 'url': 'https://foreign.test/jobs/99'},
     'no proven safe employer route'),
    ({'action': 'fill', 'selector': '#block', 'value': 'Test User'},
     'search/filter fields'),
    ({'action': 'select', 'selector': '#block', 'value': 'one'},
     'search/filter fields'),
])
async def test_discovery_refusal_before_ipc_then_safe_finish(monkeypatch, proposed, reason):
    seed = 'https://employer.test/careers'
    current = discovery_listing(discovery_posting(),
                                target(selector='#block', tag='button',
                                       type='button', label='Next', in_form=False))
    session = DiscoveryFakeSession(current)
    finish = {'action': 'finish', 'report': {'jobs': [
        {'url': discovery_posting()['href'], 'title': 'Model supplied title'}]}}
    decide = AsyncMock(side_effect=[proposed, finish])
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', decide)
    assert (await bsk._crawl(session, 'Find roles', seed))['jobs'] == finish['report']['jobs']
    assert session.calls == [('navigate', seed)]
    assert session.reads == [seed]
    assert decide.await_count == 2
    history = decide.await_args_list[1].args[2]
    assert len(history) == 1 and reason in history[0]['reason']
    assert history[0]['action'] == proposed['action']
    assert history[0]['page_url'] == seed
    assert (history[0]['selector'] == proposed.get('selector')
            and history[0]['url'] == proposed.get('url'))
    assert decide.await_args_list[1].args[1]['url'] == seed


@pytest.mark.parametrize('target_control', [
    target(selector='#block', tag='button', type='submit', label='Search jobs', submit=True),
    target(selector='#block', tag='button', type='submit', label='Next', submit=True),
    target(selector='#block', tag='button', type='button',
           label='Submit application', submit=False),
])
async def test_discovery_submission_control_remains_fatal(monkeypatch, target_control):
    seed = 'https://employer.test/careers'
    session = DiscoveryFakeSession(discovery_listing(target_control))
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', AsyncMock(return_value={
        'action': 'click', 'selector': '#block'}))
    with pytest.raises(ValueError, match='submit'):
        await bsk._crawl(session, 'Find roles', seed)
    assert session.calls == [('navigate', seed)]


@pytest.mark.parametrize('control_count,disabled', [(0, False), (2, False), (1, True)])
async def test_discovery_control_gate_is_not_a_refusal(monkeypatch, control_count, disabled):
    seed = 'https://employer.test/careers'
    controls = [target(selector='#block', tag='button', type='button',
                       label='Next', disabled=disabled) for _ in range(control_count)]
    session = DiscoveryFakeSession(discovery_listing(*controls))
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', AsyncMock(return_value={
        'action': 'click', 'selector': '#block'}))
    with pytest.raises(forms.Gate, match='absent, disabled or ambiguous'):
        await bsk._crawl(session, 'Find roles', seed)
    assert session.calls == [('navigate', seed)]


@pytest.mark.parametrize('action,exception', [
    ({'action': 'submit'}, ValueError),
    ({'action': 'gate', 'question': 'Inspect'}, bsk.Unavailable),
])
async def test_discovery_literal_submit_and_human_gate_are_fatal(monkeypatch, action, exception):
    seed = 'https://employer.test/careers'
    session = DiscoveryFakeSession(discovery_listing())
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', AsyncMock(return_value=action))
    with pytest.raises(exception):
        await bsk._crawl(session, 'Find roles', seed)
    assert session.calls == [('navigate', seed)]


async def test_discovery_finish_retains_url_only_provenance_not_title_proof(monkeypatch):
    seed = 'https://employer.test/careers'
    navigation = target(selector='#all', tag='a', type='a', in_form=False,
                        label='All jobs', href=seed + '/all')
    role = discovery_posting()
    session = DiscoveryFakeSession(discovery_listing(navigation, role))
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    bogus = [{'url': seed, 'title': 'Invented for seed'},
             {'url': navigation['href'], 'title': 'Invented for navigation'},
             {'url': role['href'], 'title': 'Model supplied title'},
             {'url': 'https://foreign.test/jobs/9', 'title': 'Unsafe'},
             {'url': seed + '/apply', 'title': 'Commit route'}]
    monkeypatch.setattr(bsk, 'decision', AsyncMock(return_value={
        'action': 'finish', 'report': {'jobs': bogus}}))
    assert (await bsk._crawl(session, 'Find roles', seed))['jobs'] == bogus[:3]


async def test_discovery_repeated_refusal_fallback_only_unambiguous_title_anchor(monkeypatch):
    seed = 'https://employer.test/careers'
    job = discovery_posting()
    page_controls = [job,
                     target(selector='#all', tag='a', type='a', in_form=False,
                            label='All jobs', href=seed + '/all'),
                     target(selector='#details', tag='a', type='a', in_form=False,
                            label='View details', href=seed + '/jobs/43'),
                     target(selector='#blank', tag='a', type='a', in_form=False,
                            label='', href=seed + '/jobs/44'),
                     target(selector='#block', tag='button', type='button',
                            label='Next', in_form=False)]
    session = DiscoveryFakeSession(discovery_listing(*page_controls))
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', AsyncMock(side_effect=[
        {'action': 'click', 'selector': '#block'}] * 2))
    report = await bsk._crawl(session, 'Find roles', seed)
    assert report == {'jobs': [{'url': job['href'], 'title': job['label']}],
                      'note': 'discovery stopped after repeated refused actions'}
    assert session.calls == [('navigate', seed)]
    assert session.reads == [seed]


@pytest.mark.parametrize('mutate', [
    lambda c, p: c.update(in_form=True),
    lambda c, p: c.pop('in_form'),
    lambda c, p: c.update(in_form='false'),
    lambda c, p: c.update(in_form=0),
    lambda c, p: c.update(type='file'),
    lambda c, p: p.update(inventory_verified=False),
    lambda c, p: p.update(truncated=True),
    lambda c, p: c.update(label='View details'),
    lambda c, p: c.update(label='All Senior Engineer Jobs'),
])
async def test_discovery_repeated_refusal_excludes_uncertain_anchor(monkeypatch, mutate):
    seed = 'https://employer.test/careers'
    job = discovery_posting()
    current = discovery_listing(job,
        target(selector='#block', tag='button', type='button', label='Next', in_form=False))
    mutate(job, current)
    session = DiscoveryFakeSession(current)
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', AsyncMock(return_value={
        'action': 'click', 'selector': '#block'}))
    with pytest.raises(bsk.Unavailable, match='repeated refused'):
        await bsk._crawl(session, 'Find roles', seed)
    assert session.calls == [('navigate', seed)]


async def test_discovery_repeated_proposal_after_verified_page_change_is_not_false_repeat(monkeypatch):
    seed = 'https://employer.test/careers'
    next_page = seed + '/all'
    block = target(selector='#block', tag='button', type='button', label='Next', in_form=False)
    link = target(selector='#all', tag='a', type='a', in_form=False,
                  label='All jobs', href=next_page)
    first = discovery_listing(block, link, discovery_posting())
    second = {**discovery_listing(block, discovery_posting()), 'url': next_page}
    session = DiscoveryFakeSession(first, nav_results={next_page: second})
    decide = AsyncMock(side_effect=[
        {'action': 'click', 'selector': '#block'},
        {'action': 'navigate', 'url': next_page},
        {'action': 'click', 'selector': '#block'},
        {'action': 'finish', 'report': {'jobs': []}}])
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', decide)
    assert (await bsk._crawl(session, 'Find roles', seed))['jobs'] == []
    assert decide.await_count == 4
    assert session.calls == [('navigate', seed), ('navigate', next_page)]


@pytest.mark.parametrize('with_witness', [False, True])
async def test_discovery_80_decisions_provide_evidenced_or_explicit_empty(monkeypatch, with_witness):
    seed = 'https://employer.test/careers'
    session = DiscoveryFakeSession(discovery_listing(*([discovery_posting()] if with_witness else [])))
    decide = AsyncMock(return_value={'action': 'wait'})
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', decide)
    monkeypatch.setattr(bsk.asyncio, 'sleep', AsyncMock())
    if with_witness:
        report = await bsk._crawl(session, 'Find roles', seed)
        assert report == {'jobs': [{'url': discovery_posting()['href'],
                                   'title': discovery_posting()['label']}],
                          'note': 'discovery step limit reached after 80 decisions'}
    else:
        with pytest.raises(bsk.Unavailable, match='step limit'):
            await bsk._crawl(session, 'Find roles', seed)
    assert decide.await_count == 80
    assert session.calls == [('navigate', seed)]


@pytest.mark.parametrize('decision_number', [1, 80])
async def test_discovery_redirect_first_foreign_inventory_no_subsequent_read(monkeypatch,
                                                                                decision_number):
    seed = 'https://employer.test/careers'
    allowed = seed + '/all'
    foreign = {'url': 'https://foreign.test/x', 'text': '', 'controls': []}
    link = target(selector='#all', tag='a', type='a', in_form=False,
                  label='All jobs', href=allowed)
    session = DiscoveryFakeSession(discovery_listing(discovery_posting(), link),
                                   nav_results={allowed: foreign})
    proposed = [{'action': 'wait'}] * (decision_number - 1) + [
        {'action': 'navigate', 'url': allowed}]
    decide = AsyncMock(side_effect=proposed)
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', decide)
    monkeypatch.setattr(bsk.asyncio, 'sleep', AsyncMock())
    with pytest.raises(ValueError, match='employer or tenant'):
        await bsk._crawl(session, 'Find roles', seed)
    assert decide.await_count == decision_number
    assert session.calls == [('navigate', seed), ('navigate', allowed)]
    assert session.reads.count(foreign['url']) == 1
    assert session.reads[-1] == foreign['url']


async def test_discovery_initial_seed_redirect_stops_first_foreign_read(monkeypatch):
    seed = 'https://employer.test/careers'
    foreign = {'url': 'https://foreign.test/', 'text': '', 'controls': []}
    session = DiscoveryFakeSession(discovery_listing(), nav_results={seed: foreign})
    decide = AsyncMock()
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', decide)
    with pytest.raises(ValueError, match='employer or tenant'):
        await bsk._crawl(session, 'Find roles', seed)
    assert session.reads == [foreign['url']]
    assert session.calls == [('navigate', seed)]
    decide.assert_not_awaited()


async def test_discovery_80th_filter_effect_foreign_page_preempts_fallback(monkeypatch):
    seed = 'https://employer.test/careers'
    search = target(selector='#search', tag='input', type='search',
                    label='Search jobs', in_form=False)
    foreign = {'url': 'https://foreign.test/', 'text': '', 'controls': []}
    session = DiscoveryFakeSession(discovery_listing(discovery_posting(), search),
                                   write_result=foreign)
    decide = AsyncMock(side_effect=[{'action': 'wait'}] * 79 + [
        {'action': 'fill', 'selector': '#search', 'value': 'data'}])
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', decide)
    monkeypatch.setattr(bsk.asyncio, 'sleep', AsyncMock())
    with pytest.raises(ValueError, match='employer or tenant'):
        await bsk._crawl(session, 'Find roles', seed)
    assert decide.await_count == 80
    assert session.calls == [('navigate', seed), ('fill', '#search', '--value', 'data')]
    assert session.reads.count(foreign['url']) == 1
    assert session.reads[-1] == foreign['url']


@pytest.mark.parametrize('corrupt', [None, {}, {'url': 'https://employer.test/careers',
                                                'text': GOOD, 'controls': None},
    {'url': 'https://employer.test/careers', 'text': GOOD, 'controls': [None]}])
async def test_discovery_malformed_inventory_fatal_despite_prior_witness(monkeypatch, corrupt):
    seed = 'https://employer.test/careers'
    search = target(selector='#search', tag='input', type='search',
                    label='Search jobs', in_form=False)
    session = DiscoveryFakeSession(discovery_listing(discovery_posting(), search),
                                   write_result=corrupt)
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', AsyncMock(side_effect=[
        {'action': 'fill', 'selector': '#search', 'value': 'data'},
        {'action': 'finish', 'report': {'jobs': []}}]))
    with pytest.raises(bsk.Unavailable, match='invalid discovery inventory'):
        await bsk._crawl(session, 'Find roles', seed)
    assert session.calls == [('navigate', seed), ('fill', '#search', '--value', 'data')]
    assert len(session.reads) == 2


@pytest.mark.parametrize('with_witness', [False, True])
async def test_discovery_run_task_repeat_reports_note_or_explicit_problem(monkeypatch, with_witness):
    seed = 'https://employer.test/careers'
    block = target(selector='#block', tag='button', type='button',
                   label='Next', in_form=False)
    session = DiscoveryFakeSession(discovery_listing(block, *(
        [discovery_posting()] if with_witness else [])))
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'Session', lambda kind: session)
    monkeypatch.setattr(bsk, 'decision', AsyncMock(return_value={
        'action': 'click', 'selector': '#block'}))
    report, problem = await bsk.run_task('Find roles', report_key='jobs', urls=[seed])
    if with_witness:
        assert problem == ''
        assert report == {'jobs': [{'url': discovery_posting()['href'],
                                   'title': discovery_posting()['label']}],
                          'note': 'discovery stopped after repeated refused actions'}
    else:
        assert report == {} and 'repeated refused' in problem
    assert session.calls == [('navigate', seed)]


async def test_discovery_final_allowed_navigation_salvages_newly_witnessed_posting(monkeypatch):
    seed = 'https://employer.test/careers'
    next_page = seed + '/openings'
    index_link = target(selector='#all', tag='a', type='a', in_form=False,
                        label='All jobs', href=next_page)
    landing = {**discovery_listing(discovery_posting()), 'url': next_page}
    session = DiscoveryFakeSession(discovery_listing(index_link),
                                   nav_results={next_page: landing})
    decide = AsyncMock(side_effect=[{'action': 'wait'}] * 79 + [
        {'action': 'navigate', 'url': next_page}])
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', decide)
    monkeypatch.setattr(bsk.asyncio, 'sleep', AsyncMock())
    report = await bsk._crawl(session, 'Find roles', seed)
    assert report == {'jobs': [{'url': discovery_posting()['href'],
                               'title': discovery_posting()['label']}],
                      'note': 'discovery step limit reached after 80 decisions'}
    assert decide.await_count == 80
    assert session.calls == [('navigate', seed), ('navigate', next_page)]
    assert session.reads[-1] == next_page

@pytest.mark.parametrize('invalid_action', [[], {}], ids=['list', 'dict'])
async def test_discovery_nonstring_action_is_sanitized_failure(monkeypatch, invalid_action):
    seed = 'https://employer.test/careers'
    session = DiscoveryFakeSession(discovery_listing(discovery_posting()))
    decide = AsyncMock(return_value={'action': invalid_action})
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'Session', lambda kind: session)
    monkeypatch.setattr(bsk, 'decision', decide)

    report, problem = await bsk.run_task('Find roles', report_key='jobs', urls=[seed])
    assert report == {}
    assert problem == 'BrowserSkill operation unavailable: invalid discovery decision'
    assert session.calls == [('navigate', seed)]
    assert session.reads == [seed]
    decide.assert_awaited_once()


async def test_discovery_fallback_excludes_collective_role_category_anchors(monkeypatch):
    seed = 'https://employer.test/careers'
    job = discovery_posting()
    another_job = target(selector='#lead', tag='a', type='a', in_form=False,
                         label='Lead Product Manager', href=seed + '/jobs/lead')
    categories = [
        target(selector=f'#category-{index}', tag='a', type='a', in_form=False,
               label=label, href=seed + f'/browse/{index}')
        for index, label in enumerate((
            'Product Manager Roles', 'Data Engineer Positions',
            'Design Manager Teams', 'Technical Director Department',
            'Senior Analyst Departments'))
    ]
    block = target(selector='#block', tag='button', type='button',
                   label='Next', in_form=False)
    session = DiscoveryFakeSession(discovery_listing(job, *categories, another_job, block))
    monkeypatch.setattr('discovery.progress.cancelled', lambda: False)
    monkeypatch.setattr(bsk, 'decision', AsyncMock(return_value={
        'action': 'click', 'selector': '#block'}))

    report = await bsk._crawl(session, 'Find roles', seed)
    assert report == {
        'jobs': [{'url': job['href'], 'title': job['label']},
                 {'url': another_job['href'], 'title': another_job['label']}],
        'note': 'discovery stopped after repeated refused actions'}
    assert session.calls == [('navigate', seed)]
    assert session.reads == [seed]


@pytest.mark.parametrize('last_decision', ['repeat', 'limit'])
async def test_discovery_cancellation_during_final_decision_preempts_fallback(
        monkeypatch, last_decision):
    seed = 'https://employer.test/careers'
    block = target(selector='#block', tag='button', type='button',
                   label='Next', in_form=False)
    session = DiscoveryFakeSession(discovery_listing(discovery_posting(), block))
    state = {'cancelled': False, 'decisions': 0}

    async def decide(*args, **kwargs):
        state['decisions'] += 1
        last = 2 if last_decision == 'repeat' else 80
        if state['decisions'] == last:
            state['cancelled'] = True
        return ({'action': 'click', 'selector': '#block'} if last_decision == 'repeat'
                else {'action': 'wait'})

    monkeypatch.setattr('discovery.progress.cancelled', lambda: state['cancelled'])
    monkeypatch.setattr(bsk, 'decision', AsyncMock(side_effect=decide))
    monkeypatch.setattr(bsk, 'Session', lambda kind: session)
    monkeypatch.setattr(bsk.asyncio, 'sleep', AsyncMock())

    report, problem = await bsk.run_task('Find roles', report_key='jobs', urls=[seed])
    assert report == {}
    assert problem == 'BrowserSkill stopped: discovery cancelled'
    assert state['decisions'] == (2 if last_decision == 'repeat' else 80)
    assert session.calls == [('navigate', seed)]


async def test_discovery_cancellation_during_final_browser_effect_preempts_fallback(
        monkeypatch):
    seed = 'https://employer.test/careers'
    state = {'cancelled': False}

    class StopAfterFilterWrite(DiscoveryFakeSession):
        async def call(self, *args):
            result = await super().call(*args)
            if args[0] == 'fill':
                state['cancelled'] = True
            return result

    search = target(selector='#search', tag='input', type='search',
                    label='Search jobs', in_form=False)
    session = StopAfterFilterWrite(discovery_listing(discovery_posting(), search))
    decide = AsyncMock(side_effect=[{'action': 'wait'}] * 79 + [
        {'action': 'fill', 'selector': '#search', 'value': 'engineer'}])
    monkeypatch.setattr('discovery.progress.cancelled', lambda: state['cancelled'])
    monkeypatch.setattr(bsk, 'decision', decide)
    monkeypatch.setattr(bsk, 'Session', lambda kind: session)
    monkeypatch.setattr(bsk.asyncio, 'sleep', AsyncMock())

    report, problem = await bsk.run_task('Find roles', report_key='jobs', urls=[seed])
    assert report == {}
    assert problem == 'BrowserSkill stopped: discovery cancelled'
    assert decide.await_count == 80
    assert session.calls == [('navigate', seed),
                             ('fill', '#search', '--value', 'engineer')]
    assert session.reads[-1] == seed

async def test_application_native_select_prompt_preserves_server_choice_semantics(monkeypatch):
    request = {}
    def completion(**kwargs):
        request.update(kwargs)
        return {'choices': [{'message': {'content': '{"action":"finish"}'},
                              'finish_reason': 'stop'}]}
    monkeypatch.setattr('litellm.completion', completion)
    monkeypatch.setattr('core.steering.messages', lambda prompt: [
        {'role': 'system', 'content': prompt}])
    assert await bsk.decision('Synthetic application', {}, [], model='synthetic') == {
        'action': 'finish'}
    prompt = request['messages'][0]['content']
    assert ('For an APPLICATION native select, leave value empty: the server chooses '
            'the option from the exact human-approved answer.') in prompt


async def test_decision_prompt_states_listed_fact_key_is_stored_answer(monkeypatch):
    request = {}

    def completion(**kwargs):
        request.update(kwargs)
        return {'choices': [{'message': {'content': '{"action":"finish"}'},
                             'finish_reason': 'stop'}]}
    monkeypatch.setattr('litellm.completion', completion)
    monkeypatch.setattr('core.steering.messages', lambda prompt: [
        {'role': 'system', 'content': prompt}])
    assert await bsk.decision('Synthetic application', {}, [], model='synthetic') == {
        'action': 'finish'}
    prompt = request['messages'][0]['content']
    assert ('essay: boolean, url: observed href, report: object, question: string}. '
            'A listed fact key IS a stored owner-approved answer that the server '
            'fills; it is never a missing fact. '
            'Use fill for text, select for native select, choose for radio/checkbox. '
            ) in prompt
    assert prompt.count('A listed fact key IS a stored owner-approved answer') == 1


_REAL_OBSERVE_CONTROLS = forms._observe_controls
_GH_URL = 'https://job-boards.greenhouse.io/example-co/jobs/123'
_GH_ALIAS = forms.GreenhouseJobAlias(
    pk='example-co#123', company='example-co', ats='greenhouse', job_id='123',
    wrapper='https://careers.example-co.test/job/123',
    careers_url='https://careers.example-co.test/', board='example-co', url=_GH_URL)


def _gh_dom(change=None, script=None):
    """Execute production JS in the same offline parent DOM as the entry tests."""
    import subprocess
    from pathlib import Path
    spec = dict(frames=['R'], visible=True, badge=True, url=_GH_URL)
    spec.update(change or {})
    result = subprocess.run(
        ['node', str(Path(__file__).with_name('entry_dom_fixture.js')),
         json.dumps(spec), script or forms.GREENHOUSE_PAGE],
        capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _gh_safe(observed, provenance=_GH_ALIAS):
    forms._greenhouse_safety(observed, lambda url: url == _GH_URL, provenance)


@pytest.mark.parametrize('origin', ['https://www.recaptcha.net', 'https://www.google.com'])
@pytest.mark.parametrize('reader', ['page', 'witness'])
def test_greenhouse_badge_exact_visible_origins(origin, reader):
    script = (forms.GREENHOUSE_PAGE if reader == 'page'
              else forms._GREENHOUSE_FIXED_CONTROL_WITNESS)
    observed = _gh_dom({'edit': {'src': origin + '/recaptcha/enterprise/anchor'}}, script)
    _gh_safe(observed)
    assert [f['status'] for f in observed['greenhouse']['frames']] == ['R']
    assert observed['greenhouse']['root']['selector'] == '#application-form'
    if reader == 'page':
        forms._inventory_ok(observed)
        assert observed['unsupported_frames'] == []
        assert not observed['opaque_controls'] and not observed['shadow_roots']


_GH_NEGATIVES = [
    {'edit': {'src': origin + '/recaptcha/enterprise/anchor'}}
    for origin in ['http://www.recaptcha.net', 'https://recaptcha.net',
                   'https://www.recaptcha.net:444', 'https://www.recaptcha.net.example-co.test']
 ] + [
    {'edit': {'src': 'https://www.recaptcha.net' + path}} for path in [
        '/prefix/recaptcha/enterprise/anchor', '/recaptcha/enterprise/anchor/suffix',
        '/recaptcha/enterprise/anchor/', '/recaptcha/api2/anchor']
 ] + [
    {'badge': False}, {'inside': True}, {'root': False}, {'ambiguous': True},
    {'marker': 'absent'}, {'bounds': {'width': 300, 'height': 60}},
    {'foreignMarker': True},
    {'bounds': {'width': 256, 'height': 61}}, {'bounds': {'width': 255, 'height': 60}},
    {'bounds': {'width': 256, 'height': 59}}, {'bounds': {'width': 256.5, 'height': 60}},
    {'bounds': 'missing'}, {'bounds': 'nan'}, {'bounds': 'infinity'},
    {'edit': {'style': {'visibility': 'collapse', 'display': 'block'}}},
    {'edit': {'style': {'visibility': 'visible', 'display': 'none'}}},
    {'sibling': True},
 ]


@pytest.mark.parametrize('change', _GH_NEGATIVES)
@pytest.mark.parametrize('reader', ['page', 'witness'])
def test_greenhouse_badge_isolated_visible_negatives(change, reader):
    script = (forms.GREENHOUSE_PAGE if reader == 'page'
              else forms._GREENHOUSE_FIXED_CONTROL_WITNESS)
    with pytest.raises(forms.Gate, match='unknown/untrusted application frame'):
        _gh_safe(_gh_dom(change, script))


@pytest.mark.parametrize('placement', [{}, {'inside': True}, {'root': False}])
def test_greenhouse_visible_bframe_precedes_root_and_unknown(placement):
    for frames in [['B'], ['R', 'B']]:
        with pytest.raises(forms.Gate, match='visible reCAPTCHA Enterprise challenge'):
            _gh_safe(_gh_dom(dict(placement, frames=frames, badge=False)))


@pytest.mark.parametrize('change', [
    {'visible': False}, {'edit': {'rect': None}},
    {'edit': {'style': {'visibility': 'hidden'}}},
    {'edit': {'rect': None, 'style': {'display': 'none'}}},
 ])
@pytest.mark.parametrize('shape', [{}, {'root': False}, {'inside': True},
                                  {'edit': {'src': 'https://untrusted.example-co.test/frame'}}])
def test_greenhouse_nonvisible_parity(change, shape):
    from tools.browser_skill_dom import PAGE
    spec = {**shape, **change, 'edit': {**shape.get('edit', {}), **change.get('edit', {})}}
    for kind in ['R', 'B'] :
        spec['frames'] = [kind]
        ordinary = _gh_dom(spec, PAGE)
        scoped = _gh_dom(spec)
        assert ordinary['unsupported_frames'] == scoped['unsupported_frames'] == []
        assert scoped['greenhouse']['frames'] == []
        if scoped['greenhouse']['root'] is None:
            # No frame exemption can authorize an action-time null root. Only
            # the bounded waiter may defer this root-dependent check.
            with pytest.raises(forms.Gate, match='unknown/untrusted application frame'):
                _gh_safe(scoped)
            forms._greenhouse_safety(scoped, lambda url: url == _GH_URL, _GH_ALIAS, readiness=True)
        else:
            _gh_safe(scoped)


def test_greenhouse_ordinary_reader_never_trusts_badge():
    from tools.browser_skill_dom import PAGE
    observed = _gh_dom(script=PAGE)
    assert 'greenhouse' not in observed and observed['unsupported_frames']
    with pytest.raises(forms.Gate, match='incomplete|uninspectable'):
        forms.check_form(observed, {}, uploaded=True)


@pytest.mark.parametrize('provenance', [None, object(), forms.VendorRedirectProvenance(
    'example-co#123', 'example-co', 'workday', '123',
    'https://careers.example-co.test/job/123', _GH_URL)])
def test_greenhouse_badge_typed_provenance_guard(provenance):
    with pytest.raises(forms.Gate, match='typed same-job provenance'):
        _gh_safe(_gh_dom(), provenance)


@pytest.mark.parametrize('change', _GH_NEGATIVES + [{'frames': ['B']}])
async def test_greenhouse_every_fresh_reader_gates_before_mutation(change):
    good, bad = _gh_dom(), _gh_dom(change)
    class Raw:
        def __init__(self):
            self.reads = 0
            self.mutations = []
        async def call(self, *args, **kwargs):
            if args[0] == 'evaluate':
                self.reads += 1
                return {'value': good if self.reads == 1 else bad}
            self.mutations.append(args)
    for later in ['page', 'witness']:
        raw = Raw()
        session = forms._GreenhouseSession(raw, lambda url: url == _GH_URL, _GH_ALIAS)
        assert await session.page() == good
        with pytest.raises(forms.Gate):
            if later == 'page':
                await session.page()
            else:
                await session.call('evaluate', forms._FIXED_CONTROL_WITNESS)
            await session.call('fill', '#email', '--value', 'test@example-co.test')
        assert not raw.mutations


async def test_greenhouse_current_alias_rechecked_on_later_witness():
    current = True
    raw = SimpleNamespace(call=AsyncMock(return_value={'value': _gh_dom()}))
    session = forms._GreenhouseSession(raw, lambda url: current and url == _GH_URL, _GH_ALIAS)
    await session.page()
    current = False
    with pytest.raises(forms.Gate, match='current row/config changed'):
        await session.call('evaluate', forms._FIXED_CONTROL_WITNESS)


async def test_greenhouse_independent_witness_and_repage_are_scoped(monkeypatch):
    monkeypatch.setattr(forms, '_observe_controls', _REAL_OBSERVE_CONTROLS)
    calls = []
    class Raw:
        async def call(self, *args):
            calls.append(args)
            return {'value': _gh_dom(script=args[1])}
    session = forms._GreenhouseSession(Raw(), lambda url: url == _GH_URL, _GH_ALIAS)
    initial = await session.page()
    current, witness = await forms._observe_controls(session, initial)
    assert current == initial and '#email' in witness
    assert calls == [('evaluate', forms.GREENHOUSE_PAGE),
                     ('evaluate', forms._GREENHOUSE_FIXED_CONTROL_WITNESS),
                     ('evaluate', forms.GREENHOUSE_PAGE)]


@pytest.mark.parametrize('key,value', [('opaque_controls', True), ('shadow_roots', True),
                                     ('truncated', True), ('inventory_verified', False)])
def test_greenhouse_badge_does_not_relax_adjacent_inventory_guards(key, value):
    observed = _gh_dom()
    observed[key] = value
    _gh_safe(observed)
    with pytest.raises(forms.Gate, match='incomplete|uninspectable'):
        forms.check_form(observed, {}, uploaded=True)


@pytest.mark.parametrize('change,old,new', [
    ({'edit': {'src': 'https://wrong.example-co.test/recaptcha/enterprise/anchor'}},
     "const provider=['https://www.recaptcha.net','https://www.google.com'].includes(origin)",
     'const provider=true'),
    ({'edit': {'src': 'https://www.recaptcha.net/recaptcha/api2/anchor'}},
     "path==='/recaptcha/enterprise/anchor' &&", 'true &&'),
    ({'badge': False}, 'badge && !!bounds', 'true && !!bounds'),
    ({'inside': True}, 'shown && outside &&', 'shown && true &&'),
    ({'ambiguous': True}, 'forms.length===1 ?', 'forms.length>=1 ?'),
    ({'edit': {'style': {'visibility': 'collapse', 'display': 'block'}}},
     "style.visibility==='visible' && style.display!=='none'", "style.display!=='none'"),
    ({'bounds': {'width': 300, 'height': 60}}, 'bounds.width===256 &&', 'true &&'),
    ({'bounds': {'width': 256, 'height': 61}}, 'bounds.height===60', 'true'),
 ])
def test_greenhouse_guard_removal_sensitivity(change, old, new):
    # Run the *same* negative assertion with exactly one production conjunct
    # removed in-memory, never changing or committing weakened production code.
    original = forms.GREENHOUSE_PAGE
    assert old in original
    def negative(script):
        with pytest.raises(forms.Gate, match='unknown/untrusted application frame'):
            _gh_safe(_gh_dom(change, script))
    negative(original)
    with pytest.raises(pytest.fail.Exception, match='DID NOT RAISE'):
        negative(original.replace(old, new))
    negative(original)


def _proxy_dom(change=None, *, page_script=None, witness_script=None):
    import subprocess
    from pathlib import Path
    spec = dict(fullPhone=True, requiredProxy=True, proxyValueAttribute=True)
    spec.update(change or {})
    return json.loads(subprocess.check_output([
        'node', str(Path(__file__).with_name('closed_widget_dom.cjs')),
        json.dumps(spec), page_script or bsk.PAGE,
        witness_script or forms._FIXED_CONTROL_WITNESS], text=True))


@pytest.mark.parametrize('value_attribute', [False, True])
def test_greenhouse_proxy_exact_four_or_five_names_in_both_readers(value_attribute):
    result = _proxy_dom({'proxyValueAttribute': value_attribute})
    # ARCH21:18 / #139 binding 6/9: a React-select proxy fingerprint is no
    # authority. Missing accessible linkage remains opaque at final Submit.
    with pytest.raises(forms.Gate, match='inventory is incomplete'):
        forms.check_form(result['page'], {}, uploaded=True)
    for reader in result.values():
        for key in ['location', 'country']:
            target = next(c for c in reader['controls'] if c['selector'] == '#' + key)
            assert target['combobox']['linked'] is False
            assert not target['combobox']['link_id']
            assert 'mode' not in target['combobox']
    # The same library can expose real rendered ARIA relationships. Generic
    # option observation must work regardless of the hidden proxy's attributes.
    opened = _proxy_dom({'proxyValueAttribute': value_attribute,
                         'phases': {'location': 'C1', 'country': 'C1'}})
    for reader in opened.values():
        for key in ['location', 'country']:
            target = next(c for c in reader['controls'] if c['selector'] == '#' + key)
            assert target['combobox']['linked'] is True
            assert target['combobox']['linked'] is True
            assert 'mode' not in target['combobox']


@pytest.mark.parametrize('mutation', [
    {'attr': ['value', 'nonempty']},
    {'attr': ['data-extra', '']},
    {'attr': ['required', None]},
    {'value': 'nonempty'},
 ])
@pytest.mark.parametrize('key', ['location', 'country'])
def test_greenhouse_proxy_five_name_isolated_negatives(mutation, key):
    result = _proxy_dom({'proxyMutation': {'key': key, **mutation}})
    with pytest.raises(forms.Gate, match='inventory is incomplete'):
        forms.check_form(result['page'], {}, uploaded=True)
    for reader in result.values():
        target = next(c for c in reader['controls'] if c['selector'] == '#' + key)
        # Binding 6/9: changing a proxy cannot manufacture an ARIA link or a
        # privileged phase certificate. Keep the opaque/no-authorization proof.
        assert target['combobox']['linked'] is False
        assert not target['combobox']['link_id']
        assert target['combobox']['options'] == []
        assert 'mode' not in target['combobox']


def test_greenhouse_proxy_live_shape_positive_detects_old_reject():
    old = ("(names.join(',')==='aria-hidden,class,required,tabindex' ||\n"
           "        (names.join(',')==='aria-hidden,class,required,tabindex,value' &&\n"
           "         p.getAttribute('value')===''))")
    # AC6/AC9/AC12 deliberately remove this fingerprint. Preserve the mutation
    # sensitivity test for the replacement generic accessible relationship.
    assert old not in bsk.PAGE and old not in forms._FIXED_CONTROL_WITNESS
    def positive(page_script, witness_script):
        result = _proxy_dom({'phases': {'location': 'C1', 'country': 'C1'}},
                            page_script=page_script, witness_script=witness_script)
        for reader in result.values():
            target = next(c for c in reader['controls'] if c['selector'] == '#location')
            assert target['combobox']['linked'] is True
            assert target['combobox']['linked'] is True
    positive(bsk.PAGE, forms._FIXED_CONTROL_WITNESS)
    marker = 'const linkId = e => {'
    assert marker in bsk.PAGE and marker in forms._FIXED_CONTROL_WITNESS
    with pytest.raises(AssertionError):
        positive(bsk.PAGE.replace(marker, marker + " return '';"),
                 forms._FIXED_CONTROL_WITNESS.replace(marker, marker + " return '';"))
    positive(bsk.PAGE, forms._FIXED_CONTROL_WITNESS)


@pytest.mark.parametrize('change', _GH_NEGATIVES + [
    {'frames': ['B']}, {'action': 'https://foreign.example-co.test/submit'}])
async def test_greenhouse_real_prefill_observation_refuses_later_frame_change(change, monkeypatch):
    # A drifted frame must not recreate the removed per-action witness Gate.
    # Initial admission and the independent final-Submit predicate have their
    # own tests; this checks only ordinary ungated preparation.
    monkeypatch.setattr(forms, '_observe_controls', _REAL_OBSERVE_CONTROLS)
    monkeypatch.setattr(forms, '_alias_current', lambda *a, **k: True)
    tracked = {'pk': _GH_ALIAS.pk, 'company': _GH_ALIAS.company,
               'job_id': _GH_ALIAS.job_id, 'jd_url': _GH_ALIAS.wrapper,
               'status': 'submitting', 'ats': 'greenhouse'}
    monkeypatch.setattr(forms, '_row',
                        lambda pk, *args, **kwargs: tracked if pk == _GH_ALIAS.pk else {})
    initial = _gh_dom()
    class Raw:
        def __init__(self):
            self.mutations = []
        async def call(self, *args, **kwargs):
            if args[0] == 'evaluate':
                spec = {} if args[1] == forms._GREENHOUSE_FIXED_CONTROL_WITNESS else change
                return {'value': _gh_dom(spec, args[1])}
            self.mutations.append(args)
    raw = Raw()
    session = forms._GreenhouseSession(raw, lambda url: url == _GH_URL, _GH_ALIAS)
    await forms.execute(session, initial, {'action': 'fill', 'selector': '#email', 'fact': 'Email'},
                        facts={'Email': 'test@example-co.test'}, filled={}, resume_path='',
                        company=_GH_ALIAS.company, jd_text='', resume_tex='', github='',
                        pk=_GH_ALIAS.pk, job_url=_GH_ALIAS.wrapper, provenance=_GH_ALIAS)
    assert raw.mutations == [('fill', '#email', '--value', 'test@example-co.test')]

@pytest.mark.parametrize('reader', ['page', 'witness'])
def test_greenhouse_bframe_priority_removal_detected(reader):
    original = forms.GREENHOUSE_PAGE if reader == 'page' else forms._GREENHOUSE_FIXED_CONTROL_WITNESS
    clause = "if (provider && path==='/recaptcha/enterprise/bframe' && shown) return 'visible-recaptcha';"
    assert clause in original
    def challenge(script):
        with pytest.raises(forms.Gate, match='visible reCAPTCHA Enterprise challenge'):
            _gh_safe(_gh_dom({'frames': ['B'], 'root': False}, script))
    challenge(original)
    with pytest.raises(AssertionError, match='Regex pattern did not match'):
        challenge(original.replace(clause, ''))
    challenge(original)


def test_greenhouse_typed_guard_removal_detected():
    import inspect
    source = inspect.getsource(forms._greenhouse_safety)
    guard = ("    if not isinstance(provenance, GreenhouseJobAlias):\n"
             "        raise Gate('Greenhouse frame observation lacks typed same-job provenance.')\n")
    assert guard in source
    scope = dict(vars(forms))
    # Destination validation is now unconditional even without frames. Isolate
    # this guard-removal experiment from that independent mandatory check.
    scope['check_form_destination'] = lambda *args, **kwargs: None
    exec(source.replace(guard, ''), scope)
    observed = _gh_dom({'frames': []})
    def negative(check):
        with pytest.raises(forms.Gate, match='typed same-job provenance'):
            check(observed, lambda url: url == _GH_URL, None)
    negative(forms._greenhouse_safety)
    with pytest.raises(pytest.fail.Exception, match='DID NOT RAISE'):
        negative(scope['_greenhouse_safety'])
    negative(forms._greenhouse_safety)


async def test_greenhouse_freshness_guard_removal_detected(monkeypatch):
    good, bad = _gh_dom(), _gh_dom({'bounds': {'width': 300, 'height': 60}})
    raw = SimpleNamespace(call=AsyncMock(side_effect=[{'value': good}, {'value': bad}]))
    session = forms._GreenhouseSession(raw, lambda url: url == _GH_URL, _GH_ALIAS)
    await session.page()
    async def negative():
        with pytest.raises(forms.Gate, match='unknown/untrusted application frame'):
            await session.page()
    await negative()
    async def cached_page():
        return good
    monkeypatch.setattr(session, 'page', cached_page)
    with pytest.raises(pytest.fail.Exception, match='DID NOT RAISE'):
        await negative()


@pytest.mark.parametrize('reader', ['page', 'witness'])
@pytest.mark.parametrize('textarea', [False, True])
@pytest.mark.parametrize('placement', [{}, {'tokenOutside': True},
                                      {'tokenOutside': True, 'tokenAssociated': True}])
@pytest.mark.parametrize('name', ['g-recaptcha-response', 'h-captcha-response'])
def test_greenhouse_response_receiver_gates_before_any_value_read(reader, textarea, placement, name):
    # The getter throws even for a hidden/form-associated node: a later Python
    # inventory Gate is too late to undo a token read in PAGE or the shared graph.
    script = forms.GREENHOUSE_PAGE if reader == 'page' else forms._GREENHOUSE_FIXED_CONTROL_WITNESS
    observed = _gh_dom(dict(placement, textarea=textarea, tokens=[name]), script)
    assert observed['greenhouse']['response_controls'] is True
    assert 'controls' not in observed
    with pytest.raises(forms.Gate, match='CAPTCHA response control'):
        _gh_safe(observed)


@pytest.mark.parametrize('reader', ['page', 'witness'])
def test_greenhouse_response_guard_preserves_visible_bframe_priority(reader):
    script = forms.GREENHOUSE_PAGE if reader == 'page' else forms._GREENHOUSE_FIXED_CONTROL_WITNESS
    observed = _gh_dom({'tokens': ['g-recaptcha-response'], 'textarea': True, 'frames': ['B']}, script)
    with pytest.raises(forms.Gate, match='visible reCAPTCHA Enterprise challenge'):
        _gh_safe(observed)


async def test_greenhouse_response_pre_read_gate_never_reaches_model(monkeypatch, tmp_path):
    from tests.tools.test_browser_bootstrap import _greenhouse_session, GH_WRAP, GH_DIRECT, GH_PK
    browser, decide, _ = _greenhouse_session(monkeypatch, tmp_path)
    original = browser.call
    async def read(*args, **kwargs):
        if args[0] == 'evaluate':
            browser.calls.append(args)
            return {'value': _gh_dom({'url': GH_DIRECT, 'tokens': ['g-recaptcha-response'],
                                     'textarea': True}, args[1])}
        return await original(*args, **kwargs)
    monkeypatch.setattr(browser, 'call', read)
    outcome = await forms.apply(GH_WRAP, 'example-co', {}, 'synthetic', pk=GH_PK,
                                resume_path='Resume.pdf')
    assert outcome['status'] == 'gate'
    assert 'CAPTCHA response control' in outcome['question']
    assert outcome.get('form_question') is None
    assert not decide.await_count
    assert not any(c[0] in {'fill', 'upload', 'select', 'click'} for c in browser.calls)


@pytest.mark.parametrize('reader', ['page', 'witness'])
def test_greenhouse_response_pre_read_guard_removal_is_red(reader):
    import subprocess
    source = forms.GREENHOUSE_PAGE if reader == 'page' else forms._GREENHOUSE_FIXED_CONTROL_WITNESS
    guard = 'if (greenhouse.response_controls) return {url:location.href, greenhouse};'
    assert guard in source
    spec = {'tokens': ['g-recaptcha-response'], 'textarea': False, 'claim': True}
    def protected(script):
        result = _gh_dom(spec, script)
        assert 'controls' not in result
        with pytest.raises(forms.Gate, match='CAPTCHA response control'):
            _gh_safe(result)
    protected(source)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        protected(source.replace(guard, ''))
    assert 'PROTECTED TOKEN VALUE READ' in caught.value.stderr
    protected(source)


def test_greenhouse_response_controller_guard_removal_is_red():
    import inspect
    source = inspect.getsource(forms._greenhouse_safety)
    guard = ("    if observed.get('response_controls'):\n"
             "        raise Gate('CAPTCHA response control requires manual inspection; no values read.')\n")
    assert guard in source
    scope = dict(vars(forms))
    exec(source.replace(guard, ''), scope)
    observed = _gh_dom({'tokens': ['g-recaptcha-response'], 'textarea': True, 'frames': []})
    def protected(check):
        with pytest.raises(forms.Gate, match='CAPTCHA response control'):
            check(observed, lambda url: url == _GH_URL, _GH_ALIAS)
    protected(forms._greenhouse_safety)
    with pytest.raises(pytest.fail.Exception, match='DID NOT RAISE'):
        protected(scope['_greenhouse_safety'])
    protected(forms._greenhouse_safety)


def _badge_response(change=None, script=None):
    spec = dict(tokenBadge=True, textarea=True, tokens=['g-recaptcha-response'])
    spec.update(change or {})
    return _gh_dom(spec, script)


@pytest.mark.parametrize('reader', ['page', 'witness'])
def test_greenhouse_exact_badge_textarea_is_never_read_or_exported(reader):
    script = forms.GREENHOUSE_PAGE if reader == 'page' else forms._GREENHOUSE_FIXED_CONTROL_WITNESS
    assert 'nodes.filter(e => !excluded.has(e) &&' in forms.GREENHOUSE_PAGE, 'PAGE exclusion rewrite drifted'
    assert 'Array.from(document.querySelectorAll(query)).filter(e => !excluded.has(e) &&' in forms._GREENHOUSE_FIXED_CONTROL_WITNESS, 'witness exclusion rewrite drifted'
    assert "Array.from(document.querySelectorAll('[role=\"combobox\"]')).filter(e=>!excluded.has(e))" in script, 'graph exclusion rewrite drifted'
    observed = _badge_response(script=script)
    _gh_safe(observed)
    assert observed['greenhouse']['response_controls'] is False
    assert observed['greenhouse']['badge_responses'] == 1
    assert all(c['selector'] != '#g-recaptcha-response-100' for c in observed['controls'])
    if reader == 'page':
        forms._inventory_ok(observed)
        assert observed['unsupported_frames'] == []
    # The prior blanket response Gate rejects this exact realistic specimen.
    blanket = 'greenhouse.response_controls=responseNodes.length>0;'
    old = 'greenhouse.response_controls=safeResponses.length!==responseNodes.length;'
    assert old in script
    with pytest.raises(forms.Gate, match='CAPTCHA response control'):
        _gh_safe(_badge_response(script=script.replace(old, blanket)))
    _gh_safe(_badge_response(script=script))


_BADGE_RESPONSE_NEGATIVES = [
    {'tokenInForm': True}, {'tokenNotInBadge': True}, {'tokenAssociated': True},
    {'frames': []}, {'badge': False}, {'tokenSiblingBadge': True},
    {'tokenVisible': True}, {'tokenEdit': {'style': {'display': 'block'}}},
    {'textarea': False}, {'tokens': ['h-captcha-response']},
    {'tokens': ['g-recaptcha-response', 'g-recaptcha-response']},
    {'tokens': ['g-recaptcha-response', 'h-captcha-response']},
    {'tokenEdit': {'id': 'synthetic-response'}},
    {'tokenEdit': {'class': 'g-recaptcha-response extra'}},
    {'tokenEdit': {'value': ''}}, {'tokenEdit': {'value': 'synthetic-nonempty'}},
    {'tokenEdit': {'data-extra': ''}}, {'badgeWrapper': {'id': 'synthetic-wrapper'}},
    {'badgeWrapper': {'class': ''}}, {'tokenNestedWrapper': True}, {'claim': True},
 ]


@pytest.mark.parametrize('reader', ['page', 'witness'])
@pytest.mark.parametrize('change', _BADGE_RESPONSE_NEGATIVES)
def test_greenhouse_badge_response_isolated_shape_refusals(change, reader):
    script = forms.GREENHOUSE_PAGE if reader == 'page' else forms._GREENHOUSE_FIXED_CONTROL_WITNESS
    with pytest.raises(forms.Gate, match='CAPTCHA response control'):
        _gh_safe(_badge_response(change, script))


@pytest.mark.parametrize('reader', ['page', 'witness'])
@pytest.mark.parametrize('change,guard', [
    ({'textarea': False}, "e.tagName==='TEXTAREA' &&"),
    ({'tokens': ['h-captcha-response']}, "e.getAttribute('name')==='g-recaptcha-response' &&"),
    ({'tokens': ['g-recaptcha-response', 'g-recaptcha-response']}, 'responseNodes.length===1 &&'),
    ({'tokenEdit': {'id': 'synthetic-response'}},
     "/^g-recaptcha-response-\\d+$/.test(e.getAttribute('id') || '') &&"),
    ({'tokenEdit': {'class': 'g-recaptcha-response extra'}},
     "e.getAttribute('class')==='g-recaptcha-response' &&"),
    ({'tokenEdit': {'data-extra': ''}}, "names==='class,id,name,style' &&"),
    ({'tokenAssociated': True}, '!e.form &&'),
    ({'tokenVisible': True}, '      !e.getClientRects().length &&'),
    ({'tokenEdit': {'style': {'display': 'block'}}}, "style.display==='none' &&"),
    ({'tokenSiblingBadge': True}, 'badges.length===1 &&'),
    ({'badgeWrapper': {'id': 'synthetic-wrapper'}}, "!wrapper.hasAttribute('id') &&"),
    ({'badgeWrapper': {'class': ''}}, "!wrapper.hasAttribute('class') &&"),
    ({'tokenNestedWrapper': True}, 'wrapper.parentElement===document.body &&'),
 ])
def test_greenhouse_badge_response_new_guard_removal_is_red(change, guard, reader):
    source = forms.GREENHOUSE_PAGE if reader == 'page' else forms._GREENHOUSE_FIXED_CONTROL_WITNESS
    assert guard in source
    def negative(script):
        with pytest.raises(forms.Gate, match='CAPTCHA response control'):
            _gh_safe(_badge_response(change, script))
    negative(source)
    with pytest.raises(pytest.fail.Exception, match='DID NOT RAISE'):
        negative(source.replace(guard, 'true &&'))
    negative(source)


@pytest.mark.parametrize('reader', ['page', 'witness'])
def test_greenhouse_badge_response_requires_admitted_anchor_on_every_read(reader):
    source = forms.GREENHOUSE_PAGE if reader == 'page' else forms._GREENHOUSE_FIXED_CONTROL_WITNESS
    guard = "greenhouseFrames.some((f,i)=>badge.contains(f) && greenhouse.frames[i].status==='R')"
    assert guard in source
    observed = _badge_response({'frames': []}, source)
    with pytest.raises(forms.Gate, match='CAPTCHA response control'):
        _gh_safe(observed)
    with pytest.raises(pytest.fail.Exception, match='DID NOT RAISE'):
        with pytest.raises(forms.Gate, match='CAPTCHA response control'):
            _gh_safe(_badge_response({'frames': []}, source.replace(guard, 'true')))
    with pytest.raises(forms.Gate, match='CAPTCHA response control'):
        _gh_safe(_badge_response({'frames': []}, source))


@pytest.mark.parametrize('reader', ['page', 'witness'])
async def test_greenhouse_later_badge_textarea_change_gates_before_next_mutation(reader):
    source = forms.GREENHOUSE_PAGE if reader == 'page' else forms._GREENHOUSE_FIXED_CONTROL_WITNESS
    good, bad = _badge_response(script=source), _badge_response({'tokenNotInBadge': True}, source)
    raw = SimpleNamespace(call=AsyncMock(side_effect=[{'value': good}, {'value': bad}]))
    session = forms._GreenhouseSession(raw, lambda u: u == _GH_URL, _GH_ALIAS)
    await session.page()
    with pytest.raises(forms.Gate, match='CAPTCHA response control'):
        if reader == 'page':
            await session.page()
        else:
            await session.call('evaluate', forms._FIXED_CONTROL_WITNESS)
        await session.call('fill', '#email', '--value', 'test@example-co.test')
    assert all(c.args[0] == 'evaluate' for c in raw.call.await_args_list)


async def test_greenhouse_exact_badge_textarea_reaches_guarded_decision(monkeypatch, tmp_path):
    from tests.tools.test_browser_bootstrap import _greenhouse_session, GH_WRAP, GH_DIRECT, GH_PK
    browser, decide, _ = _greenhouse_session(monkeypatch, tmp_path)
    original = browser.call
    async def read(*args, **kwargs):
        if args[0] == 'evaluate':
            browser.calls.append(args)
            return {'value': _badge_response({'url': GH_DIRECT}, args[1])}
        return await original(*args, **kwargs)
    monkeypatch.setattr(browser, 'call', read)
    outcome = await forms.apply(GH_WRAP, 'example-co', {}, 'synthetic', pk=GH_PK,
                                resume_path='Resume.pdf')
    assert decide.await_count == 1  # A response-presence blanket Gate fails here.
    assert outcome['status'] == 'gate'
    assert not any(c[0] in {'fill', 'upload', 'select', 'click'} for c in browser.calls)
    assert 'g-recaptcha-response' not in json.dumps(decide.await_args.args[1])


def test_diagnostic_gate_allowlist_never_infers_from_model_text():
    assert forms.Gate('owner_question; defect_structural; captcha').reason == 'unknown_field'
    assert forms.Gate('random', reason='not-a-code').reason == 'unknown_field'
    assert forms.Gate('verified', reason='defect_provenance').reason == 'defect_provenance'
    assert forms.CaptchaGate('visible challenge').form_question is None
    # A code-observed CAPTCHA stays a human handoff, never model-text inference.
    assert forms.CaptchaGate('visible challenge').reason == 'captcha'


def test_required_unanswered_field_is_refused_at_final_form_readback():
    current = page(
        target(label='Location', required=True),
        target(selector='#resume', type='file', label='Resume',
               files=['Resume.pdf']),
        application_marker=True)
    assert current['controls'][0]['label'] == 'Location'
    assert current['controls'][0]['selector'] == '#field'
    # Ungated preparation does not turn a required empty input into proof.
    with pytest.raises(forms.Gate, match='required application field is empty'):
        forms.check_form(current, {}, uploaded=True, attachment='Resume.pdf')


def test_prior_answer_bank_cannot_replace_current_required_field_readback():
    current = page(
        target(label='Location', required=True),
        target(selector='#resume', type='file', label='Resume',
               files=['Resume.pdf']),
        application_marker=True)
    assert not current['controls'][0]['has_value']
    # A stale model fact/receipt can be present while the form remains empty.
    filled = {'#field': 'Synthetic Place'}
    with pytest.raises(forms.Gate, match='required application field is empty'):
        forms.check_form(current, filled, uploaded=True, attachment='Resume.pdf')


@pytest.mark.parametrize('required', [True, False])
async def test_execute_missing_fact_requires_current_observed_unanswered_question(
        monkeypatch, required):
    current = page(target(label='Location', required=required), application_marker=True)
    # Keep this witness test offline; _row otherwise reads the configured tracking store.
    monkeypatch.setattr(forms, '_row', lambda *_args, **_kwargs: {
        'pk': 'example-co#synthetic', 'human_approved_answers': {}})
    session = SimpleNamespace(call=AsyncMock())
    with pytest.raises(forms.Gate) as gated:
        await forms.execute(session, current,
                            {'action': 'fill', 'selector': '#field', 'fact': ''},
                            facts={}, filled={}, resume_path='',
                            company='example-co', jd_text='', resume_tex='', github='',
                            pk='example-co#synthetic', job_url=current['url'])
    # Missing facts are ordinary owner questions, not per-control grants.
    assert gated.value.reason == 'owner_question'
    assert gated.value.form_question['selector'] == '#field'
    session.call.assert_not_awaited()


def test_missing_independent_visible_control_witness_is_defect_not_owner_answer():
    current = page(target(label='Location', required=True), application_marker=True)
    # The retired per-control witness is not a preparation permission.
    # A missing fact still requires the ordinary owner-answer path.
    question = forms._ordinary_owner_question(
        current, current['controls'][0], 'example-co#synthetic')
    assert question['selector'] == '#field'
    assert question['control_label'] == 'Location'


@pytest.mark.parametrize('error', [
    'ambiguous application roots',
    'application root sibling identity mismatch',
    'unsupported synthetic root evidence',
])
def test_ashby_root_reader_errors_still_refuse_final_container(error):
    job_id = '12345678-1234-1234-1234-123456789012'
    pk = 'example-co#' + job_id
    posting = 'https://jobs.ashbyhq.com/example-co/' + job_id
    provenance = forms.ApplicationEntryProvenance(
        pk=pk, company='example-co', ats='ashby', job_id=job_id,
        wrapper=posting, board='example-co', url=posting+'/application')
    observed = {'url': provenance.url,
                'entry': {'formless': {'root': '#application', 'error': error}}}
    # Exact pre-step defect taxonomy belonged to removed widget gates;
    # malformed final form identity must still refuse the committing boundary.
    with pytest.raises(forms.Gate, match='final Ashby application container is unavailable'):
        forms._formless_boundary(observed, provenance)


def test_greenhouse_missing_board_does_not_mint_final_tenant_proof(monkeypatch, tmp_path):
    from core.ids import make_pk

    pk = make_pk('example-co', '101')
    url = 'https://careers.example.test/jobs/role?gh_jid=101'
    row = {'pk': pk, 'company': 'example-co', 'job_id': '101',
           'ats': 'greenhouse', 'jd_url': url}

    # Exercise a deployed synthetic tuple instead of a mutable resolver hint.
    def deploy(*, board='verified-board', careers='https://careers.example.test/jobs'):
        (tmp_path / 'watchlist.yaml').write_text(
            'companies:\n'
            '  - name: example-co\n'
            f'    careers_url: {careers}\n'
            '    ats: greenhouse\n'
            f'    board: {board}\n'
            '    discovery: feed\n'
            '    mode: gated\n')

    deploy()
    monkeypatch.setattr(forms, '_row', lambda *_args, **_kwargs: row)
    monkeypatch.setattr('core.config.get_settings',
                        lambda: SimpleNamespace(config_dir=tmp_path))
    assert forms._configured_alias(pk, 'example-co', url) is None
    row['greenhouse_board'] = 'verified-board'
    proof = forms._configured_alias(pk, 'example-co', url)
    assert isinstance(proof, forms.GreenhouseJobAlias)
    assert proof.url == ('https://job-boards.greenhouse.io/embed/job_app'
                         '?for=verified-board&token=101')
    assert forms._alias_current(proof)
    deploy(board='')
    assert forms._configured_alias(pk, 'example-co', url) is None
    assert not forms._alias_current(proof)
    deploy(careers='https://different.example.test/jobs')
    assert forms._configured_alias(pk, 'example-co', url) is None
    assert forms._configured_alias(pk, 'wrong-company', url) is None


@pytest.mark.parametrize('pin', [None, 'stale-board'], ids=['absent-pin', 'stale-pin'])
async def test_greenhouse_missing_or_stale_board_prepares_but_cannot_submit(
        monkeypatch, tmp_path, pin):
    url = 'https://careers.example.test/jobs?gh_jid=101'
    row = dict(pk='example-co#101', company='example-co', job_id='101',
               jd_url=url, ats='greenhouse', status='submitting')
    if pin is not None:
        row['greenhouse_board'] = pin
    config = SimpleNamespace(name='example-co', ats='greenhouse',
                             board='verified-board',
                             careers_url='https://careers.example.test/')
    monkeypatch.setattr('core.config.get_settings',
                        lambda: SimpleNamespace(config_dir=tmp_path))
    monkeypatch.setattr('discovery.watchlist.load_watchlist', lambda *_: [config])
    current = {**page(), 'url': url}
    navigations = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def navigate(self, destination):
            navigations.append(destination)
            return current

        async def page(self):
            return current

        async def call(self, *args, **kwargs):
            pytest.fail('Unproved final tenant must never submit or write private data')

    monkeypatch.setattr(forms, 'check_dispatch', lambda *a, **k: None)
    monkeypatch.setattr(forms, '_row', lambda *a, **k: row)
    assert forms._configured_alias(row['pk'], 'example-co', url) is None
    monkeypatch.setattr('tools.claude_chrome._stage_resume', lambda path, owner: path)
    monkeypatch.setattr('tools.browser_apply._site_rules', lambda *a: '')
    monkeypatch.setattr(bsk, 'Session', lambda *_: Session())
    decide = AsyncMock(return_value={
        'action': 'gate', 'question': 'Stop after synthetic preparation observation'})
    monkeypatch.setattr(bsk, 'decision', decide)
    result = await forms.apply(url, 'example-co', {}, 'synthetic',
                               pk=row['pk'], resume_path='Resume.pdf')
    assert result['status'] == 'gate'
    # ARCH21:58 / AC14: missing or stale pins do not gate approved-host
    # reversible navigation; absence is examined by the one final guard.
    assert navigations == [url]
    assert decide.await_count == 1
    guard = forms.ApplicationIPCController(
        row['pk'], 'example-co', url, '', {}, {}, [], {}, lambda: None)
    with pytest.raises(forms.PreSubmitGate) as refusal:
        await guard.check_pre_submit(
            Session(), current,
            {'label': 'Submit application', 'selector': '#submit', 'in_form': False})
    assert refusal.value.item == 1
    assert refusal.value.reason == 'defect_pre_submit_item_1'
    assert not guard.committed


async def test_missing_fact_uses_owner_question_without_prewrite_provenance_gate():
    target_url = 'https://jobs.ashbyhq.com/example-co/12345678-1234-1234-1234-123456789012/application'
    provenance = forms.ApplicationEntryProvenance(
        pk='example-co#synthetic', company='example-co', ats='ashby', job_id='synthetic',
        wrapper=target_url, board='example-co', url=target_url)
    session = SimpleNamespace(call=AsyncMock())
    with pytest.raises(forms.Gate) as gated:
        await forms.execute(session, page(target(required=True)),
                            {'action': 'fill', 'selector': '#field', 'fact': 'Location'},
                            facts={}, filled={}, resume_path='', company='example-co',
                            jd_text='', resume_tex='', github='', pk='example-co#wrong',
                            job_url=target_url, provenance=provenance)
    # Per-write provenance refusal was removed; a missing value is still
    # an ordinary unanswered owner question, not authorization to write.
    assert gated.value.reason == 'owner_question'
    assert gated.value.form_question is not None
    session.call.assert_not_awaited()


@pytest.mark.asyncio

async def test_unbound_model_gate_does_not_invent_owner_control_or_resumable_answer(
        monkeypatch, tmp_path):
    from tests.tools.test_browser_reversible_gates import (
        _apply, _fixture, _mark_census_answered)
    _, endpoint, decision, _, pdf = _fixture(monkeypatch, tmp_path)
    for snapshot in (endpoint.current, endpoint.fixed):
        for control in snapshot['controls']:
            if control['selector'] != '#additional':
                _mark_census_answered(control)
        for evidence in snapshot['entry']['root_evidence']:
            if evidence['selector'] != '#additional':
                evidence.update(value='Synthetic answer', checked=True)
    decision.side_effect = [
        {'action': 'wait'},
        {'action': 'gate', 'question': 'Synthetic unbound prose'}]
    result = await _apply(pdf)
    assert decision.await_count == 2
    # Only an observed selected field with missing facts takes the owner route.
    # Free model prose is inspection, not a synthetic per-control grant.
    assert result['reason'] == 'agent_review_unavailable'
    assert result['review_route'] == 'luna'
    assert result['form_question'] is None
    # Issue #314: a safe, bounded model reason is diagnosable in the row detail only.
    assert 'Model reason: Synthetic unbound prose' in result['question']




# ---- Issue #314: bounded one-retry for a no-effect `document_changed` read ----
# The conftest fixture swaps in a context-free `_run`; these tests need the real
# cooperation path, so they capture and restore it themselves.
_REAL_SESSION_RUN = bsk.Session._run
_REAL_SESSION_SYNC = bsk.Session.sync_context


def _refusal(*, code='permission_denied', reason='document_changed', effect='none',
             extra_reason=None, top_reason=None):
    data = {'reason': reason, 'recovery': 'sync'}
    if effect is not None:
        data['effect_state'] = effect
    payload = {'code': code, 'message': 'document changed', 'exit_code': 1, 'data': data}
    if top_reason is not None:
        payload['reason'] = top_reason
    if extra_reason is not None:
        payload['data']['code'] = extra_reason
    try:
        bsk._result(json.dumps(payload), 1)
    except bsk.Unavailable as exc:
        return exc
    raise AssertionError('refusal must raise')


class _CooperativeCli:
    """Fake CLI: single-use latest context; scripted outcomes for the shared-tab actions."""

    def __init__(self, outcomes):
        self.outcomes, self.calls, self.issued, self.latest = list(outcomes), [], 0, None

    async def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ('session', 'start'):
            return {'session_id': 's1', 'browser_instance_id': 'dedicated'}
        if args[:2] == ('session', 'stop'):
            return {'ok': True}
        if args[0] == 'cooperate':
            self.issued += 1
            self.latest = f'ctx-{self.issued}'
            return {'context_id': self.latest, 'kind': 'sync'}
        given = args[args.index('--context') + 1] if '--context' in args else None
        outcome = self.outcomes.pop(0) if self.outcomes else {'ok': True, 'value': {}}
        spent, self.latest = given == self.latest, None
        if isinstance(outcome, bsk.Unavailable):
            raise outcome
        assert spent or args[0] == 'navigate', 'stale context reached the CLI'
        return outcome

    def actions(self, verb=None):
        return [c for c in self.calls
                if c[0] not in {'session', 'cooperate'} and (verb is None or c[0] == verb)]

    def syncs(self):
        return [c for c in self.calls if c[0] == 'cooperate']


@pytest.fixture
def cooperative(monkeypatch):
    monkeypatch.setattr(bsk, 'available', lambda: (True, ''))
    monkeypatch.setattr('tools.browser_profile.ensure_browser', lambda: (True, ''))
    monkeypatch.setattr('tools.browser_runtime.configuration',
                        lambda *a, **k: {'browser': 'dedicated'})
    monkeypatch.setattr(bsk, '_SYNC_POLL', 0)
    monkeypatch.setattr(bsk.Session, '_run', _REAL_SESSION_RUN)
    monkeypatch.setattr(bsk.Session, 'sync_context', _REAL_SESSION_SYNC)
    cli = _CooperativeCli([{'ok': True}])  # first outcome is the opening navigation
    monkeypatch.setattr(bsk, 'command', cli)
    return cli


async def _opened(kind):
    session = bsk.Session(kind)
    await session.__aenter__()
    await session._run(('navigate', 'http://x.test/'), {})
    return session


def _reads(cli):
    return cli.actions('evaluate')


@pytest.mark.asyncio
async def test_apply_fixed_reader_syncs_and_retries_once_on_document_changed(cooperative):
    session = await _opened('apply')
    syncs_before = len(cooperative.syncs())
    cooperative.outcomes = [_refusal(), {'ok': True, 'value': {'url': 'http://x.test/'}}]
    result = await session.call('evaluate', bsk.PAGE)
    assert result['value'] == {'url': 'http://x.test/'}
    assert len(_reads(cooperative)) == 2
    assert len(cooperative.syncs()) == syncs_before + 1
    # Each attempt carried its own fresh single-use context.
    contexts = [c[c.index('--context') + 1] for c in _reads(cooperative)]
    assert len(set(contexts)) == 2
    await session.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_a_second_document_changed_refusal_is_final(cooperative):
    session = await _opened('apply')
    cooperative.outcomes = [_refusal(), _refusal(), {'ok': True}]
    with pytest.raises(bsk.Unavailable):
        await session.call('evaluate', bsk.PAGE)
    assert len(_reads(cooperative)) == 2
    await session.__aexit__(None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize('problem', [
    _refusal(effect='unknown'),
    _refusal(effect=None),
    _refusal(effect='applied'),
    _refusal(code='cdp_failed'),
    _refusal(reason='something_else'),
    _refusal(reason='document_changed', top_reason='other_reason'),
    _refusal(extra_reason='page_crashed'),
    _refusal(reason='Document Changed!'),
])
async def test_other_document_changed_shapes_get_no_new_retry(cooperative, problem):
    session = await _opened('apply')
    cooperative.outcomes = [problem, {'ok': True}]
    with pytest.raises(bsk.Unavailable):
        await session.call('evaluate', bsk.PAGE)
    assert len(_reads(cooperative)) == 1
    await session.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_non_apply_session_and_non_fixed_scripts_get_no_new_retry(cooperative):
    session = await _opened('jd')
    cooperative.outcomes = [_refusal(), {'ok': True}]
    with pytest.raises(bsk.Unavailable):
        await session.call('evaluate', bsk.PAGE)
    assert len(_reads(cooperative)) == 1
    await session.__aexit__(None, None, None)

    cooperative.calls.clear()
    session = await _opened('apply')
    for args in (('evaluate', 'document.title'),                  # model-style script
                 ('evaluate', bsk.PAGE, '--extra'),
                 ('click', '#submit-application'),                # mutating verb
                 ('fill', '#name', '--value', 'Test User')):
        cooperative.outcomes = [_refusal(), {'ok': True}]
        before = len(cooperative.actions(args[0]))
        with pytest.raises(bsk.Unavailable):
            # `_run` is the raw seam; the public `call` already refuses these.
            await session._run(args, {})
        assert len(cooperative.actions(args[0])) == before + 1, args
    await session.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_cooperation_sync_required_retry_is_unchanged(cooperative):
    session = await _opened('apply')
    sync_required = _refusal(reason='cooperation_sync_required')
    cooperative.outcomes = [sync_required, {'ok': True, 'value': {}}]
    await session.call('evaluate', bsk.PAGE)
    assert len(_reads(cooperative)) == 2
    cooperative.outcomes = [sync_required, sync_required, {'ok': True}]
    with pytest.raises(bsk.Unavailable):
        await session.call('evaluate', bsk.PAGE)
    assert len(_reads(cooperative)) == 4
    await session.__aexit__(None, None, None)


def test_user_service_never_spawns_shared_browser_skill_helper(monkeypatch):
    monkeypatch.setenv('APPLIEDIN_USER_SERVICE', '1')

    def unavailable(*a, **kw):
        raise bsk.Unavailable('offline helper absent')

    monkeypatch.setattr(bsk, '_sync', unavailable)
    monkeypatch.setattr(bsk.subprocess, 'Popen',
                        lambda *a, **kw: pytest.fail('service launched shared helper'))
    ready, detail = bsk.ensure_daemon()
    assert not ready and 'outside' in detail and 'orchestrator' in detail


def test_user_service_reuses_reachable_shared_helper(monkeypatch):
    monkeypatch.setenv('APPLIEDIN_USER_SERVICE', '1')
    monkeypatch.setattr(bsk, '_sync', lambda *a, **kw: {'ok': True})
    monkeypatch.setattr(bsk.subprocess, 'Popen',
                        lambda *a, **kw: pytest.fail('unexpected new helper'))
    assert bsk.ensure_daemon() == (True, '')
