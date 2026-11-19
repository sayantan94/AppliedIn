"""Synthetic, offline tests for isolated optional pixel intake.

No real HTTP listener, proxy, tunnel, SMTP or owner data is used.
"""
import asyncio
import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx

from core.outreach_contacts import LinkedInSnapshot, VerifiedRecipient
from core.outreach_ledger import Ledger
from core.outreach_sources import FundedNoOpening
from core.outreach_tracking import classify_open, pixel_app, pixel_base, register_pixel

NOW = datetime(2026, 11, 20, 8, tzinfo=UTC)
KEY = "a" * 32
URL = "https://pixels.example-co.com"


def settings(tmp_path):
    return SimpleNamespace(
        mode="local", outreach_enabled=True, outreach_timezone="UTC",
        local_dir=str(tmp_path), outreach_sender="sender@example-co.com",
        outreach_pixel_enabled=True, outreach_pixel_healthy=True,
        outreach_pixel_ingress_logs_safe=True, outreach_pixel_base=URL)


def ticket(settings):
    db = Ledger(settings)
    db.record_successful_poll(uidvalidity="2", monitored_address=settings.outreach_sender,
                              now=NOW)
    lead = FundedNoOpening(KEY, "Example Co", "example-co.com",
                           "https://example-co.com", (), "synthetic", NOW, "no_openings")
    contact = VerifiedRecipient("test@example-co.com", "name:test user",
                                "Test User", "person", "https://example-co.com/team")
    attempt = db.reserve_email(
        lead, contact, LinkedInSnapshot(frozenset(), frozenset()),
        no_application=True, resume_seed_proved=True, sender_proved=True,
        variant="approved-v1", digest="a"*64, now=NOW)
    return db, attempt


def test_unconfigured_or_unproven_ingress_is_safe_not_tracked(tmp_path):
    settings_obj = settings(tmp_path)
    db, attempt = ticket(settings_obj)
    settings_obj.outreach_pixel_ingress_logs_safe = False
    assert pixel_base(settings_obj) is None
    assert register_pixel(settings_obj, db, attempt["id"]) == ("", "not_tracked")
    settings_obj.outreach_pixel_ingress_logs_safe = True
    settings_obj.outreach_pixel_healthy = False
    assert register_pixel(settings_obj, db, attempt["id"]) == ("", "not_tracked")
    settings_obj.outreach_pixel_healthy = True
    for unsafe in ("http://pixels.example-co.com", "https://localhost",
                   "https://pixels.example-co.com/foo",
                   "https://pixels.example-co.com/?token=test",
                   "https://user:pass@pixels.example-co.com"):
        settings_obj.outreach_pixel_base = unsafe
        assert pixel_base(settings_obj) is None
    settings_obj.outreach_pixel_base = URL
    assert pixel_base(settings_obj) == URL


def test_pixel_is_per_attempt_private_hashed_and_survives_restart(tmp_path):
    s = settings(tmp_path)
    db, attempt = ticket(s)
    url, mode = register_pixel(s, db, attempt["id"])
    token = url.removeprefix(URL + "/o/").removesuffix(".gif")
    assert mode == "pixel_enabled" and len(token) == 64
    assert token not in db.path.read_bytes().decode("utf-8", "ignore")
    with sqlite3.connect(db.path) as conn:
        assert conn.execute("SELECT token_sha FROM pixel_tokens").fetchone()[0] == (
            hashlib.sha256(token.encode()).hexdigest())
    db2 = Ledger(s)
    assert db2.record_pixel_open(token, "qualified", now=NOW)
    assert db2.record_pixel_open(token, "qualified", now=NOW + timedelta(hours=1))
    assert db2.record_pixel_open(token, "scanner", now=NOW + timedelta(hours=2))
    assert db2.record_pixel_open(token, "unknown", now=NOW + timedelta(hours=3))
    assert db2.record_pixel_open(token, "qualified", now=NOW + timedelta(days=1))
    assert not db2.record_pixel_open("f"*64, "qualified", now=NOW)
    report = db2.report(KEY, LinkedInSnapshot(frozenset(), frozenset()), now=NOW)
    assert report["opens"] == {"qualified": 2, "scanner": 1, "unknown": 1}
    assert report["attempts"][0]["tracking"] == "pixel_enabled"


def test_pixel_app_only_exact_get_public_host_and_local_transport(tmp_path):
    s = settings(tmp_path)
    db, attempt = ticket(s)
    url, _ = register_pixel(s, db, attempt["id"])
    token_path = "/" + url.split("/", 3)[3]
    app = pixel_app(s)

    async def requests():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 45678))
        async with httpx.AsyncClient(transport=transport, base_url=URL) as client:
            valid = await client.get(token_path, headers={"User-Agent":"Test Browser"})
            assert valid.status_code == 200 and valid.content.startswith(b"GIF89a")
            assert valid.headers["Cache-Control"] == "no-store, private"
            assert (await client.post(token_path)).status_code == 404
            assert (await client.get(token_path + "?utm=unsafe")).status_code == 404
            assert (await client.get("/admin/" + token_path)).status_code == 404
            assert (await client.get(token_path, headers={"Host":"evil.example"})).status_code == 404
            assert (await client.get("/outreach/report")).status_code == 404
        remote = httpx.ASGITransport(app=app, client=("203.0.113.1", 4321))
        async with httpx.AsyncClient(transport=remote, base_url=URL) as client:
            assert (await client.get(token_path)).status_code == 404
    asyncio.run(requests())


def test_durable_failure_returns_503_and_never_exposes_token(tmp_path, monkeypatch):
    from core import outreach_tracking

    s = settings(tmp_path)
    db, attempt = ticket(s)
    url, _ = register_pixel(s, db, attempt["id"])
    token_path = "/" + url.split("/", 3)[3]

    def failed_event(*_args, **_kwargs):
        raise sqlite3.OperationalError("private ledger internal failure")
    monkeypatch.setattr(outreach_tracking.Ledger, "record_pixel_open", failed_event)

    async def request():
        transport = httpx.ASGITransport(app=pixel_app(s), client=("127.0.0.1", 4444))
        async with httpx.AsyncClient(transport=transport, base_url=URL) as client:
            result = await client.get(token_path)
        assert result.status_code == 503
        assert token_path not in result.text
        assert result.content == b""
        assert "no-store" in result.headers["Cache-Control"]
    asyncio.run(request())


def test_unknown_and_scanners_do_not_count_as_qualified():
    assert classify_open([]) == "unknown"
    assert classify_open([(b"user-agent", b"Image Scanner")]) == "scanner"
    assert classify_open([(b"user-agent", b"Mozilla Test Browser")]) == "qualified"
