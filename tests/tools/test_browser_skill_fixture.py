"""Offline adversarial proof of the daemon-exclusive synthetic BrowserSkill fixture.

No test opens a socket, starts Chrome/BSK, reaches owner stores or queues an apply.
The eventual native-positive primary-checkout traversal is operator-only.
"""
from __future__ import annotations

import builtins
import io
import json
import os
import subprocess
import sys
import threading
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools import browser_profile as profile
from tools import browser_skill as bsk
from tools import browser_skill_apply as forms
from tools import browser_skill_fixture as fixture


def _assert_handler_socket_timeout():
    from socketserver import StreamRequestHandler

    calls = []
    connection = SimpleNamespace(
        settimeout=lambda seconds: calls.append(seconds),
        makefile=lambda *_args: io.BytesIO())
    handler = object.__new__(fixture._FixtureHandler)
    handler.request = connection
    StreamRequestHandler.setup(handler)
    assert calls == [2]
    assert fixture._FixtureHandler.timeout == 2


def test_handler_applies_timeout_to_accepted_connection(monkeypatch):
    """StreamRequestHandler, not HTTPServer.timeout, bounds the accepted read."""
    _assert_handler_socket_timeout()
    with monkeypatch.context() as removed:
        removed.delattr(fixture._FixtureHandler, 'timeout')
        with pytest.raises(AssertionError):
            _assert_handler_socket_timeout()
    _assert_handler_socket_timeout()


def _assert_scenario_teardown(monkeypatch, capsys, *, alive, reader=None):
    events = []

    class BodyStopped(Exception):
        pass

    class Server:
        def __init__(self, *_args):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def serve_forever(self):
            raise AssertionError('fake thread must never serve or bind a socket')
        def shutdown(self):
            events.append('shutdown')
        def server_close(self):
            events.append('server_close')

    class Thread:
        def __init__(self, **_kwargs):
            pass
        def start(self):
            pass
        def join(self, timeout):
            assert timeout == 3
            events.append('join')
        def is_alive(self):
            return alive

    async def stop_body(*_args, **_kwargs):
        raise BodyStopped()

    with monkeypatch.context() as fake:
        fake.setattr(fixture, 'HTTPServer', Server)
        fake.setattr(threading, 'Thread', Thread)
        fake.setattr(fixture, '_daemon_origin', lambda: True)
        fake.setattr(fixture._SyntheticRun, 'enable_positive_fixture', lambda self: None)
        fake.setattr(forms, 'apply', stop_body)
        if reader is not None:
            reader.__globals__.update(_daemon_origin=fixture._daemon_origin,
                                      HTTPServer=Server)
            fake.setattr(fixture, '_run_scenario', reader)
        with pytest.raises(Exception) as raised:
            fixture.run()
    assert events == ['shutdown', 'server_close', 'join']
    if alive:
        assert type(raised.value) is RuntimeError
        assert str(raised.value) == 'Synthetic fixture harness server did not stop.'
    else:
        assert type(raised.value) is BodyStopped
    # run() must not turn an incomplete scenario/teardown into aggregate PASS.
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('guard,alive', [
    ('            if thread.is_alive():\n'
     "                raise RuntimeError('Synthetic fixture harness server did not stop.')", True),
    ('            server.server_close()', False),
])
def test_scenario_teardown_guards_and_removal_sensitivity(monkeypatch, capsys, guard, alive):
    """Each actual teardown guard is sensitive without a listener or live thread."""
    import inspect

    _assert_scenario_teardown(monkeypatch, capsys, alive=alive)
    source = inspect.getsource(fixture._run_scenario)
    assert source.count(guard) == 1, 'counterfactual must remove exactly its own guard'
    namespace = dict(fixture.__dict__)
    exec(compile(source.replace(guard, ''), '<teardown-guard-removed>', 'exec'), namespace)
    with pytest.raises(AssertionError):
        _assert_scenario_teardown(monkeypatch, capsys, alive=alive,
                                  reader=namespace['_run_scenario'])
    _assert_scenario_teardown(monkeypatch, capsys, alive=alive)


@pytest.fixture
def authority(tmp_path, monkeypatch):
    """A unit-test-only exact-origin stub; other tests check real CLI origin."""
    document = tmp_path / "Synthetic-Resume.pdf"
    document.write_bytes(b"%PDF-1.4\n% synthetic\n%%EOF\n")
    state = fixture._SyntheticRun(str(document))
    monkeypatch.setattr(fixture, "_daemon_origin", lambda: True)
    token = fixture._SESSION_ENTRY.set(state)
    try:
        yield state
    finally:
        fixture._SESSION_ENTRY.reset(token)


def test_sealed_fixture_http_route_cannot_be_spoofed_or_reused(authority):
    """Only the daemon-minted offline fixture may prepare over loopback HTTP."""
    forms._tracked_preparation_job(
        fixture.PK, fixture.COMPANY, fixture.URL, fixture_context=authority)
    with pytest.raises(forms.Gate, match='Synthetic fixture authority'):
        forms._tracked_preparation_job(
            fixture.PK, fixture.COMPANY, fixture.URL + '?other-job=1',
            fixture_context=authority)
    with pytest.raises(forms.Gate, match='Synthetic fixture authority'):
        forms._tracked_preparation_job(
            fixture.PK, fixture.COMPANY, fixture.URL,
            fixture_context=SimpleNamespace(row=authority.row))
    old_status = authority.row['status']
    authority.row['status'] = 'needs_human'
    try:
        with pytest.raises(forms.Gate, match='Synthetic tracked job/dispatch'):
            forms._tracked_preparation_job(
                fixture.PK, fixture.COMPANY, fixture.URL, fixture_context=authority)
    finally:
        authority.row['status'] = old_status



class _ServedFormInventory(HTMLParser):
    """Project fixture form groups the same way PAGE exposes control questions."""
    def __init__(self):
        super().__init__()
        self.div_depth = 0
        self.groups = []
        self.label_group = None
        self.label_parts = []
        self.controls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            self.div_depth += 1
            if {'form-group', 'phone-input__country'} & set(attrs.get('class', '').split()):
                self.groups.append({"depth": self.div_depth, "question": ""})
        group = self.groups[-1] if self.groups else None
        if (attrs.get('data-automation-id') == 'formLabel' or attrs.get('id') == 'country-label') and group:
            self.label_group = group
            self.label_parts = []
        typ = attrs.get("role") or (
            attrs.get("type", "text") if tag == "input" else tag)
        if group and (tag in {"input", "textarea", "select", "button"}
                      or attrs.get("role") == "combobox"):
            selector = "#" + attrs["id"] if attrs.get("id") else tag
            self.controls.append({
                "selector": selector, "name": attrs.get("name"),
                "label": group["question"] or attrs.get("name", ""),
                "question": group["question"],
                "type": typ, "tag": tag, "required": False,
                "in_form": True, "form_selector": "#application-form",
                "form_action": fixture.URL,
            })

    def handle_data(self, data):
        if self.label_group is not None:
            self.label_parts.append(data)

    def handle_endtag(self, tag):
        if self.label_group is not None:
            self.label_group["question"] = " ".join(
                " ".join(self.label_parts).split())
            self.label_group = None
            self.label_parts = []
        if tag == "div":
            if self.groups and self.groups[-1]["depth"] == self.div_depth:
                self.groups.pop()
            self.div_depth -= 1


def served_fixture_page():
    """Keep the predecessor native widget snapshot as an offline regression."""
    inventory = _ServedFormInventory()
    inventory.feed(fixture._closed_form().decode('utf-8'))
    return {"url": fixture.URL, "controls": inventory.controls}



def synthetic_page(*, url=fixture.URL, receiver=fixture.URL, extra_granted=False):
    def item(selector, label, typ="text", tag="input", **attrs):
        return {"selector": selector, "label": label, "question": "",
                "type": typ, "tag": tag, "required": False,
                "disabled": False, "has_value": False, "value": "",
                "submit": typ == "submit", "in_form": True,
                "form_selector": "#synthetic-form", "form_action": receiver,
                'native_form_owner_selector': '#synthetic-form',
                "formaction": "", "files": [], **attrs}
    controls = [
        item("#name", "Name"),
        item("#additional", "Additional question", typ="textarea", tag="textarea"),
        item("#submit", "Submit application", typ="submit", tag="button"),
    ]
    return {"url": url, "title": "Synthetic role", "text": "Synthetic role " * 40,
            "controls": controls, "truncated": False, "unsupported_frames": [],
            "shadow_roots": False, "opaque_controls": False,
            "inventory_verified": True}


def _source_denied(*_a, **_kw):
    pytest.fail("Fixture reached forbidden production state, secret or browser action")


def test_no_fixture_on_library_import_or_untrusted_python_objects(monkeypatch):
    import daemon
    assert daemon.log and daemon.make_stores and daemon.is_internal_pk
    assert callable(daemon.main) and callable(daemon._recover_orphans)
    assert fixture._SESSION_ENTRY.get() is None
    assert not fixture._read_only_entry()
    for bogus in (True, 1, "fixture", {"fixture_context": True},
                  SimpleNamespace(pk=fixture.PK), fixture._SyntheticRun("/fake/pdf")):
        assert not fixture.authorized(bogus, pk=fixture.PK,
                                      company=fixture.COMPANY, url=fixture.URL)
    with pytest.raises(RuntimeError, match="exclusive daemon"):
        fixture.run()


@pytest.mark.parametrize("context", [True, {"url": fixture.URL}, "synthetic-fixture",
                                      SimpleNamespace(pk=fixture.PK)])
async def test_forged_context_refused_before_store_or_browser(monkeypatch, context):
    monkeypatch.setattr(bsk, "Session", _source_denied)
    monkeypatch.setattr("core.stores.make_stores", _source_denied)
    with pytest.raises(forms.Gate, match="authority"):
        await forms.apply(fixture.URL, fixture.COMPANY, {"Name": "Test User"},
                          "synthetic-fixture", pk=fixture.PK,
                          resume_path="/fake/pdf", fixture_context=context)


def test_served_form_hidden_machine_input_does_not_collide_with_approval_question():
    """Hidden receiver fields share group text but are not answerable controls."""
    import runpy
    # Static HTML parsing cannot prove proxy omission; use the production PAGE.
    helpers = runpy.run_path(str(Path(__file__).with_name('test_browser_phone_block.py')))
    page = helpers['served_fixture_dom']()['page']
    location = next(c for c in page["controls"] if c["selector"] == "#location")
    hidden = next(c for c in page["controls"]
                  if c["selector"] == "#location-code")
    assert location["question"] == hidden["question"] == "Where are you located?"
    # Two required validation proxies (location and phone country) are
    # independent actual values, not historical per-control grants.
    anonymous = [c for c in page["controls"]
                 if c.get("type") == "text" and not c.get("label")
                 and not c.get("name") and c.get("required") is True]
    assert len(anonymous) == 2
    assert len({c["selector"] for c in anonymous}) == 2
    assert all(c.get("in_form") is True
               and c.get("form_selector") == location["form_selector"]
               and not c.get("has_value") and c.get("value") == ""
               for c in anonymous)
    assert hidden["type"] == "hidden" and hidden["name"] == "location_value"
    assert location["question"] == "Where are you located?"
    # No historical approval can make this opaque inventory pass Submit:
    # each required proxy still needs a current nonempty native readback.
    assert page["inventory_verified"] is False and page["opaque_controls"] is True
    with pytest.raises(forms.Gate, match="incomplete"):
        forms.check_form(page, {}, uploaded=True)
    for proxy in anonymous:
        # Provide the exact current row-PDF attachment so the final readback
        # reaches this required proxy rather than stopping at its PDF check.
        resume = {"selector": "#synthetic-resume", "type": "file",
                  "label": "Resume", "disabled": False, "files": ["Synthetic-Resume.pdf"]}
        probe = {**page, "controls": [proxy, resume], "inventory_verified": True,
                 "truncated": False, "opaque_controls": False,
                 "shadow_roots": False, "unsupported_frames": []}
        with pytest.raises(forms.Gate, match="required application field is empty"):
            forms.check_form(probe, {}, uploaded=True, attachment="Synthetic-Resume.pdf")


def test_served_form_does_not_add_an_unlinked_caption_hidden_value():
    page = served_fixture_page()
    assert 'data-label-input="work-authorization-caption"' not in fixture._FORM.decode()
    assert not any(control["selector"] == "#work-authorization-caption"
                   for control in page["controls"])

async def test_fixture_orders_combos_before_the_ungranted_gate(authority):
    page = served_fixture_page()
    selectors = [control["selector"] for control in page["controls"]]
    combo_selectors = list(fixture._CHOICE_SELECTORS.values())
    assert selectors.index("#name") < min(selectors.index(s) for s in combo_selectors)
    assert max(selectors.index(s) for s in combo_selectors) < selectors.index("#additional")

    authority.enable_positive_fixture()
    actions = [
        await authority.decision("", page, [], model="synthetic-fixture")
        for _ in range(8)
    ]
    assert [action["selector"] for action in actions] == [
        "#name", *combo_selectors, "#phone", "#additional"]


def test_fixture_reports_untouched_combos_as_not_reached():
    state = fixture._SyntheticRun("/synthetic/resume.pdf")
    page = served_fixture_page()
    state.observe_page(page)
    assert all(item["outcome"] == "not_reached"
               for item in state.combo_observations.values())

    key = "Where are you located?"
    state.combo_touched.add(key)
    state.observe_page(page)
    assert state.combo_observations[key]["outcome"] == "not_committed"



@pytest.mark.parametrize("flag,value", [
    ("inventory_verified", False), ("truncated", True),
    ("opaque_controls", True), ("shadow_roots", True),
    ("unsupported_frames", ["http://127.0.0.1/frame"]),
])
async def test_incomplete_inventory_diagnostics_never_replace_guard_input(
        authority, flag, value):
    # A second read can see a settled DOM; that must not authorize the rejected
    # original inventory or hide the flags that actually caused the gate.
    page = synthetic_page()
    page[flag] = value
    diagnostic = {"ready_state": "complete", "offending_elements": [
        {"selector": "#synthetic-opaque", "tag": "div", "role": "listbox"}],
        "shadow_hosts": []}
    calls = []
    class Raw:
        async def navigate(self, url):
            return page
        async def call(self, *args):
            calls.append(args)
            return {"value": diagnostic}
    edge = authority.facade(Raw())
    observed = await edge.navigate(fixture.URL)
    assert observed is page and authority.last_page is page
    assert observed[flag] == value
    assert authority.inventory_diagnostics == diagnostic
    assert calls == [("evaluate", fixture._inventory_diagnostic_script())]
    with pytest.raises(forms.Gate, match="inventory is incomplete|uninspectable components"):
        forms.check_form(observed, {}, uploaded=True)
    assert authority.fills == authority.ipc_index == 0



def test_fixture_commit_state_comes_from_visible_and_hidden_page_readback():
    state = fixture._SyntheticRun("/synthetic/resume.pdf")
    state.combo_touched.update(fixture._MACHINE_VALUES)
    page = served_fixture_page()
    for key, selector in fixture._COMBO_SELECTORS.items():
        control = next(c for c in page["controls"] if c["selector"] == selector)
        control["combobox"] = {
            "selected_text": fixture._FACTS[key],
            'displayed_text': fixture._FACTS[key],
            "selected_value": fixture._MACHINE_VALUES[key],
            "hidden_selector": fixture._HIDDEN_SELECTORS[key],
            "hidden_value": fixture._MACHINE_VALUES[key],
        }
    state.observe_page(page)
    assert state.committed == {
        key: (fixture._FACTS[key], fixture._MACHINE_VALUES[key])
        for key in fixture._MACHINE_VALUES
    }
    assert all(item["outcome"] == "committed"
               for key, item in state.combo_observations.items() if key in fixture._MACHINE_VALUES)

    location = next(c for c in page["controls"] if c["selector"] == "#location")
    location["combobox"]["hidden_value"] = "OTHER_CITY"
    state.observe_page(page)
    assert "Where are you located?" not in state.committed
    assert state.combo_observations["Where are you located?"]["reason"] == (
        "hidden machine value did not match the selected value")



async def test_closed_facade_denies_all_extra_ipc_below_controller(authority):
    forwarded = []
    class Raw:
        async def navigate(self, url):
            forwarded.append(("navigate", url))
            return synthetic_page()
        async def page(self):
            forwarded.append(("evaluate", "PAGE"))
            return synthetic_page()
        async def call(self, *args, **kw):
            forwarded.append(tuple(args))
            return {}
    edge = authority.facade(Raw())
    assert not hasattr(edge, "__dict__") and not hasattr(edge, "session")
    await edge.navigate(fixture.URL)
    await edge.page()
    await edge.call("fill", "#name", "--value", "Test User")
    for args in [
        ("click", "#submit"), ("click", "#continue"), ("click", "#other"),
        ("upload", "#resume", "--file", "Synthetic-Resume.pdf"),
        ("select", "#other", "--value", "Yes"), ("choose", "#other"),
        ("evaluate", "alert('synthetic')"), ("fill", "#additional", "--value", "Synthetic answer"),
        ("fill", "#name", "--value", "Test User"),
        ("fill", "#name", "--value", "Altered"),
    ]:
        with pytest.raises(PermissionError, match="denies"):
            await edge.call(*args)
    for url in ("http://localhost:18789/fixture/example-co/job/1",
                fixture.URL + "?source=apply", fixture.URL + "/other", fixture.URL):
        with pytest.raises(PermissionError, match="one navigate"):
            await edge.navigate(url)
    assert forwarded == [
        ("navigate", fixture.URL), ("evaluate", "PAGE"),
        ("fill", "#name", "--value", "Test User"),
    ]


def test_fixture_handler_rejects_wrong_host_path_and_every_non_get_method():
    # Construct BaseHTTPRequestHandler without a socket or network transport.
    def request(method, path, host):
        handler = object.__new__(fixture._FixtureHandler)
        handler.path = path
        handler.command = method
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.headers = {"Host": host, "Content-Length": "0"}
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.server = SimpleNamespace(synthetic_run=None)
        captured = []
        handler.send_response = lambda status: captured.append(("status", status))
        handler.send_header = lambda key, value: captured.append((key, value))
        handler.end_headers = lambda: None
        getattr(handler, "do_" + method)()
        return captured, handler.wfile.getvalue()
    good, body = request("GET", fixture._PATH, "127.0.0.1:18789")
    assert ("status", 200) in good
    assert b'id="_systemfield_name"' in body and b'id="additional"' in body
    assert b'ashby-application-form-submit-button' in body and b'<form' not in body
    assert body.count(b'<iframe') == 3 and b'type="submit"' not in body
    assert b'<div><iframe style="display:none"></iframe></div>' in body
    for method, path, host in [
        ("GET", fixture._PATH, "localhost:18789"),
        ("GET", fixture._PATH, "[::1]:18789"),
        ("GET", fixture._PATH + "?redirect=/", "127.0.0.1:18789"),
        ("GET", "/other", "127.0.0.1:18789"),
        ("POST", fixture._PATH, "127.0.0.1:18789"),
        ("HEAD", fixture._PATH, "127.0.0.1:18789"),
        ("PUT", fixture._PATH, "127.0.0.1:18789"),
        ("DELETE", fixture._PATH, "127.0.0.1:18789"),
        ("PATCH", fixture._PATH, "127.0.0.1:18789"),
        ("OPTIONS", fixture._PATH, "127.0.0.1:18789"),
        ("CONNECT", fixture._PATH, "127.0.0.1:18789"),
        ("TRACE", fixture._PATH, "127.0.0.1:18789"),
    ]:
        headers, response = request(method, path, host)
        expected = 404 if method == "GET" else 405
        assert ("status", expected) in headers
        assert not response and not any(key.lower() == "location" for key, _ in headers)


def test_fixture_records_post_but_always_refuses_with_405():
    state = fixture._SyntheticRun("/synthetic/resume.pdf")
    handler = object.__new__(fixture._FixtureHandler)
    handler.path = fixture._PATH
    handler.command = "POST"
    handler.requestline = f"POST {fixture._PATH} HTTP/1.1"
    handler.headers = {"Host": "127.0.0.1:18789", "Content-Length": "0",
                       "Content-Type": "multipart/form-data; boundary=synthetic"}
    handler.rfile = io.BytesIO(b"an otherwise plausible form body")
    handler.wfile = io.BytesIO()
    handler.server = SimpleNamespace(synthetic_run=state)
    statuses = []
    handler.send_response = statuses.append
    handler.send_header = lambda *_args: None
    handler.end_headers = lambda: None
    handler.do_POST()
    assert statuses == [405] and not handler.wfile.getvalue()
    assert state.post_attempts == 1 and state.post_count == 0
    assert state.post_rejection == (
        "Synthetic fixture refuses every non-GET form method.")
    assert state.post_receipts[0]["response_status"] == 405



async def test_real_session_thread_context_and_read_only_connected_path(
        authority, monkeypatch):
    cfg = {"engine": "browser_skill", "browser": "synthetic-pin",
           "user_data_dir": "/synthetic/owned-chrome", "chrome_path": "/synthetic/chrome"}
    observed = {"worker_thread": False, "context": False}
    original = profile._fixture_read_only_browser
    def spy(settings=None):
        observed["worker_thread"] = threading.current_thread() is not threading.main_thread()
        observed["context"] = fixture._SESSION_ENTRY.get() is authority
        return original(settings)
    monkeypatch.setattr(profile, "_fixture_read_only_browser", spy)
    monkeypatch.setattr(profile, "_config", lambda *_a, **_kw: cfg)
    monkeypatch.setattr("tools.browser_runtime.configuration", lambda *_a, **_kw: cfg)
    monkeypatch.setattr(profile, "profile_pids", lambda *_a: [123])
    def sync(*args, **kwargs):
        if args[0] == "status":
            return {"browsers": [{"instance_id": "synthetic-pin"}]}
        return {"browsers": [{"instance_id": "synthetic-pin"}]}
    monkeypatch.setattr(bsk, "_sync", sync)
    monkeypatch.setattr(bsk, "ensure_daemon", _source_denied)
    monkeypatch.setattr(profile, "_launch", _source_denied)
    monkeypatch.setattr(profile, "_launch_lock", _source_denied)
    monkeypatch.setattr(profile.subprocess, "Popen", _source_denied)
    calls = []
    async def command(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("session", "start"):
            return {"session_id": "synthetic-session",
                    "browser_instance_id": "synthetic-pin"}
        if args[0] == "evaluate":
            return {"value": synthetic_page()}
        return {}
    monkeypatch.setattr(bsk, "command", command)
    async with bsk.Session("apply") as raw:
        # A connected real apply Session can read its original tab. Neither
        # the fixture facade nor the raw producer may mint write/navigation
        # permission merely because the thread and browser pin are valid.
        edge = authority.facade(raw)
        observed_page = await raw.page()
        assert observed_page["url"] == fixture.URL
        with pytest.raises(bsk.Unavailable, match="Unmediated application IPC"):
            await edge.navigate(fixture.URL)
        with pytest.raises(bsk.Unavailable, match="Raw application IPC refused"):
            await raw.call("fill", "#name", "--value", "Test User")
        with pytest.raises(bsk.Unavailable, match="Raw application IPC refused"):
            await raw.call("click", "#submit")
        with pytest.raises(bsk.Unavailable, match="Raw application IPC refused"):
            await raw.call("evaluate", "document.forms[0]?.requestSubmit()")
    from tools.browser_skill_dom import PAGE
    assert observed == {"worker_thread": True, "context": True}
    assert calls[0][:2] == ("session", "start")
    assert calls[-1] == ("session", "stop", "synthetic-session")
    assert ("evaluate", PAGE, "--session", "synthetic-session") in calls
    assert not any(c[0] in {"navigate", "fill", "click", "upload", "select"}
                   for c in calls)


@pytest.mark.parametrize("failure", [
    "daemon", "extension", "foreign", "ambiguous_extension",
    "missing_chrome", "ambiguous_chrome", "late_disconnect",
])
async def test_session_thread_never_autostarts_on_fixture_entry_race(
        authority, monkeypatch, failure, tmp_path):
    cfg = {"engine": "browser_skill", "browser": "synthetic-pin",
           "user_data_dir": str(tmp_path / "profile"), "chrome_path": "/synthetic/chrome"}
    monkeypatch.setattr(profile, "_config", lambda *_a, **_kw: cfg)
    monkeypatch.setattr("tools.browser_runtime.configuration", lambda *_a, **_kw: cfg)
    pids = [] if failure == "missing_chrome" else (
        [123, 456] if failure == "ambiguous_chrome" else [123])
    monkeypatch.setattr(profile, "profile_pids", lambda *_a: pids)
    def sync(*args, **kwargs):
        if failure == "daemon":
            raise bsk.Unavailable("Synthetic daemon disconnected")
        if args[0] == "status":
            return {"browsers": []}
        ids = (["foreign"] if failure == "foreign" else
               ["synthetic-pin", "synthetic-pin"] if failure == "ambiguous_extension" else
               [] if failure == "extension" else ["synthetic-pin"])
        return {"browsers": [{"instance_id": x} for x in ids]}
    monkeypatch.setattr(bsk, "_sync", sync)
    if failure == "late_disconnect":
        monkeypatch.setattr(bsk, "available", lambda: (False, "synthetic disconnect"))
    for target in ("_launch", "_launch_lock"):
        monkeypatch.setattr(profile, target, _source_denied)
    monkeypatch.setattr(bsk, "ensure_daemon", _source_denied)
    monkeypatch.setattr(profile.subprocess, "Popen", _source_denied)
    call = AsyncMock(side_effect=_source_denied)
    monkeypatch.setattr(bsk, "command", call)
    with pytest.raises(bsk.Unavailable):
        async with bsk.Session("apply"):
            pytest.fail("Unready fixture must not open any BSK Session")
    call.assert_not_awaited()
    assert not (tmp_path / "profile").exists()
    assert not list(tmp_path.glob("*.lock"))


def test_fixture_manual_jit_and_hold_evidence_never_needs_owner_store(
        authority, monkeypatch):
    monkeypatch.setattr("core.stores.make_stores", _source_denied)
    monkeypatch.setattr("tools.browser_apply._duplicate_refusal", _source_denied)
    monkeypatch.setattr("tools.claude_chrome._stage_resume", _source_denied)
    monkeypatch.setattr(forms.submit_hold, "blocked", _source_denied)
    monkeypatch.setattr(forms.submit_hold, "is_held", _source_denied)
    monkeypatch.setattr(forms.submit_hold, "mark", _source_denied)
    forms.check_dispatch(fixture.PK, authority.resume_path,
                         fixture_context=authority)
    # The retired per-field approval receipt is not an input to the existing
    # hold and current human-outcome checks below.
    forms.hold_possible_submission(
        fixture.PK, {"last_button": "Continue", "url": fixture.URL},
        fixture_context=authority)
    assert authority.is_held(fixture.PK) and authority.row["status"] == "needs_human"
    forms.check_before_committing_click(fixture.PK, fixture_context=authority)
    authority.row["status"] = "applied_manual"
    with pytest.raises(forms.Gate, match="Human outcome changed"):
        forms.check_before_committing_click(fixture.PK, fixture_context=authority)


def test_late_receiver_and_form_inventory_checks_are_nonvacuous():
    page = synthetic_page()
    page["controls"][0].update(required=True, value="Test User", has_value=True)
    page["controls"][1].update(required=True, value="Synthetic answer", has_value=True)
    resume = {"selector": "#resume", "type": "file", "tag": "input",
              "label": "Resume", "question": "", "in_form": True,
              "form_selector": "#synthetic-form", "form_action": fixture.URL,
              "formaction": "", "files": ["Synthetic-Resume.pdf"]}
    page["controls"].insert(2, resume)
    filled = {"#name": "Test User", "#additional": "Synthetic answer"}
    submit = page["controls"][-1]
    forms.check_form_destination(page, submit, fixture.URL)
    forms.check_form(page, filled, True, "Synthetic-Resume.pdf")
    altered = synthetic_page(receiver="https://foreign.example.test/collect")
    with pytest.raises(forms.Gate, match="form destination"):
        forms.check_form_destination(altered, altered["controls"][-1], fixture.URL)
    page["controls"][2]["files"] = []
    with pytest.raises(forms.Gate, match="Exactly one current résumé input"):
        forms.check_form(page, filled, True, "Synthetic-Resume.pdf")


def test_exact_daemon_fixture_cli_sanitizes_environment_before_any_product_import(
        tmp_path):
    """A fresh interpreter aborts at fixture import, before sockets or BSK start."""
    site = tmp_path / "sitecustomize.py"
    site.write_text('''
import builtins, json, os, socket
original = builtins.__import__
def deny(*a, **k):
    raise AssertionError("outbound socket is forbidden")
socket.create_connection = deny
socket.getaddrinfo = deny
socket.socket.connect = deny
def checked(name, *args, **kwargs):
    if name == "tools.browser_skill_fixture":
        print("FIXTURE_IMPORT_ENV=" + json.dumps({
            "dotenv": os.getenv("PYTHON_DOTENV_DISABLED"),
            "cost_map": os.getenv("LITELLM_LOCAL_MODEL_COST_MAP"),
            "autostart": os.getenv("BSK_AUTO_START"),
            "secret": os.getenv("SYNTHETIC_PROVIDER_SECRET"),
            "openai": os.getenv("OPENAI_API_KEY"),
            "arbitrary": os.getenv("APPLIEDIN_SECRET_SENTINEL"),
            "config_dir": os.getenv("APPLIEDIN_CONFIG_DIR"),
        }), flush=True)
        raise SystemExit(87)
    return original(name, *args, **kwargs)
builtins.__import__ = checked
''')
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env.update(PYTHONPATH=os.pathsep.join([str(tmp_path), str(root / "src")]),
               SYNTHETIC_PROVIDER_SECRET="synthetic-only-not-real",
               OPENAI_API_KEY="synthetic-only-not-real",
               APPLIEDIN_SECRET_SENTINEL="synthetic-only-not-real",
               APPLIEDIN_CONFIG_DIR=str(tmp_path / "safe-config"))
    result = subprocess.run(
        [sys.executable, "-m", "daemon", "--synthetic-browser-fixture"],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=25)
    assert result.returncode == 87, result.stderr
    marker = next(s.split("=", 1)[1] for s in result.stdout.splitlines()
                  if s.startswith("FIXTURE_IMPORT_ENV="))
    seen = json.loads(marker)
    assert seen == {"dotenv": "1", "cost_map": "True", "autostart": "0",
                    "secret": None, "openai": None, "arbitrary": None,
                    "config_dir": str(tmp_path / "safe-config")}


def test_normal_daemon_library_import_and_entry_keep_old_import_boundary(tmp_path):
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(root / "src"),
               LITELLM_LOCAL_MODEL_COST_MAP="True")
    source = ("import daemon; assert daemon.log and daemon.make_stores "
              "and daemon.is_internal_pk and daemon.main and daemon._worker_loop")
    result = subprocess.run(
        [sys.executable, "-c", source], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr



def test_fresh_daemon_synthetic_cli_fails_closed_on_unwitnessed_page(
        tmp_path):
    """The daemon refuses incomplete offline BSK evidence before any form write.

    sitecustomize runs before daemon imports. Its fake BSK never completes the
    linked-control witness, and no socket or owner-like file may be reached.
    """
    from textwrap import dedent

    root = Path(__file__).resolve().parents[2]
    home = tmp_path / "synthetic-home"
    home.mkdir()
    config = home / "config"
    config.mkdir()
    (config / "browser.local.yaml").write_text(
        "engine: browser_skill\nbrowser: synthetic-pin\n"
        f"user_data_dir: {str(home / 'owned-chrome')}\n"
        "chrome_path: /synthetic/chrome\n"
    )
    forbidden = [home / ".env", home / ".local" / "answer-bank.json",
                 home / "resume" / "base.tex", home / "output" / "candidate.pdf",
                 config / "watchlist.yaml", config / "preferences.yaml"]
    for name in forbidden:
        name.parent.mkdir(parents=True, exist_ok=True)
        name.write_text("synthetic forbidden sentinel; never read")
    log = tmp_path / "child-records.jsonl"
    site = tmp_path / "sitecustomize.py"
    site.write_text(dedent(r'''
        import asyncio
        import builtins
        import http.server
        import json
        import os
        import shutil
        import socket
        import subprocess
        import sys
        from pathlib import Path
        from types import SimpleNamespace

        _logfile = os.environ["SYNTHETIC_TEST_LOG"]
        _synthetic_home = Path(os.environ["HOME"])
        _forbidden = {
            str(_synthetic_home / ".env"),
            str(_synthetic_home / ".local" / "answer-bank.json"),
            str(_synthetic_home / "resume" / "base.tex"),
            str(_synthetic_home / "output" / "candidate.pdf"),
            str(_synthetic_home / "config" / "watchlist.yaml"),
            str(_synthetic_home / "config" / "preferences.yaml"),
        }

        def event(kind, **kwargs):
            with open(_logfile, "a", encoding="utf-8") as output:
                output.write(json.dumps({"kind": kind, **kwargs}) + "\n")

        def audit(name, args):
            if name == "open" and args:
                target = os.fspath(args[0]) if isinstance(args[0], (str, bytes, os.PathLike)) else ""
                if isinstance(target, bytes):
                    target = os.fsdecode(target)
                if target in _forbidden:
                    event("forbidden_read", path=str(Path(target).relative_to(_synthetic_home)))
                    raise AssertionError("Fixture accessed forbidden owner-like synthetic file")
                if target == str(_synthetic_home / "config" / "browser.local.yaml"):
                    event("pinned_config_observed")
        sys.addaudithook(audit)

        def no_socket(*args, **kwargs):
            event("forbidden_socket")
            raise AssertionError("No real socket is permitted in fixture offline test")
        socket.socket.connect = no_socket
        socket.socket.bind = no_socket
        socket.create_connection = no_socket
        socket.getaddrinfo = no_socket

        class OfflineHTTPServer:
            def __init__(self, address, handler):
                assert address == ("127.0.0.1", 18789)
                event("http_handler_selected")
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return None
            timeout = 1
            def serve_forever(self):
                pass
            def shutdown(self):
                pass
            def server_close(self):
                pass
        http.server.HTTPServer = OfflineHTTPServer

        original_import = builtins.__import__
        def checked_import(name, *args, **kwargs):
            if name in ("core.config", "litellm"):
                event("config_import_env",
                      module=name,
                      dotenv=os.getenv("PYTHON_DOTENV_DISABLED"),
                      cost=os.getenv("LITELLM_LOCAL_MODEL_COST_MAP"))
                assert os.getenv("PYTHON_DOTENV_DISABLED") == "1"
                assert os.getenv("LITELLM_LOCAL_MODEL_COST_MAP") == "True"
            result = original_import(name, *args, **kwargs)
            if name == "tools.browser_profile":
                # Modify only the fake interpreter, after importing this module.
                # Session.__aenter__ still uses actual ensure_browser in to_thread.
                module = sys.modules.get("tools.browser_profile")
                if module is not None:
                    module.profile_pids = lambda directory: [123]
            return result
        builtins.__import__ = checked_import

        original_which = shutil.which
        def which(command, *args, **kwargs):
            if command == "bsk":
                return "/synthetic/bsk"
            return original_which(command, *args, **kwargs)
        shutil.which = which

        _connection = {"instance_id": "synthetic-pin",
                       "unresponsive": False, "version_skew": False}
        def checked_env(env):
            assert env["BSK_AUTO_START"] == "0"
            assert env["PYTHON_DOTENV_DISABLED"] == "1"
            assert env["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
            for secret in ("OPENAI_API_KEY", "SYNTHETIC_PROVIDER_SECRET",
                           "APPLIEDIN_SECRET_SENTINEL"):
                assert secret not in env
            event("sanitized_bsk_child")

        def fake_run(argv, **kwargs):
            assert argv[0] == "/synthetic/bsk"
            assert argv[1] in ("status", "browsers")
            assert argv[-1] == "--json"
            checked_env(kwargs["env"])
            event("sync_bsk", operation=argv[1])
            return SimpleNamespace(stdout=json.dumps(
                {"ok": True, "browsers": [_connection]}), returncode=0)
        subprocess.run = fake_run

        _url = "http://127.0.0.1:18789/fixture/example-co/job/1"
        _field = {"selector": "#name", "label": "Name", "question": "",
                  "type": "text", "tag": "input", "required": False,
                  "disabled": False, "has_value": False, "value": "",
                  "submit": False, "in_form": True,
                  "form_selector": "#synthetic-form", "form_action": _url,
                  "formaction": "", "files": []}
        _second = dict(_field, selector="#additional", label="Additional question",
                       type="textarea", tag="textarea")
        _submit = dict(_field, selector="#submit", label="Submit application",
                       type="submit", tag="button", submit=True)
        _page = {"url": _url, "title": "Synthetic role",
                 "text": "Synthetic harmless role description. " * 25,
                 "controls": [_field, _second, _submit],
                 "inventory_verified": True, "truncated": False,
                 "opaque_controls": False, "shadow_roots": False,
                 "unsupported_frames": []}

        class Proc:
            returncode = 0
            def __init__(self, payload):
                self.payload = payload
            async def communicate(self):
                return json.dumps(self.payload).encode(), b""
            def kill(self):
                raise AssertionError("No real subprocess may be killed")
            async def wait(self):
                return 0

        _context = {"count": 0, "tab": False}
        async def fake_async(*argv, **kwargs):
            assert argv[0] == "/synthetic/bsk" and argv[-1] == "--json"
            checked_env(kwargs["env"])
            operation = argv[1]
            assert operation in ("session", "cooperate", "navigate", "evaluate", "fill")
            if operation in ("navigate", "evaluate", "fill"):
                # BrowserSkill 0.4: an action carries the latest sync context once.
                if _context["tab"]:
                    assert argv[argv.index("--context") + 1] == _context.pop("latest", None)
                else:  # a fresh session has no tab: its first navigation needs no context
                    assert operation == "navigate" and "--context" not in argv
                    _context["tab"] = True
            if operation == "cooperate":
                assert argv[2] == "sync" and argv[3:5] == ("--session", "fixture-only-session")
                assert _context["tab"], "sync before the first navigation is refused by bsk"
                _context["count"] += 1
                _context["latest"] = "ctx-%d" % _context["count"]
                event("async_bsk", operation="cooperate_sync")
                payload = {"context_id": _context["latest"], "kind": "sync"}
            elif operation == "session":
                subcommand = argv[2]
                assert subcommand in ("start", "stop")
                if subcommand == "start":
                    _context.update(tab=False, latest=None)  # every session starts tab-less
                    assert "--browser" in argv
                    assert argv[argv.index("--browser") + 1] == "synthetic-pin"
                    payload = {"ok": True, "session_id": "fixture-only-session",
                               "browser_instance_id": "synthetic-pin"}
                else:
                    assert "fixture-only-session" in argv
                    payload = {"ok": True}
                event("async_bsk", operation="session_" + subcommand)
            elif operation == "navigate":
                assert argv[2] == _url
                event("async_bsk", operation="navigate")
                payload = {"ok": True}
            elif operation == "evaluate":
                from tools.browser_skill_dom import ENTRY_PAGE, GREENHOUSE_PAGE
                assert argv[2] in {ENTRY_PAGE, GREENHOUSE_PAGE}
                event("async_bsk", operation="evaluate_page")
                payload = {"ok": True, "value": _page}
            else:
                assert argv[2:5] == ("#name", "--value", "Test User")
                assert argv[5] == "--context" and argv[7] == "--session"
                assert argv[8] == "fixture-only-session"
                _field["has_value"], _field["value"] = True, "Test User"
                event("async_bsk", operation="fill_name")
                payload = {"ok": True}
            return Proc(payload)
        asyncio.create_subprocess_exec = fake_async
    '''))
    env = dict(os.environ)
    env.update(PYTHONPATH=os.pathsep.join([str(tmp_path), str(root / "src")]),
               HOME=str(home),
               APPLIEDIN_CONFIG_DIR=str(config),
               SYNTHETIC_TEST_LOG=str(log),
               SYNTHETIC_PROVIDER_SECRET="not-a-real-secret",
               OPENAI_API_KEY="not-a-real-secret",
               APPLIEDIN_SECRET_SENTINEL="not-a-real-secret",
               LITELLM_LOCAL_MODEL_COST_MAP="False",
               PYTHON_DOTENV_DISABLED="0")
    result = subprocess.run(
        [sys.executable, "-m", "daemon", "--synthetic-browser-fixture"],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=45)
    # The offline transport supplies an incomplete inventory, not a false
    # success receipt; the controller must gate before any mutating operation.
    assert result.returncode == 1, (result.stdout, result.stderr)
    # A Python import/fixture crash is NOT a verified application refusal.
    # Demand structured outcomes from the real daemon fixture in addition to
    # the no-mutation subprocess trace below.
    assert "Traceback" not in result.stderr, result.stderr
    reports = [json.loads(line) for line in result.stdout.splitlines()
               if line.lstrip().startswith("{")]
    assert len(reports) == 9, (result.stdout, result.stderr)
    assert {report['scenario'] for report in reports[:-1]} == {
        'entry', 'greenhouse', 'greenhouse_empty_placeholder',
        'w_button_aria_label', 'ashby', 'ashby-upload',
        'lever_custom_radio', 'lever_custom_checkbox'}
    assert all(report['status'] == 'gate'
               and report['submitted'] is False
               and report['submit']['call_entered'] is False
               and report['submit']['ipc_returned'] is False
               and report['submit']['post_attempts'] == 0
               for report in reports[:-1]), [
        (report['scenario'], report['status'],
         report['submitted'], report['applicant_ipc'], report['submit'])
        for report in reports[:-1]
    ]
    # The aggregate summary has safe_gate/legs, not a submitted attribute;
    # the eight per-session reports carry the actual Submit IPC observations.
    assert reports[-1]['safe_gate'] is False
    events = [json.loads(line) for line in log.read_text().splitlines()]
    kinds = [e["kind"] for e in events]
    assert "http_handler_selected" in kinds
    assert "pinned_config_observed" in kinds
    assert not any(k in {"forbidden_read", "forbidden_socket"} for k in kinds)
    assert not any(e["kind"] == "config_import_env"
                   and (e["dotenv"], e["cost"]) != ("1", "True")
                   for e in events)
    actions = [e["operation"] for e in events if e["kind"] == "async_bsk"]
    # One BSK session per independently checked scenario report.
    # The final aggregate has no session.
    expected_sessions = len(reports[:-1])
    assert actions.count('session_start') == actions.count('session_stop') == expected_sessions
    assert actions.count('navigate') == expected_sessions
    assert actions.count('evaluate_page') == expected_sessions
    assert actions.count("fill_name") == 0
    assert not any(a in {"click", "upload", "select", "choose"} for a in actions)
    assert any(e["kind"] == "sanitized_bsk_child" for e in events)



@pytest.mark.parametrize("drift", ["initial_redirect", "later_page"])
async def test_closed_facade_rejects_url_drift_before_any_fill(authority, drift):
    sent = []
    class Raw:
        async def navigate(self, url):
            sent.append(("navigate", url))
            return synthetic_page(url=fixture.URL + "/wrong" if drift == "initial_redirect"
                                  else fixture.URL)
        async def page(self):
            sent.append(("evaluate", "PAGE"))
            return synthetic_page(url=fixture.URL + "?redirect=true")
        async def call(self, *args):
            pytest.fail("A changed fixture URL must never reach form IPC")
    edge = authority.facade(Raw())
    if drift == "initial_redirect":
        with pytest.raises(PermissionError, match="redirected"):
            await edge.navigate(fixture.URL)
        assert sent == [("navigate", fixture.URL)]
    else:
        await edge.navigate(fixture.URL)
        with pytest.raises(PermissionError, match="escaped"):
            await edge.page()
        assert sent == [("navigate", fixture.URL), ("evaluate", "PAGE")]


@pytest.mark.parametrize("drift", ["receiver", "status"])
async def test_native_continue_jit_is_not_hidden_by_missing_pdf_or_approval(
        authority, monkeypatch, drift):
    """Pass all other native-form/attachment evidence before changing one guard.

    This is an offline *controller* test: the facade still forbids every click,
    even if the controller predicates would otherwise allow a native Continue.
    """
    current = synthetic_page()
    current["controls"][0].update(required=True, value="Test User", has_value=True)
    current["controls"][1].update(
        required=True, value="Synthetic answer", has_value=True)
    resume = {
        "selector": "#resume", "label": "Resume", "question": "", "type": "file",
        "tag": "input", "required": True, "disabled": False,
        "in_form": True, "form_selector": "#synthetic-form", "form_action": fixture.URL,
        "formaction": "", "files": ["Synthetic-Resume.pdf"],
    }
    current["controls"].insert(2, resume)
    cont = current["controls"][-1]
    cont.update(selector="#continue", label="Continue", type="submit", tag="button",
                submit=True)
    filled = {"#name": "Test User", "#additional": "Synthetic answer"}
    # The current form state and PDF readback still precede the existing
    # possible-submission hold; removed per-field receipts do not participate.
    forms.check_form(current, filled, True, "Synthetic-Resume.pdf")
    forms.check_form_destination(current, cont, fixture.URL)
    authority.row["status"], authority.row["gate_reason"], authority.hold = (
        "needs_human", "submit_uncertain", True)
    forms.check_before_committing_click(fixture.PK, fixture_context=authority)

    sent = []
    class Raw:
        async def navigate(self, url):
            sent.append(("navigate", url))
            return current
        async def page(self):
            sent.append(("evaluate", "PAGE"))
            return current
        async def call(self, *args):
            pytest.fail("Even a valid native Continue must not commit via fixture facade")
    edge = authority.facade(Raw())
    await edge.navigate(fixture.URL)
    marks = []
    action = {"action": "click", "selector": "#continue"}
    kwargs = {
        "facts": dict(fixture._FACTS), "filled": filled,
        "resume_path": authority.resume_path, "company": fixture.COMPANY,
        "jd_text": "", "resume_tex": "", "github": "",
        "pk": fixture.PK, "allow_click": True, "job_url": fixture.URL,
        "on_committing_click": lambda: marks.append("reached"),
        "fixture_context": authority,
    }
    with pytest.raises(PermissionError, match="denies"):
        await forms.execute(edge, current, action, **kwargs)
    # r06 marks a possible click only AFTER the ONE raw producer verdict.
    # This deliberately unbound fixture facade denies the click with no mark.
    assert marks == [] and sent == [("navigate", fixture.URL)]
    if drift == "receiver":
        cont["form_action"] = "https://foreign.example.test/submit"
        with pytest.raises(forms.Gate, match="form destination"):
            forms.check_form_destination(current, cont, fixture.URL)
    else:
        authority.row["status"] = "applied_manual"
        with pytest.raises(forms.Gate, match="Human outcome changed"):
            forms.check_before_committing_click(
                fixture.PK, fixture_context=authority)
    assert not marks and sent == [("navigate", fixture.URL)]


def test_fixture_hold_readback_missing_blocks_before_row_or_click(authority, monkeypatch):
    """A synthetic hold SET without its read-back witness is not a safe click."""
    previous = dict(authority.row)
    monkeypatch.setattr(fixture._SyntheticRun, "mark_hold", lambda self, pk: None)
    with pytest.raises(forms.Gate, match="hold could not be confirmed"):
        forms.hold_possible_submission(
            fixture.PK, {"last_button": "Submit", "url": fixture.URL},
            fixture_context=authority)
    assert authority.row == previous and not authority.hold



def test_ordinary_python_module_daemon_still_enters_workers_and_server(tmp_path):
    """The non-fixture -m entry follows original startup, entirely with offline fakes."""
    from textwrap import dedent

    site = tmp_path / "sitecustomize.py"
    site.write_text(dedent(r'''
        import builtins
        import sys
        import threading
        from types import ModuleType, SimpleNamespace

        # The server is the only network-owning foreground entry in main().
        stub_server = ModuleType("server")
        stub_server.create_app = lambda: object()
        stub_server._recover_stuck = lambda settings: None
        sys.modules["server"] = stub_server

        # _serve_http owns Uvicorn so it can bound daemon shutdown. Keep this
        # ordinary-entry regression offline instead of opening the synthetic port.
        stub_uvicorn = ModuleType("uvicorn")
        class FakeConfig:
            def __init__(self, app, *, host, port, log_level):
                self.port = port
        class FakeServer:
            def __init__(self, config):
                self.config = config
            def run(self):
                print("ORDINARY_SERVER:" + str(self.config.port), flush=True)
            def handle_exit(self, sig, frame):
                pass
        stub_uvicorn.Config = FakeConfig
        stub_uvicorn.Server = FakeServer
        sys.modules["uvicorn"] = stub_uvicorn

        # Recovery imports release_claim regardless of whether rows exist.
        stub_run = ModuleType("agent.run")
        stub_run.release_claim = lambda pk, stores: None
        sys.modules["agent.run"] = stub_run

        stores = SimpleNamespace(
            queue=SimpleNamespace(drain=lambda *_a, **_kw: []),
            tracking=SimpleNamespace(all=lambda: []))
        previous_import = builtins.__import__
        class FakeThread:
            def __init__(self, *args, **kwargs):
                self.name = kwargs["name"]
            def start(self):
                print("ORDINARY_THREAD:" + self.name, flush=True)
        # This daemon starts workers before importing its supervised server path.
        # Replace threads up front so no worker races the offline startup fixture.
        threading.Thread = FakeThread
        def import_hook(name, *args, **kwargs):
            module = previous_import(name, *args, **kwargs)
            if name == "core.stores":
                sys.modules["core.stores"].make_stores = lambda *_a, **_kw: stores
            return module
        builtins.__import__ = import_hook
    '''))
    root = Path(__file__).resolve().parents[2]
    synthetic_port = 18793  # Deliberately not the daemon's default port.
    # Never inherit owner/provider settings or read a repository .env.
    # The normal daemon entry uses fake stores/server/threads and binds no socket.
    env = {
        "HOME": str(tmp_path),
        "PATH": os.defpath,
        "PYTHONPATH": os.pathsep.join([str(tmp_path), str(root / "src")]),
        "PYTHON_DOTENV_DISABLED": "1",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "APPLIEDIN_MODE": "local",
        "APPLIEDIN_DISCOVERY": "off",
        "APPLIEDIN_WEB_PORT": str(synthetic_port),
        "PYTHONNOUSERSITE": "1",
    }
    result = subprocess.run(
        [sys.executable, "-m", "daemon"], cwd=tmp_path, env=env,
        text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, (result.stdout, result.stderr)
    server_lines = [line for line in result.stdout.splitlines()
                    if line.startswith("ORDINARY_SERVER:")]
    assert server_lines == [f"ORDINARY_SERVER:{synthetic_port}"]
    assert result.stdout.count("ORDINARY_THREAD:") == 4
    assert "ORDINARY_THREAD:evaluate" in result.stdout
    assert "ORDINARY_THREAD:apply" in result.stdout
    assert "ORDINARY_THREAD:heartbeat" in result.stdout
    assert "ORDINARY_THREAD:daemon-shutdown-watchdog" in result.stdout
    assert "ORDINARY_THREAD:discovery" not in result.stdout


@pytest.mark.parametrize("extra_args", [
    ["--synthetic-browser-fixture", "--help"],
    ["--synthetic-browser-fixture", "--other"],
    ["--help"],
    ["--unknown-option"],
    ["--synthetic-browser-fixture", "--synthetic-browser-fixture"],
])
def test_invalid_daemon_cli_args_refuse_before_any_product_import(
        tmp_path, extra_args):
    """The old fallthrough imported stores/workers before noticing extra argv.

    Start a clean Python interpreter; intercept repository imports and socket
    operations *before* daemon imports. No real workers, BSK, or owner data.
    """
    site = tmp_path / "sitecustomize.py"
    site.write_text(r'''
import builtins
import socket
original_import = builtins.__import__
def checked_import(name, *args, **kwargs):
    if name in {"core.logging", "core.stores", "core.ids",
                "tools.browser_skill_fixture", "server"}:
        raise AssertionError("FORBIDDEN_PRODUCTION_IMPORT:" + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = checked_import
def no_network(*args, **kwargs):
    raise AssertionError("FORBIDDEN_NETWORK_OPERATION")
socket.socket.connect = no_network
socket.socket.bind = no_network
socket.create_connection = no_network
socket.getaddrinfo = no_network
''')
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env.update(
        PYTHONPATH=os.pathsep.join([str(tmp_path), str(root / "src")]),
        APPLIEDIN_DISCOVERY="on",
        PYTHON_DOTENV_DISABLED="1",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        OPENAI_API_KEY="synthetic-not-a-real-secret",
    )
    child = subprocess.run(
        [sys.executable, "-m", "daemon", *extra_args],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=25)
    assert child.returncode != 0
    assert "Unsupported daemon arguments" in child.stderr
    assert "FORBIDDEN_PRODUCTION_IMPORT" not in child.stderr
    assert "FORBIDDEN_NETWORK_OPERATION" not in child.stderr
    assert not list(tmp_path.glob("*.lock"))
    assert not (tmp_path / ".local").exists()



@pytest.mark.parametrize("verb", ["submit", "continue"])
@pytest.mark.parametrize("change", [
    "baseline", "form_action", "formaction", "hold_readback", "late_status",
])
async def test_apply_native_submit_and_continue_guard_order_with_staged_pdf(
        authority, monkeypatch, verb, change):
    """Reach apply()'s native Submit/Continue JIT using full valid synthetic evidence.

    The test double preserves a current staged PDF and readback values
    (never a real BrowserSkill upload). The fixture accepts the first fill,
    but no click is permitted beneath the final controller.
    Every click remains forbidden beneath the controller.
    """
    current = synthetic_page()
    # This is a genuine final-guard scenario, not a partially identified
    # posting: canonical row, final title and original URL must all agree.
    authority.row.update(job_id="synthetic-job", jd_url=fixture.URL,
                         title=current["title"], ats="custom")
    current["controls"][0].update(required=True, has_value=True,
                                  value="Test User")
    current["controls"][1].update(required=True, has_value=True,
                                  value="Synthetic answer")
    document_name = Path(authority.resume_path).name
    resume = {
        "selector": "#resume", "label": "Resume", "question": "",
        "tag": "input", "type": "file", "required": True,
        "disabled": False, "in_form": True,
        "form_selector": "#synthetic-form", "form_action": fixture.URL,
        "formaction": "", "files": [document_name],
    }
    current["controls"].insert(2, resume)
    button = current["controls"][-1]
    if verb == "continue":
        button.update(selector="#continue", label="Continue")
    button_action = {"action": "submit" if verb == "submit" else "click",
                     "selector": button["selector"]}
    # The final submission test supplies a separate test-only current form
    # observation; obsolete historical per-field grants are deliberately absent.
    # The production JS and any zero-grant fixture remain unverified by it.
    async def witnessed(_session, current):
        return current, {c["selector"]: {"visible": True, "readonly": False,
            "disabled": bool(c.get("disabled")), "inert": False}
            for c in current["controls"]}
    monkeypatch.setattr(forms, "_observe_controls", witnessed)
    observed = []
    underlying = []
    staged = []
    marks = []
    decisions = iter([
        {"action": "fill", "selector": "#name", "fact": "Name"},
        {"action": "fill", "selector": "#additional", "fact": "Additional question"},
        {"action": "upload", "selector": "#resume"},
        button_action,
    ])

    async def synthetic_decision(self, task, page, history, *, model):
        assert self is authority and model == "synthetic-fixture"
        choice = next(decisions)
        observed.append("decision:" + choice["action"])
        return choice

    monkeypatch.setattr(fixture._SyntheticRun, "decision", synthetic_decision)
    monkeypatch.setattr(bsk, "decision", _source_denied)

    actual_execute = forms.execute
    async def prevalidated_receipts(session, page, action, **kwargs):
        selector, kind = action.get("selector"), action.get("action")
        if selector == "#additional" and kind == "fill":
            control = forms.control(page, action)
            value = "Synthetic answer"
            forms.guarded_value(control, value)
            kwargs["filled"]["#additional"] = value
            staged.append("preverified_additional_receipt")
            return {"action": "fill", "label": control["label"],
                    "selector": selector, "question": control["question"],
                    "url": page["url"], "fact": "Additional question",
                    "approved_value": value}
        if selector == "#resume" and kind == "upload":
            control = forms.control(page, action)
            assert control["type"] == "file" and document_name in control["files"]
            assert Path(kwargs["resume_path"]).read_bytes().startswith(b"%PDF-")
            staged.append("preverified_attached_pdf")
            # Do not perform an upload, a real BSK call, or a premature
            # receiver check. The *final native button* owns the JIT predicate.
            return {"action": "upload", "label": "Resume", "uploaded": True}
        return await actual_execute(session, page, action, **kwargs)
    monkeypatch.setattr(forms, "execute", prevalidated_receipts)

    class FakeSession:
        def __init__(self, kind):
            assert kind == "apply"
        async def __aenter__(self):
            underlying.append(("session_start",))
            return self
        async def __aexit__(self, *args):
            underlying.append(("session_stop",))
        async def navigate(self, url):
            underlying.append(("navigate", url))
            return current
        async def page(self):
            underlying.append(("evaluate_PAGE",))
            return current
        async def call(self, *args, **kwargs):
            underlying.append(tuple(args))
            if args == ("fill", "#name", "--value", "Test User") and not kwargs:
                return {}
            pytest.fail("Unsafe BrowserSkill IPC escaped the closed fixture facade")
    monkeypatch.setattr(bsk, "Session", FakeSession)

    # Spies call the REAL predicates: their observed order must be meaningful,
    # and a removed guard must make the matching one-variable negative fail.
    checks = [
        ("receiver", "check_form_destination"),
        ("inventory", "check_form"),
        ("hold", "hold_possible_submission"),
        ("status", "check_before_committing_click"),
    ]
    for marker, attribute in checks:
        original = getattr(forms, attribute)
        def spy(*args, _original=original, _marker=marker, **kwargs):
            observed.append(_marker)
            return _original(*args, **kwargs)
        monkeypatch.setattr(forms, attribute, spy)

    if change in {"form_action", "formaction"}:
        button[change] = "https://foreign.example.test/collect"
    elif change == "hold_readback":
        original_read = fixture._SyntheticRun.is_held
        def fail_readback(self, pk):
            observed.append("hold_readback")
            assert self is authority and pk == fixture.PK
            return False
        monkeypatch.setattr(fixture._SyntheticRun, "is_held", fail_readback)
    elif change == "late_status":
        original_write = fixture._SyntheticRun.set_status
        def raced_status(self, pk, status, **attrs):
            original_write(self, pk, status, **attrs)
            self.row["status"] = "applied_manual"
            observed.append("synthetic_status_race")
        monkeypatch.setattr(fixture._SyntheticRun, "set_status", raced_status)

    # For every scenario the form has complete, independently inspectable
    # required values, exact receipts, native receiver and attached PDF.
    expected_filled = {"#name": "Test User", "#additional": "Synthetic answer"}
    forms.check_form(current, expected_filled, True, document_name)
    observed.clear()  # Precondition check is separate from apply() JIT ordering.
    assert Path(authority.resume_path).is_file()

    result = await forms.apply(
        fixture.URL, fixture.COMPANY, dict(fixture._FACTS),
        "synthetic-fixture", pk=fixture.PK,
        resume_path=authority.resume_path, fixture_context=authority)
    if change in {"form_action", "formaction"}:
        # Fresh receiver validation before the first fill now precedes all
        # staged effects; an unsafe current override cannot be postponed to JIT.
        assert staged == [] and authority.fills == 0
        assert result["status"] == "gate" and "form destination" in result["question"]
        assert "receiver" in observed and "hold" not in observed
        assert not authority.hold and authority.row["status"] == "submitting"
        assert not any(c[0] in {"fill", "click", "upload", "select", "choose"}
                       for c in underlying)
        return
    # Without per-field grants the proactive PDF receipt may precede the
    # ordinary additional-field receipt; both remain required before JIT.
    assert staged == ["preverified_attached_pdf", "preverified_additional_receipt"]
    assert observed.count("decision:" + button_action["action"]) == 1
    assert authority.fills == 1
    assert underlying.count(("fill", "#name", "--value", "Test User")) == 1
    assert underlying.count(("navigate", fixture.URL)) == 1
    assert underlying.count(("session_start",)) == 1
    assert underlying.count(("session_stop",)) == 1
    assert not any(c[0] in {"click", "upload", "select", "choose"}
                   for c in underlying)

    if change == "baseline":
        # All real controller JIT checks passed and attempted a click, but
        # the facade denied it BEFORE raw BSK IPC. No submitted application.
        assert result["status"] == "uncertain", result
        assert "receiver" in observed and "inventory" in observed
        assert "hold" in observed
        assert "status" in observed
        assert observed.index("receiver") < observed.index("inventory")
        assert observed.index("inventory") < observed.index("hold")
        assert observed.index("hold") < observed.index("status")
        assert authority.hold and authority.row["status"] == "needs_human"
    elif change in {"form_action", "formaction"}:
        assert result["status"] == "gate" and "form destination" in result["question"]
        assert "receiver" in observed and "hold" not in observed
        assert not authority.hold and authority.row["status"] == "submitting"
    elif change == "hold_readback":
        assert result["status"] == "gate" and "hold could not be confirmed" in result["question"]
        assert "inventory" in observed
        assert "hold" in observed and "hold_readback" in observed
        assert "status" not in observed and authority.row["status"] == "submitting"
    else:
        assert result["status"] == "gate" and "Human outcome changed" in result["question"]
        assert "inventory" in observed
        assert "hold" in observed and "status" in observed
        assert "synthetic_status_race" in observed
        assert authority.row["status"] == "applied_manual"


async def test_typed_fixture_entry_never_delegates_generic_navigation(authority, monkeypatch):
    from copy import deepcopy
    from tests.tools.test_browser_application_entry import dom, fake_clock
    from tools.browser_skill_dom import entry_reader
    fake_clock(monkeypatch)
    authority.ashby = True
    authority.enable_positive_fixture()
    proof = authority.entry_provenance()
    complete = dom({'root':False,'formless':True})
    fixed = dom({'root':False,'formless':True}, forms._ENTRY_FIXED_CONTROL_WITNESS)
    for page in (complete, fixed):
        page['url'] = fixture.URL
    shell = {**deepcopy(complete), 'controls':[], 'inventory_verified':False,
             'entry':{'root':None, 'native_candidates':[], 'formless':None,
                      'frames':[], 'responses':[], 'response_error':False,
                      'helper_challenge':False}}
    class Raw:
        def __init__(self):
            self.calls, self.pages = [], 0
        async def navigate(self, url):
            pytest.fail('generic raw.navigate must never observe the typed fixture')
        async def page(self):
            pytest.fail('generic raw.page must never observe the typed fixture')
        async def call(self, *args):
            self.calls.append(args)
            if args == ('navigate',fixture.URL):
                return {}
            if args == ('evaluate',forms.ENTRY_PAGE):
                self.pages += 1
                return {'value':deepcopy(shell if self.pages==1 else complete)}
            if args == ('evaluate',forms._ENTRY_FIXED_CONTROL_WITNESS):
                return {'value':deepcopy(fixed)}
            assert args == ('evaluate',entry_reader(fixture._inventory_diagnostic_script()))
            return {'value':{'ready_state':'complete','offending_elements':[],'shadow_hosts':[]}}
    raw = Raw()
    facade = authority.facade(raw)
    await facade.call('navigate',fixture.URL)
    session = forms._EntrySession(
        facade, lambda u: u == fixture.URL and forms._alias_current(
            proof, fixture_context=authority), proof, fixture_context=authority)
    result = await forms._stable_application_form(session,session.allowed,proof,
        deadline=__import__('asyncio').get_running_loop().time()+15)
    authority.entry_admitted(result)
    assert authority.entry_wait == 'PASS'
    assert authority.entry_initial_not_ready
    assert raw.calls[0] == ('navigate',fixture.URL)
    assert raw.calls[1] == ('evaluate',forms.ENTRY_PAGE)
    assert raw.pages == 5  # initial shell plus two independent PAGE+witness pairs
    assert sum(c==('evaluate',forms._ENTRY_FIXED_CONTROL_WITNESS) for c in raw.calls)==2
    assert all(c[0] in {'navigate','evaluate'} for c in raw.calls)
    assert authority.navigations == 1
    with pytest.raises(PermissionError):
        await facade.call('navigate',fixture.URL)


def test_formless_fixture_mirrors_r_r_z_but_x_bounds_remain_strict_offline():
    # AC-Z adds the exact inert frame beside the existing R pair; it does not
    # relax X's old UA-border regression.
    from tests.tools.test_browser_application_entry import dom
    assert fixture._ashby_form().count(b'<iframe') == 3
    for size, status in ((1, 'X'), (5, 'unknown')):
        page = dom(dict(frames=['X'], edit={'rect':{'width':size,'height':size}}))
        assert page['entry']['frames'][0]['status'] == status


def test_failure_diagnostic_reports_frame_box_structure_only():
    from tests.tools.test_browser_application_entry import dom
    from tools.browser_skill_dom import entry_reader
    script = entry_reader(fixture._inventory_diagnostic_script())
    page = dom(dict(frames=['X'],edit={'rect':{'width':5,'height':5},'style':{
        'visibility':'hidden','display':'inline','width':'1px','height':'1px',
        'borderTopWidth':'2px','borderRightWidth':'2px',
        'borderBottomWidth':'2px','borderLeftWidth':'2px'}}), script)
    frame, = page['frames']
    assert frame['tag'] == 'iframe' and frame['parent_path'] == ['#body']
    assert frame['rects'] == [{'width':5,'height':5}]
    assert frame['borders'] == ['2px']*4
    assert frame['width'] == frame['height'] == '1px'
    assert not {'value','src','title','text','name'} & frame.keys()


@pytest.mark.parametrize('reader', ['page','witness'])
async def test_unknown_frame_failure_diagnostic_preserves_rejected_snapshot(authority, reader):
    from tools.browser_skill_dom import entry_reader
    authority.enable_positive_fixture()
    script = forms.ENTRY_PAGE if reader=='page' else forms._ENTRY_FIXED_CONTROL_WITNESS
    rejected = {'selector':'#helper','status':'unknown','box':{'width':5,'height':5}}
    original = dict(url=fixture.URL,controls=[],inventory_verified=True,
                    entry=dict(root=None,frames=[rejected]))
    diagnostic = dict(frames=[{'selector':'#helper','tag':'iframe',
                       'rects':[{'width':1,'height':1}], 'borders':['0px']*4,
                       'parent_path':['body']}])
    calls = []
    class Raw:
        async def call(self, *args):
            calls.append(args)
            if args==('navigate',fixture.URL):
                return {}
            if args==('evaluate',script):
                return {'value':original}
            assert args==('evaluate',entry_reader(fixture._inventory_diagnostic_script()))
            return {'value':diagnostic}
    edge = authority.facade(Raw())
    await edge.call('navigate',fixture.URL)
    result = await edge.call('evaluate',script)
    assert result['value'] is original
    assert original['entry']['frames'][0]['status'] == 'unknown'
    assert authority.inventory_diagnostics['rejected_frames'][0]['box'] == {'width':5,'height':5}
    assert authority.inventory_diagnostics['frames'][0]['rects'] == [{'width':1,'height':1}]
    with pytest.raises(forms.Gate, match='unknown/untrusted application frame'):
        forms._entry_safety(original,lambda u: True,authority.entry_provenance())
    assert authority.fills == authority.ipc_index == 0
    assert len(calls) == 3


async def test_greenhouse_fixture_has_separate_typed_authority_and_scoped_readers(authority):
    from tests.tools.test_browser_skill import _badge_response
    authority.enable_positive_fixture()
    authority.greenhouse = True
    proof = authority.entry_provenance()
    assert isinstance(proof, forms.GreenhouseJobAlias)
    assert forms._alias_current(proof, fixture_context=authority)
    scripts = []
    class Raw:
        async def call(self, *args):
            scripts.append(args)
            if args == ('navigate', fixture.URL):
                return {}
            return {'value': _badge_response({'url': fixture.URL}, args[1])}
        async def page(self):
            pytest.fail('Greenhouse fixture must never use generic PAGE')
        async def navigate(self, url):
            pytest.fail('Greenhouse fixture must never use generic navigate')
    facade = authority.facade(Raw())
    await facade.call('navigate', fixture.URL)
    session = forms._GreenhouseSession(facade, lambda u: u == fixture.URL and forms._alias_current(
        proof, fixture_context=authority), proof)
    observed = await session.page()
    await session.call('evaluate', forms._FIXED_CONTROL_WITNESS)
    assert authority.visible_badge_anchor == 'PASS'
    assert authority.entry_wait == 'PENDING'
    assert authority.navigations == 1
    assert {o['reader'] for o in authority.entry_observations} == {'page', 'witness'}
    forms._inventory_ok(observed)
    assert scripts == [('navigate', fixture.URL), ('evaluate', forms.GREENHOUSE_PAGE),
                       ('evaluate', forms._GREENHOUSE_FIXED_CONTROL_WITNESS)]
    for script in [forms.ENTRY_PAGE, forms._ENTRY_FIXED_CONTROL_WITNESS, bsk.PAGE]:
        with pytest.raises(PermissionError):
            await facade.call('evaluate', script)
    with pytest.raises(PermissionError):
        await facade.call('navigate', fixture.URL)
    assert authority.submit_ipc == authority.post_attempts == 0


def test_greenhouse_fixture_required_proxy_is_five_names_and_search_is_not_required():
    class Inventory(HTMLParser):
        def __init__(self):
            super().__init__()
            self.inputs = []
        def handle_starttag(self, tag, attrs):
            if tag == 'input':
                self.inputs.append(dict(attrs))
    document = Inventory()
    document.feed(fixture._greenhouse_form().decode())
    proxies = [e for e in document.inputs if e.get('class') == 'validation-only']
    assert len(proxies) == 2
    assert all(set(e) == {'aria-hidden', 'class', 'required', 'tabindex', 'value'}
               and e['value'] == '' for e in proxies)
    searches = [e for e in document.inputs if e.get('role') == 'combobox']
    assert searches and all('required' not in e for e in searches)
    assert fixture._FORM == fixture._entry_form()
    assert b'https://www.recaptcha.net/recaptcha/enterprise/anchor' in fixture._greenhouse_form()
    assert b'width:256px;height:60px;border:0;padding:0' in fixture._greenhouse_form()
    assert (b'<textarea class="g-recaptcha-response" id="g-recaptcha-response-123" '
            b'name="g-recaptcha-response" style="display:none"></textarea>'
            in fixture._greenhouse_form())


async def test_greenhouse_fixture_location_readback_remains_unselected(authority):
    """An unselected native location is not authorized by legacy answer grants."""
    import runpy
    helpers = runpy.run_path(str(Path(__file__).with_name('test_browser_phone_block.py')))
    snapshot = helpers['served_fixture_dom']()
    authority.greenhouse = True
    authority.enable_positive_fixture()
    authority.entry_provenance()

    # The real served native bytes and production readers still admit the
    # untouched hidden-receiver graph. The failure was before opening it:
    # bootstrap asks again for a grant the fixture previously supplied.
    location = next(c for c in snapshot['page']['controls'] if c['selector'] == '#location')
    assert location['type'] == 'combobox'
    assert isinstance(location['combobox'], dict)
    assert location['combobox']['options'] == []
    assert not location['combobox'].get('selected_value')
    assert not location['combobox'].get('hidden_value')
    # No grant is needed: the final boundary must rely on these current,
    # unselected machine values rather than assuming an authorized location.
    assert authority.ipc_index == authority.decisions == 0
    assert authority.submit_ipc == authority.post_attempts == 0


@pytest.mark.parametrize('reader_name', ['entry_reader', 'greenhouse_reader'])
def test_fixture_reader_rewrite_anchors_all_match_current_inputs(reader_name):
    """A stale replacement silently removes a safety witness from fixture readers."""
    import ast
    import inspect
    from tools import browser_skill_dom as dom

    reader = getattr(dom, reader_name)
    calls = [node for node in ast.walk(ast.parse(inspect.getsource(reader)))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and isinstance(node.func.value, ast.Name)
             and node.func.value.id == 'script' and node.func.attr == 'replace']
    assert calls, 'The audit must enumerate the actual reader replacements.'

    class AuditedScript(str):
        def __new__(cls, value, observations):
            instance = super().__new__(cls, value)
            instance.observations = observations
            return instance

        def replace(self, old, new, count=-1):
            result = super().replace(old, new, count)
            self.observations.append((old, old in self, result != self))
            return type(self)(result, self.observations)

    runs = []
    for script in (dom.PAGE, forms._FIXED_CONTROL_WITNESS):
        observations = []
        assert reader(AuditedScript(script, observations)) == reader(script)
        assert len(observations) == len(calls), 'Every declared rewrite must be audited.'
        runs.append(observations)

    # PAGE-only and fixed-only anchors intentionally differ. Each replacement
    # must really change at least one current input, rather than silently no-op
    # on both as the stale fixed-witness selector anchor did after the rebase.
    for index, observations in enumerate(zip(*runs, strict=True)):
        assert len({old for old, _, _ in observations}) == 1
        assert any(present and changed for _, present, changed in observations), (
            reader_name, index, observations[0][0])


@pytest.mark.parametrize('native_valid', [False, True])
async def test_greenhouse_fixture_records_native_validity_after_existing_choice(authority, native_valid):
    authority.enable_positive_fixture()
    authority.greenhouse = True
    calls = []
    class Raw:
        async def call(self, *args):
            calls.append(args)
            if args == ('evaluate', fixture._COMMITTED_VALIDITY):
                return {'value': dict.fromkeys(fixture._CHOICE_SELECTORS.values(), native_valid)}
            return {}
    facade = authority.facade(Raw())
    await facade.call('navigate', fixture.URL)
    for ipc in authority._positive_ipc():
        await facade.call(*ipc)
    assert authority.native_validity == dict.fromkeys(fixture._CHOICE_SELECTORS.values(), native_valid)
    assert sum(c == ('evaluate', fixture._COMMITTED_VALIDITY) for c in calls) == 5
    assert all(c[0] in {'navigate','evaluate','fill','click'} for c in calls)
    assert not any(c == ('click', '#submit') for c in calls)
    assert authority.submit_call_entered == authority.post_attempts == 0


@pytest.mark.parametrize('invalid', ['required', 'value', 'validity', 'proxy', None])
def test_fixture_committed_validity_executes_native_check_not_visual_caption(invalid):
    source = r'''
const nodes=Object.fromEntries(['#location','#school','#degree','#discipline','#country'].map(s=>[s,{
  required:process.argv[2]==='required',value:process.argv[2]==='value'?'search':'',
  checkValidity:()=>process.argv[2]!=='validity',
  closest:()=>({querySelector:()=>process.argv[2]==='proxy'?{}:null})
}]));
global.document={querySelector:s=>nodes[s]};
console.log(JSON.stringify(eval(process.argv[1])));
'''
    result = json.loads(subprocess.check_output(
        ['node', '-e', source, fixture._COMMITTED_VALIDITY, invalid or 'valid'], text=True))
    assert result == dict.fromkeys(fixture._CHOICE_SELECTORS.values(), invalid is None)


def test_w_lookalike_is_one_non_origin_mutation_on_original_sealed_get(authority):
    documents = []
    for invalid in (False, True):
        authority.w_lookalike = invalid
        handler = object.__new__(fixture._FixtureHandler)
        handler.path = fixture._PATH
        handler.headers = {'Host': '127.0.0.1:18789'}
        handler.server = SimpleNamespace(synthetic_run=authority)
        handler.wfile = io.BytesIO()
        headers = []
        handler.send_response = lambda status: headers.append(('status', status))
        handler.send_header = lambda key, value: headers.append((key, value))
        handler.end_headers = lambda: None
        handler.do_GET()
        assert ('status', 200) in headers
        assert any(key == 'Content-Security-Policy' and "default-src 'none'" in value
                   for key, value in headers)
        documents.append(handler.wfile.getvalue())
    positive, negative = documents
    assert positive == fixture._FORM
    assert b'class="awli-button" type="button"></button>' in positive
    assert b'class="learn-more-link" href="https://widgets.example-co.test"' in positive
    assert negative == positive.replace(b'id="w-button"', b'id="w-button" aria-label="Synthetic consent"')
    assert authority.post_attempts == authority.post_count == 0


async def test_w_fixture_counts_denied_widget_targets_without_forwarding(authority):
    authority.enable_positive_fixture()
    forwarded = []
    class Raw:
        async def call(self, *args):
            forwarded.append(args)
            return {}
    facade = authority.facade(Raw())
    await facade.call('navigate', fixture.URL)
    assert authority.w_target_ipc == 0
    for method in ('click', 'focus', 'fill', 'evaluate'):
        for selector in ('#w-button', '#w-link'):
            with pytest.raises(PermissionError, match='denies'):
                await facade.call(method, selector)
    with pytest.raises(PermissionError, match='denies'):
        await facade.call('navigate', 'https://widgets.example-co.test')
    assert authority.w_target_ipc == 9
    assert forwarded == [('navigate', fixture.URL)]
