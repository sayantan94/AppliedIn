"""The dashboard is local, and still reachable from every page the owner has open.

"It only listens on 127.0.0.1" protects against the network. It does nothing
about the browser on the same machine, which will happily send a request here on
behalf of whatever site is in another tab. These pin the three ways that mattered.
"""
from types import SimpleNamespace

import fakeredis
import pytest
from fastapi.testclient import TestClient

import server
from core.apply_queue import ApplyQueue
from core.models import Status
from core.storage.local import FileArtifactStore, FileSecrets, RedisTracking


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLIEDIN_LOCAL_DIR", str(tmp_path / "local"))
    from core.config import get_settings

    get_settings.cache_clear()
    local = tmp_path / "local"
    local.mkdir()
    (local / "secrets.json").write_text('{"portal/acme": {"password": "hunter2"}}')
    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    stores = SimpleNamespace(tracking=tracking,
                             artifacts=FileArtifactStore(local / "artifacts"))
    stores.artifacts.put("resumes", "acme#1.pdf", b"%PDF", "application/pdf")
    monkeypatch.setattr(server, "make_stores", lambda *a: stores)
    monkeypatch.setattr("core.events.emit", lambda *a, **kw: None)
    return TestClient(server.create_app(), base_url="http://127.0.0.1:8787"), stores


def test_an_artifact_key_cannot_leave_the_store(app):
    client, _ = app
    assert client.get("/artifact/resumes/acme%231.pdf").content == b"%PDF"
    # The traversal check guarded one branch and then fell through to a second
    # read with no check at all, so this returned the saved logins.
    for key in ("..%2fsecrets.json", "%2fetc%2fhosts", "resumes%2f..%2f..%2fsecrets.json"):
        got = client.get(f"/artifact/{key}")
        assert got.status_code == 404, key
        assert b"hunter2" not in got.content


def test_the_store_itself_refuses_keys_outside_it(tmp_path):
    # Belt and braces: job ids are read off employers' pages and become keys.
    store = FileArtifactStore(tmp_path / "artifacts")
    (tmp_path / "secrets.json").write_text("{}")
    for key in ("../secrets.json", "/etc/hosts"):
        with pytest.raises(ValueError):
            store.get(key)
        assert not store.exists(key)
    with pytest.raises(ValueError):
        store.put("resumes", "../../escaped.pdf", b"x", "application/pdf")
    assert not (tmp_path / "escaped.pdf").exists()


def test_another_site_cannot_press_the_buttons(app):
    client, stores = app
    stores.tracking.set_status("acme#1", Status.FOUND, company="Acme")
    # A plain <form method=post> on any page needs no CORS preflight. The browser
    # does attach Origin, which is the only thing that tells it apart.
    evil = client.post("/actions/skip/acme%231", headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    assert stores.tracking.get("acme#1")["status"] == "found"
    # The dashboard itself, the extension and the CLI (no Origin) still work.
    for headers in ({"Origin": "http://127.0.0.1:8787"},
                    {"Origin": "chrome-extension://abcdefgh"}, {}):
        assert client.post("/actions/pause", headers=headers).status_code != 403, headers


def test_a_rebound_hostname_is_not_the_dashboard(app):
    client, _ = app
    # DNS rebinding: evil.example:8787 re-points its name at 127.0.0.1. Its Origin
    # and Host then AGREE, so comparing one against the other passes it. Only the
    # Host itself says the address bar was not this dashboard, and it has to be
    # checked on reads too: a same-origin GET carries no Origin at all.
    rebound = {"Host": "evil.example:8787"}
    assert client.get("/stats", headers=rebound).status_code == 403
    assert client.post("/actions/pause", headers={
        **rebound, "Origin": "http://evil.example:8787"}).status_code == 403
    assert client.get("/stats", headers={"Host": "localhost:8787"}).status_code != 403


def test_the_verify_page_does_not_run_what_is_in_its_url(app):
    client, _ = app
    page = client.get("/verify/%3Csvg%20onload=alert(1)%3E").text
    assert "<svg" not in page and "&lt;svg" in page


def test_requeue_refuses_a_row_with_nothing_to_send_or_already_in_hand(app):
    client, stores = app
    # "Requeue" is offered on every failed row, including ones that failed before
    # a résumé existed. Marking those tailored sent the browser in empty-handed.
    stores.tracking.set_status("acme#2", Status.FAILED, company="Acme")
    assert not client.post("/actions/queue-apply/acme%232").json()["ok"]
    assert stores.tracking.get("acme#2")["status"] == "failed"
    # And a row a browser is on right now must not get a second session.
    stores.tracking.set_status("acme#3", Status.SUBMITTING, company="Acme",
                               resume_s3_key="resumes/acme#3.pdf")
    assert not client.post("/actions/queue-apply/acme%233").json()["ok"]
    assert not ApplyQueue(stores.tracking.r).pending()


def test_secrets_are_written_whole_and_private(tmp_path):
    secrets = FileSecrets(tmp_path / "secrets.json")
    secrets.put_json("a", {"password": "x"})
    secrets.put_json("b", {"password": "y"})
    assert secrets.get_json("a") and secrets.get_json("b")
    assert (tmp_path / "secrets.json").stat().st_mode & 0o077 == 0


def test_pasting_the_link_of_a_sent_application_does_not_send_it_again(app, monkeypatch):
    # Discovery files a posting under the board's job id; "Add a job" files it
    # under a hash of the URL. Pasting the link of a role that had already gone
    # out made a second row for it, and the old code reset an existing row to
    # `found` whatever state it was in. Either way the pipeline applied again.
    client, stores = app
    monkeypatch.setattr("discovery.handler.list_watchlist_companies", lambda: ["Acme"])
    started = []
    monkeypatch.setattr("agent.run.run_job", lambda *a, **kw: started.append(a))
    monkeypatch.setattr("tools.jd.fetch_jd_meta", lambda url: {})
    stores.tracking.set_status(
        "acme#9001", Status.APPLIED, company="Acme", confirmation_id="Thanks!",
        jd_url="https://boards.greenhouse.io/acme/jobs/9001")

    out = client.post("/actions/apply-role", json={
        "url": "https://job-boards.greenhouse.io/acme/jobs/9001?gh_src=x",
        "company": "acme"}).json()

    assert not out["ok"] and "already applied" in out["error"]
    assert stores.tracking.get("acme#9001")["status"] == "applied"
    assert len(stores.tracking.all()) == 1 and not started
    # A role nobody has seen is still added, under the tracked spelling of the
    # company: the name is the key its preferences and its cap hang off.
    new = client.post("/actions/apply-role", json={
        "url": "https://job-boards.greenhouse.io/acme/jobs/9002", "company": "acme"}).json()
    assert new["ok"] and new["company"] == "Acme"
