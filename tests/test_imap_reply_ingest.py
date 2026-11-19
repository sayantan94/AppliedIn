from __future__ import annotations

import email
import json
import socket
import threading
from pathlib import Path

import pytest

from core import imap_readonly, reply_ingest


def header(**items):
    msg = email.message.EmailMessage(policy=email.policy.default)
    for key, value in items.items():
        msg[key.replace("_", "-")] = value
    return msg.as_bytes()


def test_matcher_prefers_recorded_alias_usage_and_rejects_conflict():
    rows = [
        {"pk": "example-co#one", "company": "example-co", "title": "Backend Engineer", "status": "applied", "profile_id": "rot-new"},
        {"pk": "example-co#two", "company": "example-co", "title": "Platform Engineer", "status": "applied_manual", "profile_id": "rot-other"},
    ]
    aliases = [{"id": "rot-new", "email": "alias+new@example-co.test", "company": "example-co", "used_pks": ["example-co#one"]},
               {"id": "rot-other", "email": "alias-old@example-co.test", "company": "example-co", "used_pks": ["example-co#two"]}]
    assert reply_ingest.match_message(header(To="Alias+New@example-co.test", Subject="Reply"), rows, aliases) == "example-co#one"
    unused = dict(aliases[0], id="rot-unused", email="alias-unused@example-co.test", used_pks=[])
    assert reply_ingest.match_message(header(To="alias-unused@example-co.test", Subject="Reply"), rows, [unused]) == "UNASSIGNED"


def test_competing_alias_used_pk_blocks_company_fallback():
    row = {"pk": "example-co#one", "company": "example-co",
           "title": "Backend Engineer", "status": "applied", "profile_id": ""}
    addressed = {"id": "rot-a", "email": "alias-a@example-co.test",
                 "company": "example-co", "used_pks": []}
    competing = {"id": "rot-b", "email": "alias-b@example-co.test",
                 "company": "example-co", "used_pks": [row["pk"]]}
    assert reply_ingest.match_message(
        header(To=addressed["email"], Subject="Backend Engineer"),
        [row], [addressed, competing]) == "UNASSIGNED"

def test_matcher_fallback_requires_explicit_company_and_subject_title():
    rows = [{"pk": "example-co#one", "company": "example-co", "title": "Backend Engineer"}]
    assert reply_ingest.match_message(header(From="Example-co Recruiting <jobs@shared-ats.test>",
                                             Subject="Backend Engineer"), rows, []) == "UNASSIGNED"
    assert reply_ingest.match_message(header(From="Recruiting <jobs@shared-ats.test>",
                                             Subject="Backend Engineer"), rows, []) == "UNASSIGNED"
    encoded = b"From: Recruiting <jobs@shared-ats.test>\r\nSubject: =?UTF-8?Q?Example-co_Backend_Engineer?=\r\n\r\n"
    assert reply_ingest.match_message(encoded, rows, []) == "UNASSIGNED"


def test_imap_wrapper_sets_finite_timeout_and_only_safe_fetches(monkeypatch):
    calls = []

    class Fake:
        def login(self, *_): calls.append(("login",)); return "OK", []
        def select(self, mailbox, readonly=False): calls.append(("select", mailbox, readonly)); return "OK", [b"1"]
        def uid(self, *args):
            calls.append(args)
            if args[0] == "SEARCH": return "OK", [b"1"]
            return "OK", [(b"x", b"safe")]
        def logout(self): calls.append(("logout",)); return "BYE", []

    def constructor(host, **kwargs):
        calls.append(("constructor", host, kwargs))
        return Fake()
    monkeypatch.setattr(imap_readonly.imaplib, "IMAP4_SSL", constructor)
    client = imap_readonly.ReadOnlyIMAP("imap.example-co.test", "test", "synthetic", "INBOX")
    assert client.search_uids() == [1]
    assert client.fetch_headers(1) == b"safe"
    assert client.fetch_text_prefix(1) == b"safe"
    client.logout()
    kwargs = next(call[2] for call in calls if call[0] == "constructor")
    assert kwargs["timeout"] == imap_readonly.TIMEOUT_SECONDS > 0
    assert kwargs["ssl_context"].check_hostname
    assert ("select", "INBOX", True) in calls
    assert any("BODY.PEEK[HEADER]" in args[-1] for args in calls if args[0] == "FETCH")
    assert any("BODY.PEEK[TEXT]<0.4096>" in args[-1] for args in calls if args[0] == "FETCH")


def test_secrets_file_is_restricted(tmp_path):
    path = tmp_path / "secrets.json"
    reply_ingest.secure_secrets_file(path)
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600
    path.chmod(0o644)
    reply_ingest.secure_secrets_file(path)
    assert path.stat().st_mode & 0o777 == 0o600


def test_source_has_only_the_readonly_imap_import():
    root = Path(__file__).parents[1] / "src"
    imap_imports, smtp_imports = [], []
    for path in root.rglob("*.py"):
        text = path.read_text()
        if "import imaplib" in text or "from imaplib" in text:
            imap_imports.append(path.relative_to(root).as_posix())
        if "import smtplib" in text or "from smtplib" in text:
            smtp_imports.append(path.relative_to(root).as_posix())
    assert imap_imports == ["core/imap_readonly.py"]
    # #387 slice A adds the sole reviewed SMTP boundary, not another IMAP reader.
    assert smtp_imports == ["core/outreach_email.py"]


def test_cloud_mode_never_constructs_transport_or_secrets_backend(monkeypatch):
    from core import stores
    class Settings:
        mode = "cloud"
        local_dir = "."
    monkeypatch.setattr(reply_ingest, "ReadOnlyIMAP", lambda *_: pytest.fail("cloud opened IMAP"))
    monkeypatch.setattr(stores, "make_stores", lambda *_: pytest.fail("cloud constructed secrets backend"))
    assert reply_ingest.local_credentials(Settings()) is None
    assert reply_ingest.ingest_pass(Settings(), credentials=fake_credentials()) == 0

def test_processed_uid_replay_advances_cursor_without_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr("core.rotation.aliases", lambda: [])
    settings = type("Settings", (), {"mode": "local", "local_dir": str(tmp_path)})()
    credentials = {"host": "imap.example-co.test", "username": "test", "password": "synthetic", "mailbox": "INBOX"}
    key = reply_ingest._account_key(credentials)
    path = tmp_path / reply_ingest.STATE_NAME
    prior = {"record_id": "record-1", "date": "", "sender": "", "subject": "", "message_id": "", "label": None, "pk": "UNASSIGNED"}
    path.write_text(json.dumps({"accounts": {key: {"uidvalidity": "7", "last_uid": 40, "processed": ["7:41"], "message_ids": [], "records": [prior]}}}))

    class Tracking:
        def query_status(self, status): return []
        def all(self): raise AssertionError("full tracking scan is forbidden")
    class Client:
        uidvalidity = "7"
        def search_uids(self, query): return [41]
        def fetch_headers(self, uid): raise AssertionError("processed UID must not be fetched")
        def fetch_text_prefix(self, uid): raise AssertionError("processed UID has no body fetch")
        def logout(self): pass
    stores = type("Stores", (), {"tracking": Tracking()})()
    assert reply_ingest.ingest_pass(settings, credentials, stores, Client()) == 0
    state = json.loads(path.read_text())["accounts"][key]
    assert state["last_uid"] == 41
    assert len(reply_ingest.read_records(settings)) == 1

def test_bad_header_with_eligible_alias_is_unassigned_between_valid_messages(tmp_path, monkeypatch):
    alias = {"id": "rot-a", "email": "reply@example-co.test", "company": "example-co",
             "used_pks": ["example-co#one"]}
    monkeypatch.setattr("core.rotation.aliases", lambda: [alias])
    settings = type("Settings", (), {"mode": "local", "local_dir": str(tmp_path)})()
    credentials = {"host": "imap.example-co.test", "username": "test", "password": "synthetic", "mailbox": "INBOX"}
    candidates_read = []
    row = {"pk": "example-co#one", "company": "example-co", "title": "Backend Engineer",
           "status": "applied", "profile_id": "rot-a"}
    class Tracking:
        def query_status(self, status):
            candidates_read.append(status)
            return [row] if status.value == "applied" else []
        def all(self): raise AssertionError("tracking.all() must not be used")
    messages = {1: header(To=alias["email"], From="recruiter@example-co.test", Subject="Backend Engineer"),
                2: header(To=alias["email"], From="%%%", Subject="Backend Engineer"),
                3: header(To=alias["email"], From="recruiter@example-co.test", Subject="Backend Engineer")}
    class Client:
        uidvalidity = "99"
        def search_uids(self, query): return [1, 2, 3]
        def fetch_headers(self, uid): return messages[uid]
        def fetch_text_prefix(self, uid): return b"body must remain in memory"
        def logout(self): pass
    stores = type("Stores", (), {"tracking": Tracking()})()
    assert reply_ingest.ingest_pass(settings, credentials, stores, Client()) == 2
    records = reply_ingest.read_records(settings)
    assert [record["pk"] for record in records] == [row["pk"], row["pk"]]
    assert len(records) == 2
    state = json.loads((tmp_path / reply_ingest.STATE_NAME).read_text())["accounts"][reply_ingest._account_key(credentials)]
    assert state["unresolved_uids"] == [2] and state["last_uid"] == 1
    assert b"body must remain in memory" not in (tmp_path / reply_ingest.STATE_NAME).read_bytes()
    assert len(candidates_read) == 2

class FakeTracking:
    def __init__(self, applied=None, manual=None):
        self.applied = applied or []
        self.manual = manual or []
        self.queries = []

    def query_status(self, status):
        self.queries.append(status)
        return self.applied if status.value == "applied" else self.manual

    def all(self):
        raise AssertionError("tracking.all() is outside the candidate snapshot contract")


class FakeClient:
    def __init__(self, validity="1", uids=(), messages=None):
        self.uidvalidity = validity
        self.uids = list(uids)
        self.messages = messages or {}
        self.fetched = []
        self.queries = []

    def search_uids(self, query):
        self.queries.append(query)
        if query == "ALL":
            return list(self.uids)
        start = int(query.removeprefix("UID ").removesuffix(":*"))
        return [uid for uid in self.uids if uid >= start]

    def fetch_headers(self, uid):
        self.fetched.append(uid)
        return self.messages.get(uid, b"From: recruiter@example-co.test\r\nSubject: reply\r\n\r\n")

    def fetch_text_prefix(self, uid):
        return b"synthetic body prefix"

    def logout(self):
        pass


def fake_stores(tracking=None):
    return type("Stores", (), {"tracking": tracking or FakeTracking()})()


def fake_credentials(host="imap.example-co.test", username="test"):
    return {"host": host, "mailbox": "INBOX", "username": username, "password": "synthetic-secret"}


def run_fake_ingest(tmp_path, monkeypatch, client, credentials=None, tracking=None, classifier=None):
    monkeypatch.setattr("core.rotation.aliases", lambda: [])
    settings = type("Settings", (), {"mode": "local", "local_dir": str(tmp_path)})()
    inserted = reply_ingest.ingest_pass(settings, credentials or fake_credentials(),
                                        fake_stores(tracking), client, classifier)
    return settings, inserted


def test_positive_alias_evidence_with_no_eligible_positive_fails_closed():
    alias = {"id": "rot-a", "email": "reply@example-co.test", "company": "example-co",
             "used_pks": ["example-co#old"]}
    only_new = {"pk": "example-co#new", "company": "example-co", "title": "Backend Engineer",
                "status": "applied", "profile_id": ""}
    assert reply_ingest.match_message(
        header(To="reply@example-co.test", Subject="Backend Engineer"), [only_new], [alias]) == "UNASSIGNED"


def test_matcher_handles_cc_delivered_to_retired_alias_and_competing_recipients():
    row = {"pk": "example-co#one", "company": "example-co", "title": "Backend Engineer",
           "status": "applied", "profile_id": "rot-retired"}
    retired = {"id": "rot-retired", "email": "old@example-co.test", "company": "example-co",
               "used_pks": [row["pk"]], "retired": True}
    for recipient in ("Cc", "Delivered-To"):
        assert reply_ingest.match_message(header(**{recipient: retired["email"]}), [row], [retired]) == row["pk"]
    other = {"id": "rot-other", "email": "other@example-co.test", "company": "example-co", "used_pks": []}
    assert reply_ingest.match_message(
        header(To=f"{retired['email']}, {other['email']}"), [row], [retired, other]) == "UNASSIGNED"


def test_multiple_alias_used_pks_need_unique_subject_title():
    alias = {"id": "rot-a", "email": "reply@example-co.test", "company": "example-co",
             "used_pks": ["example-co#one", "example-co#two"]}
    rows = [{"pk": "example-co#one", "company": "example-co", "title": "Backend Engineer",
             "profile_id": "rot-a"},
            {"pk": "example-co#two", "company": "example-co", "title": "Platform Engineer",
             "profile_id": "rot-a"}]
    assert reply_ingest.match_message(header(To=alias["email"], Subject="Platform Engineer"), rows, [alias]) == "UNASSIGNED"
    assert reply_ingest.match_message(header(To=alias["email"], Subject="reply"), rows, [alias]) == "UNASSIGNED"


def test_shared_ats_and_url_host_do_not_prove_sender_company():
    row = {"pk": "example-co#one", "company": "example-co", "title": "Backend Engineer",
           "jd_url": "https://jobs.shared-ats.test/example-co/123", "ats": "greenhouse"}
    assert reply_ingest.match_message(
        header(From="Recruiting <jobs@shared-ats.test>", Subject="Backend Engineer"), [row], []) == "UNASSIGNED"


def test_wrapper_refuses_generic_and_mutating_commands():
    class Fake:
        def login(self, *_): return "OK", []
        def select(self, *_args, **_kwargs): return "OK", []
        def response(self, *_): return "UIDVALIDITY", [b"3"]
        def logout(self): return "BYE", []
    original = imap_readonly.imaplib.IMAP4_SSL
    imap_readonly.imaplib.IMAP4_SSL = lambda *_args, **_kwargs: Fake()
    try:
        client = imap_readonly.ReadOnlyIMAP("imap.example-co.test", "test", "synthetic", "INBOX")
        assert not hasattr(client, "uid") and not hasattr(client, "command")
        with pytest.raises(ValueError):
            client.search_uids("UID 1:* STORE 1 +FLAGS \\Seen")
        client.logout()
    finally:
        imap_readonly.imaplib.IMAP4_SSL = original


def test_two_inbox_accounts_with_same_uidvalidity_and_uid_are_isolated(tmp_path, monkeypatch):
    for host, username in (("imap-one.example-co.test", "account-one"), ("imap-two.example-co.test", "account-two")):
        client = FakeClient("77", [9], {9: b"From: recruiter@example-co.test\r\nMessage-ID: <same@example-co.test>\r\n\r\n"})
        settings, inserted = run_fake_ingest(tmp_path, monkeypatch, client, fake_credentials(host, username))
        assert inserted == 1
    state = json.loads((tmp_path / reply_ingest.STATE_NAME).read_text())
    assert len(state["accounts"]) == 2
    assert len(reply_ingest.read_records(settings)) == 2


def test_new_uid_with_duplicate_message_id_does_not_create_record(tmp_path, monkeypatch):
    credentials = fake_credentials()
    one = FakeClient("78", [1], {1: b"Message-ID: <same@example-co.test>\r\n\r\n"})
    settings, inserted = run_fake_ingest(tmp_path, monkeypatch, one, credentials)
    assert inserted == 1
    two = FakeClient("78", [1, 2], {2: b"Message-ID: <same@example-co.test>\r\n\r\n"})
    _, inserted = run_fake_ingest(tmp_path, monkeypatch, two, credentials)
    assert inserted == 0
    account = json.loads((tmp_path / reply_ingest.STATE_NAME).read_text())["accounts"][reply_ingest._account_key(credentials)]
    assert len(account["records"]) == 1 and "78:2" in account["processed"] and account["last_uid"] == 2
    rid = account["records"][0]["record_id"]
    assert account["record_uids"] == {rid: {"uid": 1, "uidvalidity": "78"}}


def test_uidvalidity_reset_scans_only_newest_200_and_retains_known_message_id(tmp_path, monkeypatch):
    credentials = fake_credentials()
    first = FakeClient("10", [1], {1: b"Message-ID: <known@example-co.test>\r\n\r\n"})
    settings, _ = run_fake_ingest(tmp_path, monkeypatch, first, credentials)
    second = FakeClient("11", range(1, 351),
                       {uid: b"Message-ID: <known@example-co.test>\r\n\r\n" for uid in range(151, 351)})
    _, inserted = run_fake_ingest(tmp_path, monkeypatch, second, credentials)
    assert inserted == 0 and second.fetched == list(range(151, 351))
    account = json.loads((tmp_path / reply_ingest.STATE_NAME).read_text())["accounts"][reply_ingest._account_key(credentials)]
    assert account["last_uid"] == 350 and len(account["records"]) == 1


def test_storage_failure_keeps_cursor_and_next_pass_retries(tmp_path, monkeypatch):
    client = FakeClient("12", [41], {41: b"From: recruiter@example-co.test\r\n\r\n"})
    original = reply_ingest._write_state
    writes = 0
    def fail_second_write(path, state):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("synthetic disk failure")
        original(path, state)
    monkeypatch.setattr(reply_ingest, "_write_state", fail_second_write)
    settings = type("Settings", (), {"mode": "local", "local_dir": str(tmp_path)})()
    with pytest.raises(OSError, match="synthetic disk failure"):
        reply_ingest.ingest_pass(settings, fake_credentials(), fake_stores(), client)
    path = tmp_path / reply_ingest.STATE_NAME
    state = json.loads(path.read_text())["accounts"][reply_ingest._account_key(fake_credentials())]
    assert state["last_uid"] == 0 and state["processed"] == []
    monkeypatch.setattr(reply_ingest, "_write_state", original)
    retry = FakeClient("12", [41], {41: b"From: recruiter@example-co.test\r\n\r\n"})
    assert reply_ingest.ingest_pass(settings, fake_credentials(), fake_stores(), retry) == 1
    state = json.loads(path.read_text())["accounts"][reply_ingest._account_key(fake_credentials())]
    assert state["last_uid"] == 41 and len(state["records"]) == 1


def test_socket_open_is_forbidden_during_fake_ingest(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("offline test attempted to open a socket")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    client = FakeClient("13", [1], {1: b"Subject: offline\r\n\r\n"})
    _, inserted = run_fake_ingest(tmp_path, monkeypatch, client)
    assert inserted == 1


def test_daemon_poller_registration_gates_all_configuration(monkeypatch):
    import daemon
    from core import reply_ingest
    started = []
    class Thread:
        def __init__(self, *_, name="", **__): self.name = name
        def start(self): started.append(self.name)
    monkeypatch.setattr(daemon.threading, "Thread", Thread)
    monkeypatch.setattr(reply_ingest, "enabled", lambda: True)
    monkeypatch.setattr(reply_ingest, "poll_interval", lambda: 30)
    monkeypatch.setattr(reply_ingest, "local_credentials", lambda *_: {"host": "x"})
    stores = type("Stores", (), {"secrets": object()})()
    local = type("Settings", (), {"mode": "local"})()
    cloud = type("Settings", (), {"mode": "cloud"})()
    assert daemon._start_imap_poller(stores, local) is True
    assert started == ["imap-replies"]
    for setting, enabled, interval, credentials in (
            (local, False, 30, {"host": "x"}), (local, True, None, {"host": "x"}),
            (local, True, 30, None), (cloud, True, 30, {"host": "x"})):
        monkeypatch.setattr(reply_ingest, "enabled", lambda value=enabled: value)
        monkeypatch.setattr(reply_ingest, "poll_interval", lambda value=interval: value)
        monkeypatch.setattr(reply_ingest, "local_credentials", lambda *_, value=credentials: value)
        assert daemon._start_imap_poller(stores, setting) is False
    assert started == ["imap-replies"]



def test_state_read_permission_error_preserves_file_and_retries(tmp_path, monkeypatch):
    settings = type("Settings", (), {"mode": "local", "local_dir": str(tmp_path)})()
    credentials = fake_credentials()
    path = tmp_path / reply_ingest.STATE_NAME
    original = b'{"accounts":{}}'
    path.write_bytes(original)
    real_read_text = Path.read_text
    failed = False
    def fail_once(instance, *args, **kwargs):
        nonlocal failed
        if instance == path and not failed:
            failed = True
            raise PermissionError("synthetic permission failure")
        return real_read_text(instance, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", fail_once)
    client = FakeClient("12", [41], {41: b"Message-ID: <retry@example-co.test>\r\n\r\n"})
    with pytest.raises(PermissionError, match="synthetic permission failure"):
        reply_ingest.ingest_pass(settings, credentials, fake_stores(), client)
    assert path.read_bytes() == original
    monkeypatch.setattr(Path, "read_text", real_read_text)
    retry = FakeClient("12", [41], {41: b"Message-ID: <retry@example-co.test>\r\n\r\n"})
    assert reply_ingest.ingest_pass(settings, credentials, fake_stores(), retry) == 1
    account = json.loads(path.read_text())["accounts"][reply_ingest._account_key(credentials)]
    assert account["last_uid"] == 41 and len(account["records"]) == 1



def test_corrupt_state_is_not_treated_as_empty(tmp_path):
    path = tmp_path / reply_ingest.STATE_NAME
    path.write_text("not-json")
    with pytest.raises(json.JSONDecodeError):
        reply_ingest._read_state(path)
    assert path.read_text() == "not-json"



def test_poller_uses_configured_cadence_and_survives_pass_failure(monkeypatch, caplog):
    import logging
    from core import config, stores
    monkeypatch.setenv("APPLIEDIN_IMAP_ENABLED", "1")
    monkeypatch.setenv("APPLIEDIN_IMAP_POLL_INTERVAL_SEC", "23")
    settings = type("Settings", (), {"mode": "local"})()
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(stores, "make_stores", lambda *_: object())
    monkeypatch.setattr(reply_ingest, "local_credentials", lambda *_: fake_credentials())
    monkeypatch.setattr(reply_ingest, "ingest_pass", lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic-secret")))
    waiting, release = threading.Event(), threading.Event()
    class StopEvent:
        def is_set(self): return False
        def wait(self, interval):
            assert interval == 23
            waiting.set()
            release.wait(3)
            return True
    thread = threading.Thread(target=reply_ingest.run_poller, args=(StopEvent(),), daemon=True)
    with caplog.at_level(logging.WARNING, logger="core.reply_ingest"):
        thread.start()
        assert waiting.wait(3)
        assert thread.is_alive()
        release.set()
        thread.join(3)
    assert not thread.is_alive() and "synthetic-secret" not in caplog.text
    assert "IMAP poll failed; will retry on next interval" in caplog.text



def test_imap_setup_cloud_refuses_before_prompt_and_store(monkeypatch):
    import builtins
    import getpass
    from core import config, stores
    from cli import imap_setup
    cloud = type("Settings", (), {"mode": "cloud", "local_dir": "."})()
    monkeypatch.setattr(config, "get_settings", lambda: cloud)
    monkeypatch.setattr(builtins, "input", lambda *_: pytest.fail("cloud setup prompted"))
    monkeypatch.setattr(getpass, "getpass", lambda *_: pytest.fail("cloud setup prompted for password"))
    monkeypatch.setattr(stores, "make_stores", lambda *_: pytest.fail("cloud setup built a backend"))
    assert imap_setup()["status"] == "refused"



def test_setup_health_disabled_cloud_timeout_and_success(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import server
    from core import reply_ingest
    from core.config import Settings
    credentials = fake_credentials()
    def request(mode, is_enabled, creds, transport_error=None):
        settings = Settings(mode=mode, config_dir=str(tmp_path / "config"), local_dir=str(tmp_path))
        monkeypatch.setattr(server, "get_settings", lambda: settings)
        monkeypatch.setattr(reply_ingest, "enabled", lambda: is_enabled)
        monkeypatch.setattr(reply_ingest, "local_credentials", lambda *_: creds if mode == "local" else None)
        class Transport:
            def __init__(self, *_):
                if transport_error: raise transport_error
            def __enter__(self): return self
            def __exit__(self, *_): pass
        monkeypatch.setattr("core.imap_readonly.ReadOnlyIMAP", Transport)
        return TestClient(server.create_app()).get("/setup-health").json()["checks"]
    disabled = request("local", False, credentials)
    assert next(c for c in disabled if c["name"] == "IMAP replies")["state"] == "check"
    cloud = request("cloud", True, credentials)
    assert next(c for c in cloud if c["name"] == "IMAP replies")["state"] == "check"
    timeout = request("local", True, credentials, TimeoutError("synthetic-secret"))
    timeout_item = next(c for c in timeout if c["name"] == "IMAP replies")
    assert timeout_item["state"] == "action" and "synthetic-secret" not in str(timeout)
    success = request("local", True, credentials)
    assert next(c for c in success if c["name"] == "IMAP replies")["state"] == "ready"
    assert all(any(c["name"] == name for c in timeout) for name in ("Résumé", "Model access", "PDF rendering"))



def test_local_imap_setup_uses_getpass_file_secrets_and_keeps_mode_0600(monkeypatch, tmp_path):
    import builtins
    import getpass
    from core import config, stores
    from core.storage.local import FileSecrets
    from cli import imap_setup
    settings = type("Settings", (), {"mode": "local", "local_dir": str(tmp_path)})()
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    answers = iter(["imap.example-co.test", "INBOX", "account", "imap.example-co.test", "INBOX", "account"])
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(answers))
    passwords = iter(["synthetic-password-one", "synthetic-password-two"])
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: next(passwords))
    path = tmp_path / "secrets.json"
    monkeypatch.setattr(stores, "make_stores", lambda _settings: type("Stores", (), {"secrets": FileSecrets(path)})())
    assert imap_setup()["status"] == "saved"
    assert path.stat().st_mode & 0o777 == 0o600
    assert imap_setup()["status"] == "saved"
    assert path.stat().st_mode & 0o777 == 0o600
    stored = json.loads(path.read_text())
    assert set(stored) == {"imap"}
    assert stored["imap"]["password"] == "synthetic-password-two"



def test_local_imap_setup_unsafe_target_aborts_before_password_prompt(monkeypatch, tmp_path):
    import builtins
    import getpass
    from core import config
    from cli import imap_setup
    settings = type("Settings", (), {"mode": "local", "local_dir": str(tmp_path)})()
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    target = tmp_path / "target.json"
    target.write_text("{}")
    (tmp_path / "secrets.json").symlink_to(target)
    monkeypatch.setattr(builtins, "input", lambda *_: pytest.fail("unsafe target prompted"))
    monkeypatch.setattr(getpass, "getpass", lambda *_: pytest.fail("unsafe target requested password"))
    with pytest.raises(OSError, match="unsafe secrets file"):
        imap_setup()


def test_classifier_failure_records_null_label_and_advances(tmp_path, monkeypatch):
    client = FakeClient("14", [1], {1: b"Subject: test\r\n\r\n"})
    settings = type("Settings", (), {"mode": "local", "local_dir": str(tmp_path)})()
    tracking = FakeTracking()
    inserted = reply_ingest.ingest_pass(settings, fake_credentials(), fake_stores(tracking), client,
                                       lambda *_: (_ for _ in ()).throw(RuntimeError("classifier")))
    assert inserted == 1
    assert reply_ingest.read_records(settings)[0]["label"] == "unclassified"
    account = json.loads((tmp_path / reply_ingest.STATE_NAME).read_text())["accounts"][reply_ingest._account_key(fake_credentials())]
    assert account["last_uid"] == 1

def test_opt_in_and_interval_parsers_fail_closed(monkeypatch):
    monkeypatch.delenv("APPLIEDIN_IMAP_ENABLED", raising=False)
    monkeypatch.delenv("APPLIEDIN_IMAP_POLL_INTERVAL_SEC", raising=False)
    assert not reply_ingest.enabled() and reply_ingest.poll_interval() is None
    monkeypatch.setenv("APPLIEDIN_IMAP_ENABLED", "true")
    assert reply_ingest.enabled()
    for value in ("invalid", "0", "-5"):
        monkeypatch.setenv("APPLIEDIN_IMAP_POLL_INTERVAL_SEC", value)
        assert reply_ingest.poll_interval() is None
    monkeypatch.setenv("APPLIEDIN_IMAP_POLL_INTERVAL_SEC", "12")
    assert reply_ingest.poll_interval() == 12


def test_state_write_fsyncs_file_and_parent_directory(tmp_path, monkeypatch):
    calls = []
    original = reply_ingest.os.fsync
    monkeypatch.setattr(reply_ingest.os, "fsync", lambda fd: (calls.append(fd), original(fd))[1])
    path = tmp_path / reply_ingest.STATE_NAME
    reply_ingest._write_state(path, {"accounts": {}})
    assert len(calls) == 2


def test_uid_commit_holds_lock_across_state_reread_and_write(tmp_path, monkeypatch):
    original_read, original_write = reply_ingest._read_state, reply_ingest._write_state
    def locked_read(path):
        assert reply_ingest.WRITE_LOCK.locked()
        return original_read(path)
    def locked_write(path, state):
        assert reply_ingest.WRITE_LOCK.locked()
        return original_write(path, state)
    monkeypatch.setattr(reply_ingest, "_read_state", locked_read)
    monkeypatch.setattr(reply_ingest, "_write_state", locked_write)
    key = reply_ingest._account_key(fake_credentials())
    record = {"record_id": "record-1", "date": "", "sender": "", "subject": "",
              "message_id": "", "label": "unclassified", "pk": "UNASSIGNED"}
    reply_ingest._account_state(tmp_path / reply_ingest.STATE_NAME, key, "15")
    assert reply_ingest._commit_uid(tmp_path / reply_ingest.STATE_NAME, key, "15", 1, record, "")


def test_submission_proof_is_unique_global_and_status_bounded():
    row = {"pk": "example-co#one", "company": "example-co", "title": "Backend Engineer",
           "status": "applied", "profile_id": "rot-jobs"}
    same_title = dict(row, pk="example-co#two", profile_id="")
    old = dict(row, pk="example-co#old", status="tailored")
    alias = {"id": "rot-jobs", "email": "jobs@first.test", "company": "example-co",
             "used_pks": [row["pk"]]}
    msg = header(To="JOBS@FIRST.TEST", Subject="Backend Engineer")
    assert reply_ingest.match_message(msg, [same_title, row], [alias]) == row["pk"]
    assert reply_ingest.match_message(msg, [row, same_title], [alias]) == row["pk"]
    assert reply_ingest.match_message(msg, [old], [alias]) == "UNASSIGNED"
    assert reply_ingest.match_message(msg, [row], [dict(alias, used_pks=[])]) == "UNASSIGNED"
    assert reply_ingest.match_message(msg, [row], [dict(alias, used_pks=[old["pk"], row["pk"]])]) == "UNASSIGNED"
    assert reply_ingest.match_message(msg, [row], [dict(alias, used_pks=[row["pk"], same_title["pk"]])]) == "UNASSIGNED"
    assert reply_ingest.match_message(msg, [dict(row, profile_id="rot-different")], [alias]) == "UNASSIGNED"
    assert reply_ingest.match_message(msg, [row], [alias, dict(alias, email="jobs@second.test")]) == "UNASSIGNED"
    # record_submission could write the second domain's submission on the first
    # ID-matched ledger row. Neither address can authenticate that history.
    assert reply_ingest.match_message(header(To="jobs@second.test"), [row],
                                      [alias, dict(alias, email="jobs@second.test")]) == "UNASSIGNED"
    assert reply_ingest.match_message(msg, [row],
                                      [alias, dict(alias, id="rot-other", email="other@test", used_pks=[row["pk"]])]) == "UNASSIGNED"
    assert reply_ingest.match_message(header(To="jobs@first.test, other@test"), [row],
                                      [alias, dict(alias, id="rot-other", email="other@test", used_pks=[])]) == "UNASSIGNED"
    assert reply_ingest.match_message(header(From="example-co Updates <noreply@ats.test>",
                                             Subject="Backend Engineer"), [row], [alias]) == "UNASSIGNED"


def test_historical_multi_use_never_disambiguates_using_subject_or_candidate_filter():
    row = {"pk": "example-co#new", "company": "example-co", "title": "Backend Engineer",
           "status": "applied_manual", "profile_id": "rot-a"}
    old = dict(row, pk="example-co#old", status="discovered", title="Platform Engineer")
    alias = {"id": "rot-a", "email": "reply@unique.test", "company": "example-co",
             "used_pks": [old["pk"], row["pk"]]}
    for subject in ("Application update", "Backend Engineer", row["pk"], "42", "Platform Engineer"):
        assert reply_ingest.match_message(header(To=alias["email"], Subject=subject),
                                          [row], [alias]) == "UNASSIGNED"
        assert reply_ingest.match_message(header(To=alias["email"], Subject=subject),
                                          [old, row], [alias]) == "UNASSIGNED"
    assert reply_ingest.match_message(header(To=alias["email"]), [row],
                                      [dict(alias, used_pks=[row["pk"]])]) == row["pk"]


@pytest.mark.parametrize(("delayed_high", "winning_high"), [(900, 1000), (1000, 1001)])
def test_overlapping_first_scans_respect_committed_uid_floor(
        tmp_path, monkeypatch, delayed_high, winning_high):
    # Both passes snapshot scan_pending before either SEARCH completes. The
    # wider scan wins the floor write; the stale scan must neither fetch
    # historical UIDs nor commit them from its precomputed last-200 window.
    winner = FakeClient("90", range(1, winning_high + 1))

    class DelayedSearch(FakeClient):
        def search_uids(self, query):
            assert query == "ALL"
            self.queries.append(query)
            _, admitted = run_fake_ingest(
                tmp_path, monkeypatch, winner, classifier=lambda *_: "other")
            assert admitted == 200
            return list(range(1, delayed_high + 1))

    delayed = DelayedSearch("90")
    model_calls = []
    _, inserted = run_fake_ingest(
        tmp_path, monkeypatch, delayed,
        classifier=lambda *_: model_calls.append("stale") or "other")
    assert inserted == 0 and delayed.fetched == [] and model_calls == []
    assert winner.fetched == list(range(winning_high - 199, winning_high + 1))
    path = tmp_path / reply_ingest.STATE_NAME
    key = reply_ingest._account_key(fake_credentials())
    account = json.loads(path.read_text())["accounts"][key]
    floor = winning_high - 200
    assert account["uid_floor"] == floor
    assert account["last_uid"] == winning_high
    assert account["unresolved_uids"] == [] and account["scan_pending"] is False
    assert len(account["records"]) == 200
    assert len(account["processed"]) == 200

    # A stale caller could have fetched a UID before the winning floor was
    # persisted; commit must independently enforce the locked floor reread.
    old_record = {"record_id": "excluded-uid", "label": "other", "pk": "UNASSIGNED"}
    assert not reply_ingest._commit_uid(path, key, "90", floor, old_record, "")
    assert json.loads(path.read_text())["accounts"][key] == account


def test_first_scan_floor_and_omitted_uid_hole_survive_without_backfill(tmp_path, monkeypatch):
    class Window(FakeClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.body_reads = []
        def fetch_text_prefix(self, uid):
            self.body_reads.append(uid)
            return b"synthetic inline text"
    messages = {uid: (b"From: recruiter@example-co.test\r\nMessage-ID: <uid-"
                      + str(uid).encode() + b"@example-co.test>\r\n\r\n")
                for uid in range(801, 1002)}
    messages[801] = b""
    first = Window("90", range(1, 1001), messages)
    settings, inserted = run_fake_ingest(tmp_path, monkeypatch, first, classifier=lambda *_: "other")
    key = reply_ingest._account_key(fake_credentials())
    path = tmp_path / reply_ingest.STATE_NAME
    state = json.loads(path.read_text())["accounts"][key]
    assert inserted == 199 and first.fetched == list(range(801, 1001))
    assert first.body_reads == list(range(802, 1001))
    assert state["uid_floor"] == 800 and state["unresolved_uids"] == [801]
    assert state["last_uid"] == 800 and state["scan_pending"] is False

    omitted = Window("90", range(802, 1002), messages)
    _, inserted = run_fake_ingest(tmp_path, monkeypatch, omitted, classifier=lambda *_: "other")
    state = json.loads(path.read_text())["accounts"][key]
    assert omitted.queries == ["UID 801:*"] and omitted.fetched == [1001]
    assert omitted.body_reads == [1001] and inserted == 1
    assert state["last_uid"] == 800 and state["unresolved_uids"] == [801]
    messages[801] = (b"From: recruiter@example-co.test\r\n"
                     b"Message-ID: <uid-801@example-co.test>\r\n\r\n")
    recovered = Window("90", range(801, 1002), messages)
    _, inserted = run_fake_ingest(tmp_path, monkeypatch, recovered, classifier=lambda *_: "other")
    state = json.loads(path.read_text())["accounts"][key]
    assert recovered.queries == ["UID 801:*"] and recovered.fetched == [801]
    assert recovered.body_reads == [801] and inserted == 1
    assert state["unresolved_uids"] == [] and state["last_uid"] == 1001
    assert len(state["records"]) == 201
    assert all(uid > 800 for client in (first, omitted, recovered) for uid in client.fetched)


def test_unknown_and_stale_uidvalidity_cannot_reset_or_commit(tmp_path):
    path = tmp_path / reply_ingest.STATE_NAME
    key = reply_ingest._account_key(fake_credentials())
    reply_ingest._account_state(path, key, "7")
    reply_ingest._set_first_floor(path, key, "7", [801, 802])
    reply_ingest._mark_unresolved(path, key, "7", 801)
    reply_ingest._account_state(path, key, "8")
    reply_ingest._set_first_floor(path, key, "8", [901, 902])
    reply_ingest._mark_unresolved(path, key, "8", 901)
    before = json.loads(path.read_text())
    for old in ("7", "unknown", "", "0", "abc"):
        reply_ingest._account_state(path, key, old)
        reply_ingest._finish_scan(path, key, old)
        reply_ingest._advance_cursor(path, key, old, 9999)
        reply_ingest._mark_unresolved(path, key, old, 888)
        assert not reply_ingest._commit_uid(path, key, old, 9999, {
            "record_id": "stale", "label": "other", "pk": "UNASSIGNED"}, "")
    assert json.loads(path.read_text()) == before


def test_unclassified_exact_uid_retry_keeps_manual_assignment(tmp_path, monkeypatch):
    credentials = fake_credentials()
    settings = type("Settings", (), {"mode": "local", "local_dir": str(tmp_path)})()
    mid = b"Message-ID: <exact@example-co.test>\r\n"
    headers = {4: b"From: recruiter@example-co.test\r\n" + mid + b"Subject: update\r\n\r\n"}
    first = FakeClient("22", [4], headers)
    error = lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic provider error"))
    monkeypatch.setattr("core.rotation.aliases", lambda: [])
    assert reply_ingest.ingest_pass(settings, credentials, fake_stores(), first, classifier=error) == 1
    rows = reply_ingest.read_records(settings)
    assert len(rows) == 1 and rows[0]["label"] == "unclassified"
    rid = rows[0]["record_id"]
    assert reply_ingest.assign(rid, "example-co#manual", settings) == "assigned"
    path = tmp_path / reply_ingest.STATE_NAME
    account = json.loads(path.read_text())["accounts"][reply_ingest._account_key(credentials)]
    assert account["record_uids"] == {rid: {"uid": 4, "uidvalidity": "22"}}
    assert "record_uids" not in rows[0] and "uid_floor" not in rows[0]
    second = FakeClient("22", [4], headers)
    assert reply_ingest.ingest_pass(settings, credentials, fake_stores(), second,
                                    classifier=lambda *_: "rejection") == 0
    final = reply_ingest.read_records(settings)
    assert final[0]["record_id"] == rid and final[0]["pk"] == "example-co#manual"
    assert final[0]["label"] == "rejection" and len(final) == 1
    assert second.fetched == [4]


def test_retry_refuses_mismatched_id_old_generation_and_unlinked_legacy(tmp_path, monkeypatch):
    credentials = fake_credentials()
    settings = type("Settings", (), {"mode": "local", "local_dir": str(tmp_path)})()
    path = tmp_path / reply_ingest.STATE_NAME
    headers = {1: b"Message-ID: <first@test>\r\n\r\n"}
    monkeypatch.setattr("core.rotation.aliases", lambda: [])
    first = FakeClient("41", [1], headers)
    assert reply_ingest.ingest_pass(settings, credentials, fake_stores(), first,
                                    classifier=lambda *_: "unclassified") == 1
    key = reply_ingest._account_key(credentials)
    data = json.loads(path.read_text())
    account = data["accounts"][key]
    rid = account["records"][0]["record_id"]
    account["records"].append({"record_id": "legacy", "message_id": "<legacy@test>",
                                "label": "unclassified", "pk": "UNASSIGNED"})
    reply_ingest._write_state(path, data)
    wrong = FakeClient("41", [1], {1: b"Message-ID: <different@test>\r\n\r\n"})
    assert reply_ingest.ingest_pass(settings, credentials, fake_stores(), wrong,
                                    classifier=lambda *_: "rejection") == 0
    assert [r["label"] for r in reply_ingest.read_records(settings)] == ["unclassified", "unclassified"]
    changed = FakeClient("42", [1], headers)
    assert reply_ingest.ingest_pass(settings, credentials, fake_stores(), changed,
                                    classifier=lambda *_: "rejection") == 0  # same Message-ID dedup
    data = json.loads(path.read_text())["accounts"][key]
    assert data["record_uids"][rid]["uidvalidity"] == "41"
    assert [r["label"] for r in reply_ingest.read_records(settings)] == ["unclassified", "unclassified"]


def test_unreadable_headers_and_unlinked_empty_prefix_do_not_admit(tmp_path, monkeypatch):
    class EmptyPrefix(FakeClient):
        def fetch_text_prefix(self, uid):
            return b""
    messages = {1: b"", 2: b"garbage text", 3: b"Subject: ordinary reply\r\n\r\n",
                4: b"Message-ID: <safe@test>\r\nSubject: ordinary reply\r\n\r\n",
                5: b"Subject: Application security code: 123456\r\n\r\n"}
    client = EmptyPrefix("43", [1, 2, 3, 4, 5], messages)
    calls = []
    settings, inserted = run_fake_ingest(tmp_path, monkeypatch, client,
                                         classifier=lambda *_: calls.append("model") or "other")
    assert inserted == 2 and calls == []
    rows = reply_ingest.read_records(settings)
    assert [r["label"] for r in rows] == ["unclassified", "other"]
    account = json.loads((tmp_path / reply_ingest.STATE_NAME).read_text())["accounts"][reply_ingest._account_key(fake_credentials())]
    assert account["unresolved_uids"] == [1, 2, 3]
    assert account["last_uid"] == 0
    assert all(f"43:{i}" not in account["processed"] for i in (1, 2, 3))



def test_retry_requires_valid_identical_message_id_and_nonblank_prefix(tmp_path, monkeypatch):
    credentials = fake_credentials()
    settings = type("Settings", (), {"mode": "local", "local_dir": str(tmp_path)})()
    monkeypatch.setattr("core.rotation.aliases", lambda: [])
    original = b"Message-ID: <verified@example-co.test>\r\nContent-Type: text/plain\r\n\r\n"
    first = FakeClient("55", [5], {5: original})
    assert reply_ingest.ingest_pass(settings, credentials, fake_stores(), first,
                                    classifier=lambda *_: "unclassified") == 1
    rid = reply_ingest.read_records(settings)[0]["record_id"]
    # An otherwise identical UID with a malformed or changed Message-ID is
    # not a proof that it is the same message.
    for bad in (b"Message-ID: invalid\r\n\r\n",
                b"Message-ID: <different@example-co.test>\r\n\r\n",
                b""):
        client = FakeClient("55", [5], {5: bad})
        assert reply_ingest.ingest_pass(settings, credentials, fake_stores(), client,
                                        classifier=lambda *_: "rejection") == 0
        assert reply_ingest.read_records(settings)[0]["label"] == "unclassified"

    class Blank(FakeClient):
        def fetch_text_prefix(self, uid):
            return b"   "
    assert reply_ingest.ingest_pass(settings, credentials, fake_stores(),
                                    Blank("55", [5], {5: original}),
                                    classifier=lambda *_: "rejection") == 0
    assert reply_ingest.read_records(settings)[0]["label"] == "unclassified"
    assert reply_ingest.ingest_pass(settings, credentials, fake_stores(),
                                    FakeClient("55", [5], {5: original}),
                                    classifier=lambda *_: "rejection") == 0
    records = reply_ingest.read_records(settings)
    assert len(records) == 1 and records[0]["record_id"] == rid
    assert records[0]["label"] == "rejection"


def test_unverified_first_scan_prefix_never_trusts_model_or_advances(tmp_path, monkeypatch):
    class Unreadable(FakeClient):
        def fetch_text_prefix(self, uid):
            return b"\xff\xfe\xff" if uid >= 11 else b" \t "
    messages = {9: b"Subject: ordinary update\r\nContent-Type: text/plain\r\n\r\n",
                10: b"Message-ID: not-rfc-message-id\r\nSubject: ordinary update\r\n\r\n",
                11: b"Subject: ordinary update\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n",
                12: b"Message-ID: <undecodable@test>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"}
    calls = []
    client = Unreadable("61", [9, 10, 11, 12], messages)
    settings, admitted = run_fake_ingest(tmp_path, monkeypatch, client,
                                         classifier=lambda *_: calls.append("model") or "rejection")
    account = json.loads((tmp_path / reply_ingest.STATE_NAME).read_text())["accounts"][reply_ingest._account_key(fake_credentials())]
    assert admitted == 1 and calls == []
    assert account["unresolved_uids"] == [9, 10, 11] and account["last_uid"] == 8
    assert len(reply_ingest.read_records(settings)) == 1
    assert reply_ingest.read_records(settings)[0]["label"] == "unclassified"
    rid = account["records"][0]["record_id"]
    assert account["record_uids"][rid] == {"uid": 12, "uidvalidity": "61"}



def test_legacy_unknown_uidvalidity_has_no_commit_or_cursor_authority(tmp_path, monkeypatch):
    credentials = fake_credentials()
    path = tmp_path / reply_ingest.STATE_NAME
    key = reply_ingest._account_key(credentials)
    original = {"accounts": {key: {
        "uidvalidity": "unknown", "last_uid": 41, "scan_pending": True,
        "uid_floor": None, "unresolved_uids": [7], "processed": ["unknown:9"],
        "message_ids": [], "records": [],
    }}}
    reply_ingest._write_state(path, original)
    assert reply_ingest._account_state(path, key, "unknown")[1] is False
    assert not reply_ingest._processed(path, key, "unknown", 9)
    reply_ingest._set_first_floor(path, key, "unknown", [10, 11])
    reply_ingest._mark_unresolved(path, key, "unknown", 8)
    reply_ingest._advance_cursor(path, key, "unknown", 500)
    reply_ingest._finish_scan(path, key, "unknown")
    assert not reply_ingest._commit_uid(path, key, "unknown", 10, {
        "record_id": "invalid", "label": "other", "pk": "UNASSIGNED"}, "")
    monkeypatch.setattr("core.rotation.aliases", lambda: [])
    unknown = FakeClient("unknown", [10])
    settings = type("Settings", (), {"mode": "local", "local_dir": str(tmp_path)})()
    assert reply_ingest.ingest_pass(settings, credentials, fake_stores(), unknown,
                                    classifier=lambda *_: "other") == 0
    assert unknown.queries == [] and unknown.fetched == []
    assert json.loads(path.read_text()) == original


def _synthetic_outreach_ticket(tmp_path):
    """Private local synthetic setup; no IMAP or SMTP connection is constructed."""
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from core.outreach_contacts import LinkedInSnapshot, VerifiedRecipient
    from core.outreach_sources import FundedNoOpening
    from core.outreach_ledger import Ledger

    now = datetime(2026, 11, 20, 8, tzinfo=UTC)
    s = SimpleNamespace(mode="local", outreach_enabled=True,
                        outreach_timezone="UTC", local_dir=str(tmp_path),
                        outreach_sender="sender@example-co.com",
                        outreach_provider_dsn_verified=True,
                        outreach_dsn_sender_addresses="mailer-daemon@example-co.com")
    ledger = Ledger(s)
    ledger.record_successful_poll(uidvalidity="7",
                                  monitored_address=s.outreach_sender, now=now)
    lead = FundedNoOpening("a"*32, "Example Co", "example-co.com",
                           "https://example-co.com", (), "synthetic",
                           now, "no_openings")
    person = VerifiedRecipient("test@example-co.com", "name:test user", "Test User",
                               "person", "https://example-co.com/team")
    ticket = ledger.reserve_email(
        lead, person, LinkedInSnapshot(frozenset(), frozenset()),
        no_application=True, resume_seed_proved=True, sender_proved=True,
        variant="approved-v1", digest="b"*64, now=now)
    ledger.begin_wire(ticket["id"], source_checked=True, no_application=True,
                      linkedin_checked=True, now=now)
    ledger.finish_wire(ticket["id"], accepted=True, now=now)
    return s, ledger, lead, ticket


def test_cold_thread_human_reply_records_company_stop(tmp_path):
    from core.outreach_ledger import OutreachHold
    s, ledger, lead, ticket = _synthetic_outreach_ticket(tmp_path)
    headers = (f"From: Test User <test@example-co.com>\r\n"
               f"To: sender@example-co.com\r\n"
               f"Message-ID: <reply-one@example-co.com>\r\n"
               f"In-Reply-To: {ticket['message_id']}\r\n\r\n").encode()
    reply_ingest._cold_outreach_message(
        ledger, s, headers, b"Please do not contact me.", "7", 11)
    assert ledger.report(lead.company_id,
                         __import__("core.outreach_contacts", fromlist=["LinkedInSnapshot"])
                         .LinkedInSnapshot(frozenset(), frozenset()))["company_stop"] == "opt_out"
    # Same IMAP UID is idempotent; the new attempt remains forbidden.
    assert not ledger.record_inbound_uid("7", 11, "matched_human_stop")


def test_cold_thread_auto_reply_not_human_and_provider_dsn_must_be_permanent(tmp_path):
    from core.outreach_contacts import LinkedInSnapshot
    s, ledger, lead, ticket = _synthetic_outreach_ticket(tmp_path)
    auto = (f"From: Test User <test@example-co.com>\r\n"
            f"To: sender@example-co.com\r\n"
            f"In-Reply-To: {ticket['message_id']}\r\n"
            f"Auto-Submitted: auto-replied\r\n\r\n").encode()
    reply_ingest._cold_outreach_message(ledger, s, auto, b"Out of office.", "7", 12)
    assert ledger.report(lead.company_id, LinkedInSnapshot(frozenset(), frozenset()))["company_stop"] is None
    dsn = (f"From: mailer-daemon@example-co.com\r\n"
           f"To: sender@example-co.com\r\n"
           f"Content-Type: multipart/report; report-type=delivery-status\r\n"
           f"X-Failed-Recipients: test@example-co.com\r\n"
           f"In-Reply-To: {ticket['message_id']}\r\n\r\n").encode()
    reply_ingest._cold_outreach_message(ledger, s, dsn, b"Status: 4.2.2", "7", 13)
    report = ledger.report(lead.company_id, LinkedInSnapshot(frozenset(), frozenset()))
    assert report["company_email_hold"] == "suspected_dsn_needs_review"
    assert report["global_email_pause"] is False


def test_cold_provider_confirmed_permanent_bounce_is_contact_scoped(tmp_path):
    from core.outreach_contacts import LinkedInSnapshot
    s, ledger, lead, ticket = _synthetic_outreach_ticket(tmp_path)
    dsn = (f"From: mailer-daemon@example-co.com\r\n"
           f"To: sender@example-co.com\r\n"
           f"Content-Type: multipart/report; report-type=delivery-status\r\n"
           f"X-Failed-Recipients: test@example-co.com\r\n"
           f"In-Reply-To: {ticket['message_id']}\r\n\r\n").encode()
    reply_ingest._cold_outreach_message(
        ledger, s, dsn, b"Final-Recipient: rfc822; test@example-co.com\r\nStatus: 5.1.1",
        "7", 14)
    report = ledger.report(lead.company_id, LinkedInSnapshot(frozenset(), frozenset()))
    assert report["company_email_hold"] == "confirmed_hard_bounce_review_required"
    assert report["global_email_pause"] is False


def test_cold_ambiguous_thread_reference_holds_company(tmp_path):
    from core.outreach_contacts import LinkedInSnapshot
    s, ledger, lead, ticket = _synthetic_outreach_ticket(tmp_path)
    headers = (f"From: Unknown <other@sample.test>\r\n"
               f"To: sender@example-co.com\r\n"
               f"In-Reply-To: {ticket['message_id']} <other@sample.test>\r\n"
               f"Message-ID: <reply-ambiguous@sample.test>\r\n\r\n").encode()
    reply_ingest._cold_outreach_message(ledger, s, headers, b"Please stop.", "7", 15)
    report = ledger.report(lead.company_id, LinkedInSnapshot(frozenset(), frozenset()))
    assert report["company_stop"] is None
    assert report["company_email_hold"] == "unthreaded_or_ambiguous_reply"


def test_cold_x_autorespond_header_cannot_count_as_human(tmp_path):
    from core.outreach_contacts import LinkedInSnapshot
    s, ledger, lead, ticket = _synthetic_outreach_ticket(tmp_path)
    headers = (f"From: Test User <test@example-co.com>\r\n"
               f"To: sender@example-co.com\r\n"
               f"In-Reply-To: {ticket['message_id']}\r\n"
               f"X-Autorespond: yes\r\n\r\n").encode()
    reply_ingest._cold_outreach_message(ledger, s, headers, b"Automatic response.", "7", 16)
    report = ledger.report(lead.company_id, LinkedInSnapshot(frozenset(), frozenset()))
    assert report["company_stop"] is None
