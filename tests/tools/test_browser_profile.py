"""Cold Chrome starts must be owned, serialized, and pinned to a human-confirmed ID."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from tools import browser_profile as profile
from tools import browser_runtime as runtime
from tools import browser_skill as bsk

_REAL_BROWSERS = profile._browsers


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    cfgfile = tmp_path / 'browser.local.yaml'
    directory = tmp_path / 'owned chrome'
    cfgfile.write_text(yaml.safe_dump({'engine': 'browser_skill', 'browser': 'pinned',
                                     'user_data_dir': str(directory),
                                     'chrome_path': '/fake/chrome'}))
    monkeypatch.setattr(profile, '_chrome', lambda cfg: '/fake/chrome')
    monkeypatch.setattr(bsk, 'ensure_daemon', lambda: (True, ''))
    monkeypatch.setattr(profile, '_STATUS_CACHE', ())
    monkeypatch.setattr(profile, 'profile_pids', lambda directory: [])
    monkeypatch.setattr(profile, '_browsers', lambda: [])
    monkeypatch.setattr(profile.subprocess, 'Popen',
                        lambda *a, **kw: pytest.fail('Unexpected real Chrome launch'))
    return cfgfile, directory


def test_connected_id_without_dedicated_process_is_refused(sandbox, monkeypatch):
    monkeypatch.setattr(profile, '_browsers', lambda: [{'instance_id': 'pinned'}])
    ready, why = profile.ensure_browser()
    assert not ready and 'absent or ambiguous' in why
    assert profile.connection_status()['state'] == 'unavailable'


@pytest.mark.parametrize('pids', [[123], [123, 456]])
def test_connected_id_requires_exactly_one_owned_chrome_process(sandbox, monkeypatch, pids):
    monkeypatch.setattr(profile, '_browsers', lambda: [{'instance_id': 'pinned'}])
    monkeypatch.setattr(profile, 'profile_pids', lambda directory: pids)
    ready, why = profile.ensure_browser()
    assert ready is (len(pids) == 1)
    if len(pids) != 1:
        assert 'ambiguous' in why
    else:
        assert profile.can_start() == (True, '')


def test_parallel_cold_starts_launch_once_with_only_approved_flags(sandbox, monkeypatch):
    _, directory = sandbox
    launched = []
    state = {'started': False}
    def spawn(args, **kwargs):
        launched.append((args, kwargs))
        state['started'] = True
        return SimpleNamespace(pid=123)
    monkeypatch.setattr(profile.subprocess, 'Popen', spawn)
    monkeypatch.setattr(profile, '_browsers', lambda: (
        [{'instance_id': 'pinned'}] if state['started'] else []))
    monkeypatch.setattr(profile, 'profile_pids',
                        lambda directory: [123] if state['started'] else [])
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: profile.ensure_browser(), range(6)))
    assert results == [(True, '')] * 6
    assert len(launched) == 1
    argv, kwargs = launched[0]
    assert argv == ['/fake/chrome', '--user-data-dir=' + str(directory),
                    '--no-first-run', '--no-default-browser-check', profile.DASHBOARD]
    assert kwargs['start_new_session'] is True
    assert kwargs['stdin'] == kwargs['stdout'] == kwargs['stderr'] == -3


def test_running_owned_profile_without_extension_never_launches_a_second(sandbox, monkeypatch):
    monkeypatch.setattr(profile, 'profile_pids', lambda directory: [123])
    ready, why = profile.ensure_browser()
    assert not ready and 'profile is running' in why and 'No second' in why


def test_new_foreign_instance_is_rejected_after_one_launch(sandbox, monkeypatch):
    state = {'launched': False}
    def spawn(*a, **kw):
        state['launched'] = True
        return SimpleNamespace(pid=123)
    monkeypatch.setattr(profile.subprocess, 'Popen', spawn)
    monkeypatch.setattr(profile, '_browsers', lambda: (
        [{'instance_id': 'foreign'}] if state['launched'] else []))
    ready, why = profile.ensure_browser()
    assert not ready and 'different instance' in why


def test_timeout_is_bounded_without_relaunch(sandbox, monkeypatch):
    launches = []
    clock = {'now': 0}
    monkeypatch.setattr(profile.subprocess, 'Popen',
                        lambda *a, **kw: (launches.append(a) or SimpleNamespace(pid=123)))
    monkeypatch.setattr(profile.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr(profile.time, 'sleep',
                        lambda seconds: clock.update(now=clock['now'] + seconds))
    ready, why = profile.ensure_browser()
    assert not ready and '45 seconds' in why
    assert len(launches) == 1 and clock['now'] == 45


@pytest.mark.parametrize('value', [[], False, 123, ''])
def test_directory_requires_a_real_nonempty_path(sandbox, value):
    path, _ = sandbox
    path.write_text(yaml.safe_dump({'engine': 'browser_skill', 'user_data_dir': value}))
    with pytest.raises(ValueError, match='user_data_dir'):
        runtime.configuration()


@pytest.mark.parametrize('value', [[], False, 123])
def test_chrome_path_requires_a_string(sandbox, value):
    path, _ = sandbox
    path.write_text(yaml.safe_dump({'engine': 'browser_skill', 'chrome_path': value}))
    with pytest.raises(ValueError, match='chrome_path'):
        runtime.configuration()


@pytest.mark.parametrize('directory', ['~/.config/google-chrome',
                                     '~/.config/google-chrome/synthetic-profile',
                                     str(Path(__file__).resolve().parents[2] / '.local/chrome')])
def test_main_profile_and_repository_paths_are_rejected(sandbox, directory):
    path, _ = sandbox
    path.write_text(yaml.safe_dump({'engine': 'browser_skill', 'user_data_dir': directory}))
    with pytest.raises(ValueError, match='outside'):
        runtime.configuration()


def test_unknown_config_keys_still_fail(sandbox):
    path, _ = sandbox
    path.write_text('engine: browser_skill\nremote_debugging_port: 9222\n')
    with pytest.raises(ValueError, match='Invalid browser configuration'):
        runtime.configuration()


def test_process_detection_matches_exact_directory_and_split_flag(tmp_path, monkeypatch):
    directory = tmp_path / 'owned chrome'
    monkeypatch.setattr(profile, '_processes', lambda: iter([
        (1, ['chrome', '--user-data-dir=' + str(directory)]),
        (2, ['chrome', '--user-data-dir=' + str(directory) + '-other']),
        (3, ['chrome', '--user-data-dir', str(directory)]),
        (4, ['chrome']),
    ]))
    assert profile.profile_pids(str(directory)) == [1, 3]


@pytest.mark.parametrize(('connected', 'pids', 'expected'), [
    (True, [123], 'connected'), (True, [], 'unavailable'),
    (False, [123], 'extension_missing'), (False, [], 'not_running'),
])
def test_status_distinguishes_connection_states_without_launch(sandbox, monkeypatch,
                                                               connected, pids, expected):
    monkeypatch.setattr(profile, '_browsers',
                        lambda: [{'instance_id': 'pinned'}] if connected else [])
    monkeypatch.setattr(profile, 'profile_pids', lambda directory: pids)
    status = runtime.browser_status()
    assert status['state'] == expected
    check = runtime.setup_check()
    assert check['state'] == ('ready' if expected == 'connected' else
                              'check' if expected == 'not_running' else 'action')



_SKEW_DETAIL = 'bsk update, then bsk daemon stop; the next bsk call starts the new daemon'
_MISSING_DETAIL = 'Dedicated Chrome is running but pinned BrowserSkill is not connected.'
_CONNECTED_DETAIL = 'Pinned extension and dedicated Chrome argv observed.'
_NOT_RUNNING_DETAIL = 'Dedicated Chrome is not running; it will start for the next browser task.'


@pytest.mark.parametrize(('rows', 'pids', 'state', 'detail'), [
    ([{'instance_id': 'pinned', 'version_skew': True}], [123],
     'version_skew', _SKEW_DETAIL),
    ([{'instance_id': 'pinned', 'version_skew': True},
      {'instance_id': 'foreign', 'version_skew': True}], [123],
     'version_skew', _SKEW_DETAIL),
    ([{'instance_id': 'pinned', 'version_skew': True},
      {'instance_id': 'pinned', 'version_skew': True}], [123],
     'extension_missing', _MISSING_DETAIL),
    ([{'instance_id': 'foreign', 'version_skew': True}], [123],
     'extension_missing', _MISSING_DETAIL),
    ([{'instance_id': 'pinned', 'unresponsive': True}], [123],
     'extension_missing', _MISSING_DETAIL),
    ([], [123], 'extension_missing', _MISSING_DETAIL),
    ([{'instance_id': 'pinned', 'version_skew': False}], [123],
     'connected', _CONNECTED_DETAIL),
    ([{'instance_id': 'pinned', 'version_skew': True},
      {'instance_id': 'pinned'}], [123], 'connected', _CONNECTED_DETAIL),
    ([{'instance_id': 'pinned', 'version_skew': True}], [],
     'not_running', _NOT_RUNNING_DETAIL),
    ([{'instance_id': 'pinned', 'version_skew': True}], [123, 456],
     'unavailable', 'Dedicated Chrome process is absent or ambiguous for pinned user-data-dir'),
    ([{'instance_id': 'pinned'}], [],
     'unavailable', 'Dedicated Chrome process is absent or ambiguous for pinned user-data-dir'),
])
def test_status_classifies_only_observed_pinned_skew(
        sandbox, monkeypatch, rows, pids, state, detail):
    observations = []

    def browsers():
        observations.append(True)
        return rows

    monkeypatch.setattr(profile, '_browsers', browsers)
    monkeypatch.setattr(profile, 'profile_pids', lambda _directory: pids)
    expected = {'state': state, 'detail': detail}
    assert profile.connection_status() == expected
    assert runtime.browser_status() == expected
    # Each status consumer gets one enumeration, not a second potentially
    # contradictory snapshot for skew classification.
    assert len(observations) == 2


def test_status_without_pin_never_enumerates_or_checks_process(sandbox, monkeypatch):
    path, _ = sandbox
    cfg = yaml.safe_load(path.read_text())
    cfg['browser'] = ''
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(profile, '_browsers',
                        lambda: pytest.fail('No enumeration before pinning'))
    monkeypatch.setattr(profile, 'profile_pids',
                        lambda _: pytest.fail('No process check before pinning'))
    assert profile.connection_status() == {
        'state': 'setup_required',
        'detail': 'Run ./appliedin browser-setup for the clean profile.',
    }


@pytest.mark.parametrize('source', ['rows', 'process'])
def test_status_observation_error_remains_unavailable(sandbox, monkeypatch, source):
    def fail(*_args):
        raise OSError('synthetic observation error')

    if source == 'rows':
        monkeypatch.setattr(profile, '_browsers', fail)
    else:
        monkeypatch.setattr(profile, 'profile_pids', fail)
    assert profile.connection_status() == {
        'state': 'unavailable', 'detail': 'synthetic observation error'}


def test_status_configuration_error_remains_unavailable(sandbox):
    path, _ = sandbox
    path.write_text(yaml.safe_dump({
        'engine': 'browser_skill', 'browser': 'pinned', 'user_data_dir': ''}))
    assert runtime.browser_status()['state'] == 'unavailable'


def test_status_version_skew_is_diagnostic_only(sandbox, monkeypatch):
    rows = [{'instance_id': 'pinned', 'version_skew': True}]
    assert not profile._connected(rows, 'pinned')
    monkeypatch.setattr(profile, '_browsers', lambda: rows)
    monkeypatch.setattr(profile, 'profile_pids', lambda _directory: [123])

    def forbidden(*_args, **_kwargs):
        pytest.fail('Status must not start, update, stop, navigate, or apply')

    for target, name in ((profile, '_launch'), (profile, '_launch_lock'),
                         (profile, 'ensure_browser'), (profile, 'browser_setup'),
                         (bsk, 'ensure_daemon'), (bsk, 'command')):
        monkeypatch.setattr(target, name, forbidden)
    assert profile.connection_status() == {
        'state': 'version_skew', 'detail': _SKEW_DETAIL}
    assert runtime.browser_status() == {
        'state': 'version_skew', 'detail': _SKEW_DETAIL}
    assert runtime.setup_check()['state'] == 'action'


def test_status_version_skew_reaches_stats_route_offline(sandbox, monkeypatch):
    from fastapi.testclient import TestClient
    from core import flags, preparation
    from discovery import handler
    import server

    monkeypatch.setattr(profile, '_browsers',
                        lambda: [{'instance_id': 'pinned', 'version_skew': True}])
    monkeypatch.setattr(profile, 'profile_pids', lambda _directory: [123])

    def forbidden(*_args, **_kwargs):
        pytest.fail('/stats must not cause browser, daemon, or application actions')

    for target, name in ((profile, '_launch'), (profile, '_launch_lock'),
                         (profile, 'ensure_browser'), (bsk, 'ensure_daemon'),
                         (bsk, 'command')):
        monkeypatch.setattr(target, name, forbidden)
    monkeypatch.setattr(server, 'make_stores', lambda _settings: SimpleNamespace(
        tracking=SimpleNamespace(status_counts=lambda: {})))
    monkeypatch.setattr(preparation, 'snapshot', lambda: [])
    monkeypatch.setattr(handler, 'scanning', lambda: [])
    monkeypatch.setattr(handler, 'active', lambda: False)
    monkeypatch.setattr(server, '_discovery_running', lambda: False)
    monkeypatch.setattr(server, '_applying_now', lambda: 0)
    monkeypatch.setattr(server, '_throttle_state', lambda: {})
    monkeypatch.setattr(server, '_RUNNING', {'discover': False, 'process': False})
    for name, value in (('llm_error', ''), ('paused', False),
                        ('apply_mode', 'gated'), ('browser_headless', False),
                        ('workers_down', [])):
        monkeypatch.setattr(flags, name, lambda result=value: result)

    response = TestClient(server.create_app()).get('/stats')
    assert response.status_code == 200
    assert response.json()['browser_status'] == {
        'state': 'version_skew', 'detail': _SKEW_DETAIL}




@pytest.fixture
def stats_queue_world(sandbox, monkeypatch):
    """Stats must use a synthetic queue and never contact owner Redis or Chrome."""
    import fakeredis
    import server
    from core import flags, preparation
    from core.apply_queue import ApplyQueue
    from discovery import handler

    client = fakeredis.FakeRedis(decode_responses=True)
    stores = SimpleNamespace(tracking=SimpleNamespace(
        r=client, status_counts=lambda: {},
    ))
    monkeypatch.setattr(server, 'make_stores', lambda _settings: stores)
    monkeypatch.setattr(server, 'get_settings',
                        lambda: SimpleNamespace(auto_min_score=7))
    monkeypatch.setattr(server, '_discovery_running', lambda: False)
    monkeypatch.setattr(server, '_applying_now', lambda: 0)
    monkeypatch.setattr(server, '_throttle_state', lambda: {})
    monkeypatch.setattr(server, '_RUNNING', {'discover': False, 'process': False})
    monkeypatch.setattr(preparation, 'snapshot', lambda: [])
    monkeypatch.setattr(handler, 'scanning', lambda: [])
    monkeypatch.setattr(handler, 'active', lambda: False)
    monkeypatch.setattr(runtime, 'browser_status',
                        lambda _settings: {'state': 'unavailable'})
    for name, value in (('llm_error', ''), ('paused', False),
                        ('apply_mode', 'gated'), ('submit_review_enabled', False),
                        ('browser_headless', False), ('workers_down', [])):
        monkeypatch.setattr(flags, name, lambda result=value: result)
    endpoint = next(route.endpoint for route in server.create_app().routes
                    if getattr(route, 'path', '') == '/stats')
    return endpoint, ApplyQueue(client), client


def test_stats_uses_oldest_positive_pending_queue_stamp(stats_queue_world, monkeypatch):
    """A legacy zero stamp must not make a busy queue look decades old."""
    import json
    import time

    endpoint, queue, client = stats_queue_world
    monkeypatch.setattr(time, 'time', lambda: 500_000.0)
    assert queue.put('example-co#oldest', 'example-co', queued_at=499_870.0)
    assert queue.put('other-co#newest', 'other-co', queued_at=499_950.0)
    # Pre-stamp queue records are still waiting, but 0 is not January 1970.
    client.sadd('applyq:companies', 'legacy-co')
    client.rpush('applyq:co:legacy-co',
                 json.dumps({'pk': 'legacy-co#old', 'company': 'legacy-co',
                             'queued_at': 0}))
    assert endpoint()['queue_age_seconds'] == 130


def test_stats_empty_queue_has_no_age(stats_queue_world):
    """No pending applications must be shown as no age, not a fabricated zero."""
    endpoint, _, _ = stats_queue_world
    assert endpoint()['queue_age_seconds'] is None


def test_stats_queue_read_error_does_not_break_dashboard(stats_queue_world, monkeypatch):
    """A temporary queue-store fault must not take the whole dashboard offline."""
    from core.apply_queue import ApplyQueue

    endpoint, _, _ = stats_queue_world

    def broken_pending(_self):
        raise RuntimeError('synthetic queue read failure')

    monkeypatch.setattr(ApplyQueue, 'pending', broken_pending)
    assert endpoint()['queue_age_seconds'] is None


@pytest.mark.parametrize('answer', ['', 'n', 'y'])
def test_setup_saves_only_after_y_and_keeps_optional_settings(sandbox, monkeypatch, answer):
    path, directory = sandbox
    # Cold initial setup has no saved pin; a previously saved different ID
    # must never be silently overwritten by a newly visible instance.
    settings = yaml.safe_load(path.read_text())
    settings['browser'] = ''
    path.write_text(yaml.safe_dump(settings))
    before = path.read_text()
    calls = []
    monkeypatch.setattr(profile, '_browsers', lambda: (
        [{'instance_id': 'new-instance'}] if calls else []))
    monkeypatch.setattr(profile.subprocess, 'Popen',
                        lambda args, **kw: (calls.append(args) or SimpleNamespace(pid=123)))
    monkeypatch.setattr(profile, 'profile_pids',
                        lambda directory: [123] if calls else [])
    monkeypatch.setattr('builtins.input', lambda prompt: answer)
    result = profile.browser_setup(timeout_s=2)
    assert calls[0][-1] == profile.WEB_STORE
    if answer == 'y':
        assert result['status'] == 'configured'
        cfg = yaml.safe_load(path.read_text())
        assert cfg['browser'] == 'new-instance'
        assert cfg['user_data_dir'] == str(directory) and cfg['chrome_path'] == '/fake/chrome'
    else:
        assert result['saved'] is False and path.read_text() == before


async def test_session_checks_auto_start_before_starting_bsk(sandbox, monkeypatch):
    monkeypatch.setattr(bsk, 'available', lambda: (True, ''))
    ensured = []
    monkeypatch.setattr(profile, 'ensure_browser', lambda: (ensured.append(True) or True, ''))
    command = AsyncMock(side_effect=[
        {'session_id': 'ours', 'browser_instance_id': 'pinned'}, {},
    ])
    monkeypatch.setattr(bsk, 'command', command)
    async with bsk.Session('jd'):
        assert ensured == [True]
    assert command.call_args_list[0].args[:2] == ('session', 'start')
    assert command.call_args_list[-1].args == ('session', 'stop', 'ours')


def test_cli_setup_does_not_start_daemon_or_discovery(sandbox, monkeypatch, capsys):
    from cli import main
    monkeypatch.setattr(profile, 'browser_setup', lambda: {'status': 'waiting'})
    main(['browser-setup'])
    assert 'waiting' in capsys.readouterr().out


def test_empty_browser_snapshot_does_not_call_the_five_second_waiter(monkeypatch):
    calls = []
    def sync(*args, **kwargs):
        calls.append(args)
        assert args == ('status',)
        return {'browsers': []}
    monkeypatch.setattr(bsk, '_sync', sync)
    assert profile._browsers() == []
    assert calls == [('status',)]


@pytest.mark.parametrize('raw', [
    b'/opt/google/chrome/chrome\0--user-data-dir=/tmp/owned\0--no-first-run\0',
    b'/opt/google/chrome/chrome --user-data-dir=/tmp/owned --no-first-run\0',
])
def test_native_and_rewritten_chrome_argv_identify_the_same_owned_process(monkeypatch, raw):
    args = profile._argv(raw)
    assert Path(args[0]).name == 'chrome'
    monkeypatch.setattr(profile, '_processes', lambda: iter([(123, args)]))
    assert profile.profile_pids('/tmp/owned') == [123]
    assert profile.profile_pids('/tmp/other') == []


def test_rewritten_quoted_profile_path_preserves_spaces(monkeypatch):
    args = profile._argv(b'/opt/google/chrome/chrome --user-data-dir="/tmp/owned chrome"\0')
    monkeypatch.setattr(profile, '_processes', lambda: iter([(123, args)]))
    assert profile.profile_pids('/tmp/owned chrome') == [123]


def test_launched_pid_cannot_be_claimed_without_observable_lineage(sandbox, monkeypatch):
    _, directory = sandbox
    monkeypatch.setattr(profile, 'profile_pids', lambda directory: [234])
    monkeypatch.setattr(profile, '_spawned_lineage', lambda launcher, observed: False)
    with pytest.raises(ValueError, match='lineage'):
        profile._owned_profile_pid({'user_data_dir': str(directory)}, launched_pid=123)


async def test_connected_session_checks_process_before_browser_ipc(sandbox, monkeypatch):
    monkeypatch.setattr(profile, '_browsers', lambda: [{'instance_id': 'pinned'}])
    monkeypatch.setattr(bsk, 'available', lambda: (True, ''))
    calls = AsyncMock()
    monkeypatch.setattr(bsk, 'command', calls)
    with pytest.raises(bsk.Unavailable, match='absent or ambiguous'):
        async with bsk.Session('apply'):
            pytest.fail('No session should open without Chrome argv proof')
    calls.assert_not_awaited()


def test_setup_refuses_pin_without_a_process(sandbox, monkeypatch):
    path, _directory = sandbox
    cfg = yaml.safe_load(path.read_text())
    cfg['browser'] = ''
    path.write_text(yaml.safe_dump(cfg))
    original = path.read_text()
    state = {'started': False}
    monkeypatch.setattr(profile.subprocess, 'Popen',
                        lambda *a, **kw: (state.update(started=True)
                                           or SimpleNamespace(pid=123)))
    monkeypatch.setattr(profile, '_browsers', lambda: (
        [{'instance_id': 'new-instance'}] if state['started'] else []))
    monkeypatch.setattr('builtins.input', lambda _prompt: 'y')
    outcome = profile.browser_setup(timeout_s=2)
    assert outcome['status'] == 'blocked'
    assert 'absent or ambiguous' in outcome['error']
    assert path.read_text() == original


@pytest.mark.parametrize('flags', [
    ['--user-data-dir=/tmp/owned', '--user-data-dir=/tmp/other'],
    ['--user-data-dir', '/tmp/owned', '--user-data-dir=/tmp/other'],
    ['--user-data-dir=/tmp/owned', '--user-data-dir=/tmp/owned'],
])
def test_conflicting_or_repeated_profile_flags_are_not_process_proof(monkeypatch, flags):
    monkeypatch.setattr(profile, '_processes', lambda: iter([(123, ['chrome', *flags])]))
    assert profile.profile_pids('/tmp/owned') == []


def test_setup_valid_connected_pin_is_idempotent_without_launch_or_write(
        sandbox, monkeypatch):
    path, directory = sandbox
    old = path.read_bytes()
    monkeypatch.setattr(profile, '_browsers', lambda: [{'instance_id': 'pinned'}])
    monkeypatch.setattr(profile, 'profile_pids', lambda _directory: [123])
    monkeypatch.setattr(profile, '_launch_lock',
                        lambda *a: pytest.fail('Valid rerun may not create a lock'))
    monkeypatch.setattr('builtins.input',
                        lambda *a: pytest.fail('Pinned rerun needs no new consent'))
    result = profile.browser_setup(timeout_s=2)
    assert result == {'status': 'ready', 'browser': 'pinned',
                      'user_data_dir': str(directory), 'saved': False}
    assert path.read_bytes() == old


def test_setup_unpinned_connected_candidate_can_be_human_verified(
        sandbox, monkeypatch):
    path, directory = sandbox
    cfg = yaml.safe_load(path.read_text())
    cfg['browser'] = ''
    path.write_text(yaml.safe_dump(cfg))
    old = path.read_bytes()
    monkeypatch.setattr(profile, '_browsers', lambda: [{'instance_id': 'existing'}])
    monkeypatch.setattr(profile, 'profile_pids', lambda _directory: [123])
    seen = []
    monkeypatch.setattr('builtins.input',
                        lambda prompt: (seen.append(prompt) or 'y'))
    result = profile.browser_setup(timeout_s=2)
    assert result['status'] == 'configured' and result['browser'] == 'existing'
    assert 'Verify' in seen[0]
    assert yaml.safe_load(path.read_text()) == {**cfg, 'browser': 'existing'}
    assert old != path.read_bytes()


@pytest.mark.parametrize('changed,new_value', [
    ('browser', 'competing-pin'),
    ('engine', 'chrome'),
    ('user_data_dir', '/tmp/another-synthetic-profile'),
    ('chrome_path', '/opt/another-synthetic-chrome'),
])
def test_setup_drift_during_confirmation_never_overwrites(
        sandbox, monkeypatch, changed, new_value):
    path, _directory = sandbox
    cfg = yaml.safe_load(path.read_text())
    cfg['browser'] = ''
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(profile, '_browsers',
                        lambda: [{'instance_id': 'existing'}])
    monkeypatch.setattr(profile, 'profile_pids', lambda _: [123])
    def competitor(_prompt):
        values = yaml.safe_load(path.read_text())
        values[changed] = new_value
        path.write_text(yaml.safe_dump(values))
        return 'y'
    monkeypatch.setattr('builtins.input', competitor)
    result = profile.browser_setup(timeout_s=2)
    assert result['status'] == 'blocked' and result.get('status') != 'ready'
    assert yaml.safe_load(path.read_text())[changed] == new_value
    assert yaml.safe_load(path.read_text())['browser'] == (
        new_value if changed == 'browser' else '')


def test_setup_saved_pin_rejects_foreign_candidate_without_repin(
        sandbox, monkeypatch):
    path, _ = sandbox
    initial = path.read_bytes()
    monkeypatch.setattr(profile, '_browsers',
                        lambda: [{'instance_id': 'foreign'}])
    monkeypatch.setattr(profile, 'profile_pids', lambda _: [123])
    outcome = profile.browser_setup(timeout_s=2)
    assert outcome['status'] == 'blocked' and 'saved pin' in outcome['error']
    assert path.read_bytes() == initial



@pytest.mark.parametrize("snapshot", ["connected_at_entry", "reconnecting"])
def test_setup_retained_pin_with_chrome_engine_never_reports_ready(
        sandbox, monkeypatch, snapshot):
    """A connected BSK ID is not readiness while the Claude Chrome engine is selected."""
    from contextlib import nullcontext

    path, _ = sandbox
    saved = yaml.safe_load(path.read_text())
    saved["engine"] = "chrome"
    path.write_text(yaml.safe_dump(saved))
    initial = path.read_bytes()
    snapshots = []
    def browsers():
        snapshots.append(True)
        if snapshot == "reconnecting" and len(snapshots) == 1:
            return []
        return [{"instance_id": "pinned"}]
    monkeypatch.setattr(profile, "_browsers", browsers)
    monkeypatch.setattr(profile, "profile_pids", lambda _: [123])
    # For comparison with the old reconnect path, never create a real lock.
    monkeypatch.setattr(profile, "_launch_lock", lambda _: nullcontext())
    monkeypatch.setattr(profile, "_save_instance",
                        lambda *a, **kw: pytest.fail("Wrong engine may not rewrite pin"))
    monkeypatch.setattr("builtins.input",
                        lambda *_a: pytest.fail("A saved pin needs no new consent"))
    result = profile.browser_setup(timeout_s=2)
    assert result["status"] == "blocked" and result["saved"] is False
    assert "engine: browser_skill" in result["error"]
    assert "rerun" in result["error"]
    assert path.read_bytes() == initial
    assert not snapshots  # Refuse before even consulting an unrelated backend.


def test_first_time_setup_still_allows_explicit_engine_selection_with_consent(
        sandbox, monkeypatch):
    """Do not break the unpinned, human-confirmed chrome -> BSK migration."""
    path, _ = sandbox
    cfg = yaml.safe_load(path.read_text())
    cfg["engine"], cfg["browser"] = "chrome", ""
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(profile, "_browsers", lambda: [{"instance_id": "new-synthetic"}])
    monkeypatch.setattr(profile, "profile_pids", lambda _: [123])
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    result = profile.browser_setup(timeout_s=2)
    assert result["status"] == "configured"
    updated = yaml.safe_load(path.read_text())
    assert updated["browser"] == "new-synthetic"
    assert updated["engine"] == "browser_skill"



def _fixture_enumeration(monkeypatch, *, rows=None, unavailable=False):
    monkeypatch.setattr(profile, '_browsers', _REAL_BROWSERS)

    def sync(command, **_kwargs):
        if command == 'status':
            return {'browsers': [{'instance_id': 'status-only'}]}
        if unavailable:
            raise bsk.Unavailable('synthetic BrowserSkill enumeration failure')
        return {'browsers': rows if rows is not None else []}
    monkeypatch.setattr(bsk, '_sync', sync)


@pytest.mark.parametrize('browser_unavailable', [False, True])
@pytest.mark.parametrize('process_args', [None, ['--user-data-dir=/synthetic/personal']])
def test_fixture_missing_canonical_process_diagnostic(
        sandbox, monkeypatch, browser_unavailable, process_args):
    _, directory = sandbox
    _fixture_enumeration(monkeypatch, unavailable=browser_unavailable)
    monkeypatch.setattr(profile, 'profile_pids', lambda _directory: [])
    monkeypatch.setattr(profile, '_processes', lambda: iter(
        [] if process_args is None else [(101, ['chrome', *process_args])]))
    monkeypatch.setattr(profile, '_launch',
                        lambda *_a, **_kw: pytest.fail('Fixture must not launch Chrome'))
    monkeypatch.setattr(profile, '_launch_lock',
                        lambda *_a, **_kw: pytest.fail('Fixture must not lock for launch'))
    monkeypatch.setattr(bsk, 'ensure_daemon',
                        lambda: pytest.fail('Fixture must not recover BrowserSkill'))
    ready, why = profile._fixture_read_only_browser()
    assert not ready
    assert 'dedicated Chrome not running' in why
    assert 'responsible orchestrator' in why and 'AGENTS.md' in why
    assert 'reconnect it' not in why and 'Popen' not in why


@pytest.mark.parametrize('flags, expected', [
    ([], 'dedicated Chrome not running'),
    (['--user-data-dir=/synthetic/personal'], 'dedicated Chrome not running'),
    (None, 'pinned extension is absent, disconnected or ambiguous'),
    (['--user-data-dir=TARGET', '--user-data-dir=TARGET'],
     'pinned extension is absent, disconnected or ambiguous'),
    (['--user-data-dir', 'TARGET', '--user-data-dir=/synthetic/personal'],
     'pinned extension is absent, disconnected or ambiguous'),
])
def test_fixture_process_argv_target_scan(sandbox, monkeypatch, flags, expected):
    _, directory = sandbox
    _fixture_enumeration(monkeypatch)
    target = str(directory.resolve())
    if flags is None:
        args = ['--user-data-dir=' + target]
    else:
        args = [value.replace('TARGET', target) for value in flags]
    monkeypatch.setattr(profile, '_processes', lambda: iter([(101, ['chrome', *args])]))
    ready, why = profile._fixture_read_only_browser()
    assert not ready and expected in why
    if 'ambiguous' in expected:
        assert 'dedicated Chrome not running' not in why and 'AGENTS.md' not in why


def test_fixture_genuine_empty_browser_snapshot_uses_process_verdict(sandbox, monkeypatch):
    # Status reports an enumeration candidate; the successful second read is empty.
    _fixture_enumeration(monkeypatch, rows=[])
    monkeypatch.setattr(profile, 'profile_pids', lambda _directory: [])
    monkeypatch.setattr(profile, '_processes', lambda: iter([]))
    ready, why = profile._fixture_read_only_browser()
    assert not ready and 'dedicated Chrome not running' in why
    assert 'AGENTS.md' in why and 'responsible orchestrator' in why


def test_fixture_swallowed_browser_unavailable_uses_independent_proc_verdict(
        sandbox, monkeypatch):
    _fixture_enumeration(monkeypatch, unavailable=True)
    monkeypatch.setattr(profile, 'profile_pids', lambda _directory: [])
    monkeypatch.setattr(profile, '_processes', lambda: iter([]))
    ready, why = profile._fixture_read_only_browser()
    assert not ready and 'dedicated Chrome not running' in why
    assert 'AGENTS.md' in why


def test_fixture_swallowed_browser_unavailable_target_process_keeps_ambiguity(
        sandbox, monkeypatch):
    _, directory = sandbox
    _fixture_enumeration(monkeypatch, unavailable=True)
    target = str(directory.resolve())
    monkeypatch.setattr(profile, 'profile_pids', lambda _directory: [])
    monkeypatch.setattr(profile, '_processes', lambda: iter([
        (101, ['chrome', '--user-data-dir=' + target, '--user-data-dir=/synthetic/other'])]))
    ready, why = profile._fixture_read_only_browser()
    assert not ready
    assert why == ('BrowserSkill unavailable: pinned extension is absent, '
                   'disconnected or ambiguous; reconnect it and rerun fixture.')


def test_fixture_proc_enumeration_failure_stays_preflight_failure(sandbox, monkeypatch):
    _fixture_enumeration(monkeypatch, unavailable=True)
    monkeypatch.setattr(profile, 'profile_pids', lambda _directory: [])
    def failed_process_scan():
        raise OSError('synthetic /proc iterdir failure')
    monkeypatch.setattr(profile, '_processes', failed_process_scan)
    ready, why = profile._fixture_read_only_browser()
    assert not ready and 'read-only fixture preflight failed (OSError)' in why
    assert 'dedicated Chrome not running' not in why and 'AGENTS.md' not in why


def test_fixture_disconnected_and_ready_paths_remain_distinct(sandbox, monkeypatch):
    _fixture_enumeration(monkeypatch)
    monkeypatch.setattr(profile, 'profile_pids', lambda _directory: [101])
    ready, why = profile._fixture_read_only_browser()
    assert not ready and 'pinned extension is absent' in why
    assert 'dedicated Chrome not running' not in why

    _fixture_enumeration(monkeypatch, rows=[{'instance_id': 'pinned'}])
    monkeypatch.setattr(profile, 'profile_pids', lambda _directory: [101])
    assert profile._fixture_read_only_browser() == (True, '')


def test_fixture_changed_configuration_wins_over_missing_diagnostic(sandbox, monkeypatch):
    _fixture_enumeration(monkeypatch)
    initial = profile._config()
    changed = {**initial, 'browser': 'competing-pin'}
    values = iter([initial, changed])
    monkeypatch.setattr(profile, '_config', lambda _settings=None: next(values))
    monkeypatch.setattr(profile, 'profile_pids', lambda _directory: [])
    monkeypatch.setattr(profile, '_processes', lambda: iter([]))
    ready, why = profile._fixture_read_only_browser()
    assert not ready and 'configuration changed' in why
    assert 'dedicated Chrome not running' not in why and 'AGENTS.md' not in why


def test_fixture_connected_extension_without_owned_process_is_contradictory(
        sandbox, monkeypatch):
    _fixture_enumeration(monkeypatch, rows=[{'instance_id': 'pinned'}])
    monkeypatch.setattr(profile, 'profile_pids', lambda _directory: [])
    monkeypatch.setattr(profile, '_processes', lambda: iter([]))
    ready, why = profile._fixture_read_only_browser()
    assert not ready and 'not independently observed' in why
    assert 'dedicated Chrome not running' not in why and 'AGENTS.md' not in why


@pytest.mark.parametrize('failure', ['status', 'profile_pids'])
def test_fixture_propagated_observation_errors_stay_preflight_failures(
        sandbox, monkeypatch, failure):
    if failure == 'status':
        monkeypatch.setattr(bsk, '_sync',
                            lambda *_a, **_kw: (_ for _ in ()).throw(
                                bsk.Unavailable('synthetic status failure')))
    else:
        _fixture_enumeration(monkeypatch)
        monkeypatch.setattr(profile, 'profile_pids',
                            lambda _directory: (_ for _ in ()).throw(
                                OSError('synthetic profile observation failure')))
    ready, why = profile._fixture_read_only_browser()
    assert not ready and 'read-only fixture preflight failed' in why
    assert 'dedicated Chrome not running' not in why and 'AGENTS.md' not in why


def test_fixture_multiple_owned_processes_remain_ambiguous(sandbox, monkeypatch):
    _fixture_enumeration(monkeypatch, rows=[{'instance_id': 'pinned'}])
    monkeypatch.setattr(profile, 'profile_pids', lambda _directory: [101, 102])
    ready, why = profile._fixture_read_only_browser()
    assert not ready and 'dedicated Chrome process is ambiguous' in why
    assert 'dedicated Chrome not running' not in why and 'AGENTS.md' not in why


def test_fixture_changed_configuration_wins_over_ready_result(sandbox, monkeypatch):
    _fixture_enumeration(monkeypatch, rows=[{'instance_id': 'pinned'}])
    initial = profile._config()
    changed = {**initial, 'browser': 'competing-pin'}
    values = iter([initial, changed])
    monkeypatch.setattr(profile, '_config', lambda _settings=None: next(values))
    monkeypatch.setattr(profile, 'profile_pids', lambda _directory: [101])
    ready, why = profile._fixture_read_only_browser()
    assert not ready and 'configuration changed' in why
    assert 'dedicated Chrome not running' not in why


def test_fixture_path_resolution_failure_stays_preflight_failure(
        sandbox, monkeypatch):
    _fixture_enumeration(monkeypatch)
    config = profile._config()
    monkeypatch.setattr(profile, '_config', lambda _settings=None: config)
    class UnresolvablePath:
        def __init__(self, _value):
            pass
        def expanduser(self):
            return self
        def resolve(self):
            raise OSError('synthetic path resolution failure')
    monkeypatch.setattr(profile, 'Path', UnresolvablePath)
    ready, why = profile._fixture_read_only_browser()
    assert not ready and 'read-only fixture preflight failed (OSError)' in why
    assert 'dedicated Chrome not running' not in why and 'AGENTS.md' not in why


def test_fixture_duplicate_pinned_rows_remain_ambiguous(sandbox, monkeypatch):
    _fixture_enumeration(monkeypatch, rows=[
        {'instance_id': 'pinned'}, {'instance_id': 'pinned'}])
    monkeypatch.setattr(profile, 'profile_pids', lambda _directory: [])
    monkeypatch.setattr(profile, '_processes', lambda: iter([]))
    ready, why = profile._fixture_read_only_browser()
    assert not ready and 'pinned extension is absent' in why
    assert 'dedicated Chrome not running' not in why and 'AGENTS.md' not in why
