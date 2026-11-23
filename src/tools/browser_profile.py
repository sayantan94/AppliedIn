"""Dedicated Chrome lifecycle. Never reuse the owner's default Chrome directory.

The BrowserSkill instance is pinned by human confirmation, not inferred from a
Chrome process. Process arguments prove only ownership of the directory we launch.
"""
from __future__ import annotations

import fcntl
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import yaml

WEB_STORE = 'https://chromewebstore.google.com/detail/hhcmgoofomhgciiibhipgmgkgnoenaoi'
DASHBOARD = 'http://127.0.0.1:8788/'
_STATUS_CACHE: tuple = ()
_STATUS_LOCK = threading.Lock()


def validate_directory(directory: Path, settings) -> None:
    roots = [Path.home() / '.config/google-chrome', Path.home() / '.config/chromium',
             Path(__file__).resolve().parents[2]]
    if directory == Path.home() or directory == Path('/'):
        raise ValueError('Use a dedicated Chrome directory, not the home or root directory')
    for root in roots:
        root = root.resolve()
        if directory.is_relative_to(root) or root.is_relative_to(directory):
            raise ValueError('Chrome user_data_dir must be outside the repository and main profile')


def _config(settings=None) -> dict:
    from tools.browser_runtime import configuration
    return configuration(settings)


def _browsers() -> list[dict]:
    from tools.browser_skill import Unavailable, _sync
    # The CLI has a five-second connection grace period when no browser exists.
    # Allow that grace, but avoid a second waiter for an already-empty snapshot.
    if not _sync('status', timeout=7).get('browsers'):
        return []
    try:
        return _sync('browsers', timeout=7).get('browsers', [])
    except Unavailable:
        return []  # The last extension may disconnect between the two reads.


def _connected(rows: list[dict], instance: str) -> bool:
    return bool(instance) and any(
        b.get('instance_id') == instance and not b.get('unresponsive')
        and not b.get('version_skew') for b in rows)


def _ids(rows: list[dict]) -> set[str]:
    return {b['instance_id'] for b in rows if isinstance(b.get('instance_id'), str)}


def _argv(raw: bytes) -> list[str]:
    args = raw.decode(errors='replace').rstrip('\0').split('\0')
    # Linux Chrome rewrites its process title into one argv entry. Without this,
    # a live owned profile looked absent and could be launched a second time.
    if len(args) == 1:
        try:
            return shlex.split(args[0])
        except ValueError:
            return []
    return args


def _processes():
    # Read argv as NUL-delimited bytes: ps text loses quoting for directories
    # with spaces. Never log argv; a user's launch URL may contain an OAuth code.
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = _argv((entry / 'cmdline').read_bytes())
        except OSError:
            continue
        if not args or Path(args[0]).name not in {
                'chrome', 'google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'}:
            continue
        if any(a.startswith('--type=') for a in args):
            continue
        yield int(entry.name), args


def profile_pids(directory: str) -> list[int]:
    target = Path(directory).expanduser().resolve()
    result = []
    for pid, args in _processes():
        values = [arg.partition('=')[2] if arg.startswith('--user-data-dir=') else (
            args[i + 1] if i + 1 < len(args) else '')
            for i, arg in enumerate(args)
            if arg.startswith('--user-data-dir=') or arg == '--user-data-dir']
        # Conflicting/repeated flags are ambiguous even if one matches the pin.
        if len(values) == 1 and values[0] and Path(values[0]).expanduser().resolve() == target:
            result.append(pid)
    return result


def _chrome(cfg: dict) -> str:
    selected = cfg['chrome_path']
    found = shutil.which(selected) if selected else (
        shutil.which('google-chrome-stable') or shutil.which('google-chrome'))
    if not found:
        raise ValueError('Chrome executable not found; configure chrome_path in browser.local.yaml')
    return found


@contextmanager
def _launch_lock(directory: str):
    profile = Path(directory)
    profile.parent.mkdir(parents=True, exist_ok=True)
    # Lock beside the profile, so Chrome's own SingletonLock remains untouched.
    with (profile.parent / ('.' + profile.name + '.appliedin-launch.lock')).open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _launch(cfg: dict, url: str) -> int:
    if _in_user_service():
        raise ValueError('Chrome launch is orchestrator-owned and forbidden inside the user service')
    Path(cfg['user_data_dir']).mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [_chrome(cfg), '--user-data-dir=' + cfg['user_data_dir'],
         '--no-first-run', '--no-default-browser-check', url],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    if not isinstance(proc.pid, int) or proc.pid <= 0:
        raise ValueError('Chrome launcher did not expose its process ID')
    return proc.pid


def _spawned_lineage(launcher: int, observed: int) -> bool:
    """Only independently observed PID ancestry, not a BSK-ID-to-PID claim."""
    pid, seen = observed, set()
    while pid > 1 and pid not in seen:
        if pid == launcher:
            return True
        seen.add(pid)
        try:
            status = (Path('/proc') / str(pid) / 'status').read_text()
            parent = next(line for line in status.splitlines()
                          if line.startswith('PPid:')).partition(':')[2].strip()
            pid = int(parent)
        except (OSError, ValueError, StopIteration):
            return False
    return False


def _owned_profile_pid(cfg: dict, launched_pid: int | None = None) -> int:
    """Independently observe exactly one canonical Chrome owner process."""
    pids = profile_pids(cfg['user_data_dir'])
    if len(pids) != 1:
        raise ValueError('Dedicated Chrome process is absent or ambiguous for pinned user-data-dir')
    if launched_pid is not None and not _spawned_lineage(launched_pid, pids[0]):
        raise ValueError('Launched Chrome PID/argv lineage cannot be established')
    return pids[0]

def _in_user_service() -> bool:
    return os.environ.get('APPLIEDIN_USER_SERVICE') == '1'


def _outside_user_service_cgroup(pid: int) -> bool:
    """The pinned Chrome owner must be outside this daemon service cgroup."""
    try:
        def unified(path):
            for line in path.read_text().splitlines():
                if line.startswith('0::'):
                    return line[3:]
            return ''

        ours = unified(Path('/proc/self/cgroup'))
        chrome = unified(Path('/proc') / str(pid) / 'cgroup')
        return bool(ours and chrome and ours != '/'
                    and chrome != ours and not chrome.startswith(ours.rstrip('/') + '/'))
    except OSError:
        return False


def _adopt_service_browser(cfg: dict, rows: list[dict]) -> tuple[bool, str]:
    """No launch, profile switch or ambiguous extension/process adoption."""
    matches = [b for b in rows if b.get('instance_id') == cfg['browser']
               and not b.get('unresponsive') and not b.get('version_skew')]
    if len(matches) != 1:
        return False, ('BrowserSkill unavailable: pinned extension is absent, disconnected '
                       'or ambiguous; the orchestrator must repair the external browser.')
    try:
        pid = _owned_profile_pid(cfg)
        if not _outside_user_service_cgroup(pid):
            return False, ('BrowserSkill unavailable: pinned Chrome is inside the daemon '
                           'service cgroup or cgroup separation is unverified; '
                           'the orchestrator must repair the external browser.')
        return True, ''
    except (OSError, ValueError, RuntimeError):
        return False, ('BrowserSkill unavailable: one dedicated pinned Chrome process '
                       'was not proven; the orchestrator must inspect it.')


def can_start(settings=None) -> tuple[bool, str]:
    """A connected ID without an owned Chrome argv is NOT a usable session."""
    try:
        cfg = _config(settings)
        if not cfg['browser']:
            return False, 'BrowserSkill unavailable: run ./appliedin browser-setup first.'
        rows = _browsers()
        if _in_user_service():
            return _adopt_service_browser(cfg, rows)
        if _connected(rows, cfg['browser']):
            _owned_profile_pid(cfg)
            return True, ''
        if profile_pids(cfg['user_data_dir']):
            return False, ('BrowserSkill unavailable: profile is running, but BrowserSkill '
                           'is not connected.')
        _chrome(cfg)
        return True, ''
    except (OSError, ValueError, RuntimeError) as exc:
        return False, 'BrowserSkill unavailable: ' + str(exc)

def _fixture_read_only_browser(settings=None) -> tuple[bool, str]:
    """At the real Session thread hop, NEVER recover a missing daemon/Chrome."""
    from tools.browser_skill import Unavailable, _sync

    try:
        cfg = _config(settings)  # Only the existing Session-owned pinned selection.
        if cfg['engine'] != 'browser_skill' or not cfg['browser']:
            return False, ('BrowserSkill unavailable: select the pinned BrowserSkill '
                           'instance using browser-setup; fixture cannot start Chrome.')
        _sync('status', timeout=7)  # Observe; never call ensure_daemon().
        rows = _browsers()
        matches = [r for r in rows if r.get('instance_id') == cfg['browser']
                   and not r.get('unresponsive') and not r.get('version_skew')]
        directory = Path(cfg['user_data_dir']).expanduser().resolve()
        pids = profile_pids(cfg['user_data_dir'])
        if not pids:
            # Empty extension rows can mean either no extension or a swallowed
            # BrowserSkill enumeration failure; only a complete process scan can
            # distinguish an absent dedicated Chrome from an unrelated profile.
            for _, args in _processes():
                values = [arg.partition('=')[2] if arg.startswith('--user-data-dir=') else (
                    args[i + 1] if i + 1 < len(args) else '')
                    for i, arg in enumerate(args)
                    if arg.startswith('--user-data-dir=') or arg == '--user-data-dir']
                if any(value and Path(value).expanduser().resolve() == directory
                       for value in values):
                    return False, ('BrowserSkill unavailable: pinned extension is absent, '
                                   'disconnected or ambiguous; reconnect it and rerun fixture.')
            if len(matches) > 1:
                return False, ('BrowserSkill unavailable: pinned extension is absent, '
                               'disconnected or ambiguous; reconnect it and rerun fixture.')
            if matches:
                return False, ('BrowserSkill unavailable: pinned extension reports connected, '
                               'but its dedicated Chrome process was not independently observed.')
            if _config(settings) != cfg:
                return False, ('BrowserSkill unavailable: pinned browser configuration changed; '
                               'inspect the selection and rerun fixture.')
            return False, ('BrowserSkill unavailable: dedicated Chrome not running; the '
                           'responsible orchestrator must follow AGENTS.md → Dedicated '
                           'application Chrome health.')
        if len(pids) != 1:
            return False, ('BrowserSkill unavailable: dedicated Chrome process is ambiguous '
                           'for pinned user-data-dir; inspect the profile and rerun fixture.')
        if len(matches) != 1:
            return False, ('BrowserSkill unavailable: pinned extension is absent, '
                           'disconnected or ambiguous; reconnect it and rerun fixture.')
        _owned_profile_pid(cfg)  # One independently observed canonical Chrome argv.
        if _config(settings) != cfg:
            return False, ('BrowserSkill unavailable: pinned browser configuration changed; '
                           'inspect the selection and rerun fixture.')
        return True, ''
    except (Unavailable, OSError, ValueError, RuntimeError) as exc:
        return False, ('BrowserSkill unavailable: read-only fixture preflight failed ('
                       f'{type(exc).__name__}); check bsk doctor, verify one owned '
                       'Chrome and rerun fixture.')


def ensure_browser(settings=None) -> tuple[bool, str]:
    # A private per-invocation ContextVar arrives here through Session.__aenter__
    # -> asyncio.to_thread(ensure_browser), before session start. Without exact
    # daemon origin/authority, keep the ordinary production recovery unchanged.
    from tools.browser_skill_fixture import _read_only_entry
    if _read_only_entry():
        return _fixture_read_only_browser(settings)
    from tools.browser_skill import ensure_daemon
    try:
        cfg = _config(settings)
        if not cfg['browser']:
            return False, 'BrowserSkill unavailable: run ./appliedin browser-setup first.'
        ready, problem = ensure_daemon()
        if not ready:
            return False, problem
        rows = _browsers()
        if _in_user_service():
            return _adopt_service_browser(cfg, rows)
        if _connected(rows, cfg['browser']):
            _owned_profile_pid(cfg)
            return True, ''
        with _launch_lock(cfg['user_data_dir']):
            rows = _browsers()
            if _connected(rows, cfg['browser']):
                _owned_profile_pid(cfg)
                return True, ''
            if profile_pids(cfg['user_data_dir']):
                return False, ('BrowserSkill unavailable: profile is running, but BrowserSkill '
                               'is not connected. No second Chrome was launched.')
            before = _ids(rows)
            launched = _launch(cfg, DASHBOARD)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                rows = _browsers()
                if _connected(rows, cfg['browser']):
                    _owned_profile_pid(cfg, launched)
                    return True, ''
                if _ids(rows) - before - {cfg['browser']}:
                    return False, ('BrowserSkill unavailable: a different instance connected; '
                                   'the pinned instance was not used. Run browser-setup.')
                time.sleep(1)
            return False, ('BrowserSkill unavailable: dedicated Chrome started, but the pinned '
                           'BrowserSkill instance did not connect within 45 seconds.')
    except (OSError, ValueError, RuntimeError) as exc:
        return False, 'BrowserSkill unavailable: ' + str(exc)

def connection_status(settings=None) -> dict:
    """Read-only diagnostics: observe a real process, never launch on status."""
    global _STATUS_CACHE
    try:
        cfg = _config(settings)
        key = (cfg['browser'], cfg['user_data_dir'], cfg['chrome_path'], os.getenv('BSK_HOME'))
        with _STATUS_LOCK:
            rows = _browsers() if cfg['browser'] else []
            if not cfg['browser']:
                state, detail = 'setup_required', 'Run ./appliedin browser-setup for the clean profile.'
            elif _connected(rows, cfg['browser']):
                if _in_user_service():
                    ready, problem = _adopt_service_browser(cfg, rows)
                    if not ready:
                        return {'state': 'unavailable', 'detail': problem}
                else:
                    _owned_profile_pid(cfg)
                state, detail = 'connected', 'Pinned extension and dedicated Chrome argv observed.'
            elif profile_pids(cfg['user_data_dir']):
                matching = [row for row in rows if row.get('instance_id') == cfg['browser']]
                if len(matching) == 1 and matching[0].get('version_skew'):
                    _owned_profile_pid(cfg)
                    state, detail = 'version_skew', (
                        'bsk update, then bsk daemon stop; the next bsk call starts the new daemon')
                else:
                    state, detail = 'extension_missing', (
                        'Dedicated Chrome is running but pinned BrowserSkill is not connected.')
            else:
                _chrome(cfg)
                state, detail = 'not_running', (
                    'Dedicated Chrome is not running; the orchestrator must restore it '
                    'outside the daemon service cgroup.' if _in_user_service() else
                    'Dedicated Chrome is not running; it will start for the next browser task.')
            result = {'state': state, 'detail': detail}
            _STATUS_CACHE = (key, time.monotonic(), result)
            return dict(result)
    except (OSError, ValueError, RuntimeError) as exc:
        return {'state': 'unavailable', 'detail': str(exc)}

def _setup_selection_unchanged(initial: dict, executable: str, settings=None) -> None:
    """Interactive approval cannot overwrite a concurrent change to the pinned selection."""
    current = _config(settings)
    for key in ('browser', 'engine', 'user_data_dir', 'chrome_path'):
        if current[key] != initial[key]:
            raise ValueError(
                f'{key} changed during browser-setup; keep the new selection and explicitly rerun setup.')
    if _chrome(current) != executable:
        raise ValueError('Chrome executable changed during browser-setup; verify it and rerun setup.')


def _save_instance(instance: str, directory: str, settings=None, *,
                   initial: dict | None = None, executable: str = '') -> None:
    from core.config import get_settings
    from tools.browser_runtime import _config_path
    settings = settings or get_settings()
    path = _config_path(settings)
    # Re-read immediately before saving, including the pin and engine even when
    # the directory did not change while the human inspected the extension.
    if initial is not None:
        _setup_selection_unchanged(initial, executable, settings)
    elif _config(settings)['user_data_dir'] != directory:
        raise ValueError('user_data_dir changed during setup; instance was not saved')
    values = yaml.safe_load(path.read_text()) if path.exists() else {}
    values = values or {}
    values.update(engine='browser_skill', browser=instance)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.browser-setup-',
                                     delete=False) as handle:
        temporary = Path(handle.name)
        yaml.safe_dump(values, handle, sort_keys=False)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def browser_setup(settings=None, *, timeout_s: int = 900) -> dict:
    """Verify an existing ID or explicitly pin the unique owned Chrome extension."""
    from tools.browser_skill import ensure_daemon
    cfg = _config(settings)
    # A retained pin does not authorize an implicit backend switch. Cold setup
    # with no saved ID still allows the existing explicit human-confirmed
    # transition to BrowserSkill when _save_instance() runs.
    if cfg['browser'] and cfg['engine'] != 'browser_skill':
        return {'status': 'blocked', 'saved': False,
                'error': ('The saved browser ID is not the selected apply engine '
                          '(engine is not browser_skill). Explicitly select '
                          'engine: browser_skill in browser.local.yaml and rerun '
                          'browser-setup; no profile or pin was changed.')}
    try:
        executable = _chrome(cfg)
    except (OSError, ValueError) as exc:
        return {'status': 'blocked', 'error': f'{exc}; configure Chrome and rerun setup.'}
    ready, problem = ensure_daemon()
    if not ready:
        return {'status': 'blocked', 'error': problem or 'Inspect bsk doctor and rerun setup.'}

    def connected_id(rows, instance):
        return (sum(1 for r in rows if r.get('instance_id') == instance
                    and not r.get('unresponsive') and not r.get('version_skew')) == 1)

    # A pre-existing connected ID is not "new"; waiting for _ids(rows)-before
    # used to time out on valid reruns. A saved pin additionally proves ownership.
    rows = _browsers()
    if cfg['browser'] and connected_id(rows, cfg['browser']):
        try:
            _owned_profile_pid(cfg)
            _setup_selection_unchanged(cfg, executable, settings)
        except (OSError, ValueError, RuntimeError) as exc:
            return {'status': 'blocked', 'error': f'{exc}; inspect the owned Chrome and rerun setup.'}
        return {'status': 'ready', 'browser': cfg['browser'],
                'user_data_dir': cfg['user_data_dir'], 'saved': False}

    with _launch_lock(cfg['user_data_dir']):
        launched = None
        try:
            if not profile_pids(cfg['user_data_dir']):
                launched = _launch(cfg, WEB_STORE)
            else:
                _owned_profile_pid(cfg)
        except (OSError, ValueError) as exc:
            return {'status': 'blocked', 'error': f'{exc}; verify the dedicated profile and rerun setup.'}
        print('Dedicated Chrome profile: ' + cfg['user_data_dir'], flush=True)
        print('Verify BrowserSkill in this dedicated profile yourself; enable file-URL access. '
              'No extension installation, sign-in or consent is automated.', flush=True)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            rows = _browsers()
            ids = _ids(rows)
            if cfg['browser']:
                if connected_id(rows, cfg['browser']):
                    try:
                        _owned_profile_pid(cfg, launched)
                        _setup_selection_unchanged(cfg, executable, settings)
                    except (OSError, ValueError, RuntimeError) as exc:
                        return {'status': 'blocked',
                                'error': f'{exc}; verify the pinned profile and rerun setup.'}
                    return {'status': 'ready', 'browser': cfg['browser'],
                            'user_data_dir': cfg['user_data_dir'], 'saved': False}
                if ids - {cfg['browser']}:
                    return {'status': 'blocked', 'error': (
                        'Another BrowserSkill instance is visible but not the saved pin. '
                        'Reconnect the pinned extension or correct configuration explicitly; rerun setup.')}
            else:
                if len(ids) > 1:
                    return {'status': 'blocked', 'error': (
                        'Multiple BrowserSkill instances are visible; inspect the dedicated '
                        'Chrome extension and rerun setup without ambiguous candidates.')}
                if len(ids) == 1:
                    instance = next(iter(ids))
                    if not connected_id(rows, instance):
                        time.sleep(1)
                        continue
                    try:
                        _owned_profile_pid(cfg, launched)
                    except (OSError, ValueError, RuntimeError) as exc:
                        return {'status': 'blocked',
                                'error': f'{exc}; verify one owned Chrome and rerun setup.'}
                    print('Candidate extension ID: ' + instance, flush=True)
                    answer = input(
                        'Verify this ID in the dedicated Chrome extension popup and consent to pin [y/N]: '
                    ).strip().lower()
                    if answer != 'y':
                        return {'status': 'not confirmed', 'saved': False,
                                'hint': 'Verify the dedicated extension and explicitly rerun setup.'}
                    if not connected_id(_browsers(), instance):
                        return {'status': 'blocked', 'error': (
                            'The candidate extension disconnected or became ambiguous; '
                            'reconnect it and rerun setup.')}
                    try:
                        _owned_profile_pid(cfg, launched)
                        _save_instance(instance, cfg['user_data_dir'], settings,
                                       initial=cfg, executable=executable)
                    except (OSError, ValueError, RuntimeError) as exc:
                        return {'status': 'blocked', 'error': (
                            f'{exc}; inspect the saved selection and explicitly rerun setup.')}
                    return {'status': 'configured', 'browser': instance,
                            'user_data_dir': cfg['user_data_dir']}
            time.sleep(1)
    return {'status': 'waiting timed out', 'saved': False,
            'hint': ('No unique responsive pinned extension was proven within the deadline. '
                     'Inspect/reconnect BrowserSkill in the owned profile and rerun setup.')}
