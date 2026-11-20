"""Offline owner-copy queue, conservative channel slots and strict local API."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from core import outreach_manual as manual
from core.outreach_contacts import LinkedInSnapshot, VerifiedRecipient
from core.outreach_ledger import Ledger, OutreachHold
from core.outreach_sources import read_no_opening
from discovery.funding import State

T = datetime(2026, 11, 20, 8, tzinfo=UTC)
KEY = "a" * 32
TWO = LinkedInSnapshot(frozenset({"name:alpha person", "name:beta person"}),
                       frozenset())


def setup(tmp_path, monkeypatch, linkedin=TWO):
    s = SimpleNamespace(mode="local", funding_enabled=True, outreach_enabled=True,
                        outreach_manual_enabled=True, local_dir=str(tmp_path),
                        outreach_timezone="UTC", outreach_sender="sender@example-co.com")
    data = State(tmp_path / "funded-watchlist.yaml")
    data.data["pending"][KEY] = {
        "name":"Example Co", "homepage":"https://example-co.com",
        "employer_domain":"example-co.com", "aliases":[],
        "source":"synthetic", "reason":"no_openings", "seen_at":T.isoformat(),
        "next_check":(T + timedelta(days=1)).isoformat(),
        "expires":(T + timedelta(days=40)).isoformat()}
    data.data["deals"][KEY] = {
        "status":"pending", "reason":"no_openings", "source":"synthetic",
        "seen_at":T.isoformat()}
    data.save()
    def page(_settings, _url, _now):
        return "https://example-co.com/team", (
            '<p>Test User Fourth Person Third Person</p>'
            '<a href="https://x.com/fourth">X</a>'
            '<a href="https://x.com/third">X</a>'
            '<a href="https://linkedin.com/in/test-user">Test User</a>')
    monkeypatch.setattr(manual, "_official_page", page)
    monkeypatch.setattr(manual, "approved_variant", lambda *_: {"id":"owner-v1"})
    monkeypatch.setattr(manual, "render_approved", lambda *_: ("Owner subject", "Approved text"))
    monkeypatch.setattr(manual, "read_linkedin", lambda *_args, **_kw: linkedin)
    monkeypatch.setattr(manual, "company_application_clear", lambda *_: True)
    return s


def queue(s, channel, target, name, *, now=T):
    return manual.queue_target(settings=s, company_id=KEY, channel=channel,
                               target_url=target, evidence_url="https://example-co.com/team",
                               person_name=name, variant_id="owner-v1", now=now)


def test_two_existing_linkedin_plus_email_refuses_fourth_manual_copy(tmp_path, monkeypatch):
    s = setup(tmp_path, monkeypatch)
    db = Ledger(s)
    db.record_successful_poll(uidvalidity="2", monitored_address=s.outreach_sender, now=T)
    lead = read_no_opening(s, KEY, now=T)
    db.reserve_email(
        lead, VerifiedRecipient("third@example-co.com", "name:third person",
                                "Third Person", "person",
                                "https://example-co.com/team"), TWO,
        no_application=True, resume_seed_proved=True, sender_proved=True,
        variant="owner-v1", digest="a"*64, now=T)
    row = queue(s, "x", "https://x.com/fourth", "Fourth Person")
    with pytest.raises(OutreachHold, match="hard_company_three_person_cap"):
        manual.act_on_target(settings=s, company_id=KEY, target_id=row["id"],
                             version=1, action="copy", stores=object(), now=T)
    report = manual.manual_report(settings=s, company_id=KEY, now=T)
    assert (report["used"], report["remaining"], report["hard_limit"]) == (3, 0, 3)
    assert report["accepted_first_emails"] == 0  # reserved != accepted


def test_stale_source_warning_allows_provisional_copy_and_mark_once(tmp_path, monkeypatch):
    s = setup(tmp_path, monkeypatch)
    stale = T + timedelta(days=8)
    # Two LinkedIn people; one unresolved person gets exactly one available slot.
    row = queue(s, "x", "https://x.com/third", "", now=stale)
    copied = manual.act_on_target(settings=s, company_id=KEY, target_id=row["id"],
                                  version=1, action="copy", stores=object(), now=stale)
    assert copied["message"] == "Approved text"
    assert copied["state"] == "copied" and copied["version"] == 2
    report = manual.manual_report(settings=s, company_id=KEY, now=stale)
    assert report["source_stale_for_email"] is True
    assert report["provisional"] == 1 and report["used"] == 3
    another = queue(s, "x", "https://x.com/fourth", "", now=stale)
    with pytest.raises(OutreachHold, match="hard_company_three_person_cap"):
        manual.act_on_target(settings=s, company_id=KEY, target_id=another["id"],
                             version=1, action="copy", stores=object(), now=stale)
    marked = manual.act_on_target(settings=s, company_id=KEY, target_id=row["id"],
                                  version=2, action="mark", stores=object(), now=stale)
    assert marked["state"] == "owner_marked_sent" and marked["version"] == 3
    assert manual.act_on_target(settings=s, company_id=KEY, target_id=row["id"],
                                version=3, action="mark", stores=object(),
                                now=stale)["version"] == 3
    with pytest.raises(OutreachHold, match="version_conflict"):
        manual.act_on_target(settings=s, company_id=KEY, target_id=row["id"],
                             version=2, action="mark", stores=object(), now=stale)


def test_future_linkedin_requires_published_target_and_combined_reservation(tmp_path, monkeypatch):
    s = setup(tmp_path, monkeypatch)
    row = queue(s, "linkedin", "https://linkedin.com/in/test-user", "Test User")
    reserved = manual.owner_linkedin_reserve(
        settings=s, company_id=KEY, target_id=row["id"], stores=object(), now=T)
    assert reserved["reserved"] and reserved["remaining"] == 0
    assert manual.manual_report(settings=s, company_id=KEY, now=T)["used"] == 3
    # The existing tracker is read-only; after its separately recorded update,
    # the same name is counted once instead of charged a fourth time.
    snap = LinkedInSnapshot(TWO.people | {"name:test user"}, frozenset())
    again = Ledger(s).reserve_linkedin_slot(
        KEY, "name:test user", snap, no_application=True, now=T)
    assert again["reconciled"] is True and not again["reserved"]
    with pytest.raises(OutreachHold, match="hard_company_three_person_cap"):
        Ledger(s).reserve_linkedin_slot(
            KEY, "name:fourth person", snap, no_application=True, now=T)


def test_stop_prevents_manual_copy_and_owner_response_is_not_smtp(tmp_path, monkeypatch):
    s = setup(tmp_path, monkeypatch, linkedin=LinkedInSnapshot(frozenset(), frozenset()))
    row = queue(s, "x", "https://x.com/fourth", "Fourth Person")
    db = Ledger(s)
    db.record_stop(KEY, kind="opt_out", now=T)
    with pytest.raises(OutreachHold, match="recorded_company"):
        manual.act_on_target(settings=s, company_id=KEY, target_id=row["id"],
                             version=1, action="copy", stores=object(), now=T)


def test_csrf_host_origin_and_loopback_required_without_network(tmp_path, monkeypatch):
    import asyncio
    import httpx
    from fastapi import FastAPI

    s = setup(tmp_path, monkeypatch)
    app = FastAPI()
    manual.register_routes(app, s)
    monkeypatch.setattr("core.stores.make_stores", lambda *_: object())
    monkeypatch.setattr(manual, "act_on_target", lambda **_kwargs: {"state":"copied"})

    async def listening_loopback_app(scope, receive, send):
        # ASGITransport does not operate a listening socket. Model a concrete
        # local listener so the strict Host:port == scope.server:port check is
        # tested instead of depending on the transport's server default.
        if scope["type"] == "http":
            scope = {**scope, "server": ("127.0.0.1", 8788)}
        await app(scope, receive, send)

    async def exercise():
        transport = httpx.ASGITransport(
            app=listening_loopback_app, client=("127.0.0.1", 45678))
        async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8788") as client:
            token_response = await client.get("/outreach/csrf")
            assert token_response.status_code == 200, token_response.json()
            csrf = token_response.json()["csrf"]
            assert (await client.get("/outreach/csrf", headers={
                "Host": "evil.example"})).status_code == 403
            body = {"company_id":KEY, "target_id":"synthetic", "version":1}
            assert (await client.post("/outreach/manual/copy", json=body)).status_code == 403
            wrong = {"Origin":"http://evil.example", "X-Outreach-CSRF":csrf}
            assert (await client.post("/outreach/manual/copy", json=body,
                                      headers=wrong)).status_code == 403
            good = {"Origin":"http://127.0.0.1:8788", "X-Outreach-CSRF":csrf}
            response = await client.post("/outreach/manual/copy", json=body,
                                         headers=good)
            assert response.status_code == 200
            assert response.json()["state"] == "copied"
            assert response.headers["Cache-Control"] == "no-store"
        remote = httpx.ASGITransport(
            app=listening_loopback_app, client=("203.0.113.10", 45678))
        async with httpx.AsyncClient(
                transport=remote, base_url="http://127.0.0.1:8788") as client:
            assert (await client.get("/outreach/csrf")).status_code == 403

    asyncio.run(exercise())


def test_manual_name_on_page_not_bound_to_unrelated_icon_handle(tmp_path, monkeypatch):
    """Ambiguous links use a conservative one-person provisional slot."""
    s = setup(tmp_path, monkeypatch, linkedin=LinkedInSnapshot(frozenset(), frozenset()))
    # The page lists Fourth Person, but the X anchor says only 'X'.
    row = queue(s, "x", "https://x.com/fourth", "Fourth Person")
    copied = manual.act_on_target(settings=s, company_id=KEY, target_id=row["id"],
                                  version=1, action="copy", stores=object(), now=T)
    assert copied["state"] == "copied"
    saved = Ledger(s).manual_rows(KEY)
    assert saved[0]["person_key"].startswith("provisional:manual:")
    report = manual.manual_report(settings=s, company_id=KEY, now=T)
    assert report["provisional"] == 1 and report["used"] == 1
