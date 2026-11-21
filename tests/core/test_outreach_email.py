"""Immutable variants and pixel-less SMTP only with fakes."""
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from core.outreach_email import approved_variant, render_approved
from core.outreach_ledger import OutreachHold
from core.outreach_sources import FundedNoOpening
from core.outreach_contacts import VerifiedRecipient


def test_approved_template_digest_and_bounded_render(tmp_path):
    v = {"id": "generic-v1", "kind": "first", "subject": "Hello {first_name}",
         "body": "Hello {first_name}, I am interested in {company_name} and {role_family}.",
         "role_family": "product management"}
    digest = hashlib.sha256(json.dumps(v, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=True).encode()).hexdigest()
    path = tmp_path / "templates.json"
    path.write_text(json.dumps({"variants": [{**v, "approved": True, "sha256": digest}]}))
    path.chmod(0o600)
    settings = SimpleNamespace(local_dir=str(tmp_path), outreach_approvals_file=path.name)
    item = approved_variant(settings, "generic-v1", "first")
    lead = FundedNoOpening("a"*32, "Example Co", "example-co.com",
                           "https://example-co.com", (), "synthetic",
                           datetime(2026, 11, 20, tzinfo=UTC), "no_openings")
    contact = VerifiedRecipient("test@example-co.com", "name:test user", "Test User",
                                "person", "https://example-co.com/team")
    assert render_approved(item, lead, contact)[0] == "Hello Test"
    path.write_text(json.dumps({"variants": [{**v, "body": "Changed",
                                              "approved": True, "sha256": digest}]}))
    with pytest.raises(OutreachHold, match="approved_immutable"):
        approved_variant(settings, "generic-v1", "first")


def test_unapproved_funding_sentence_or_format_object_not_rendered():
    lead = FundedNoOpening("a"*32, "Example Co", "example-co.com",
                           "https://example-co.com", (), "synthetic",
                           datetime(2026, 11, 20, tzinfo=UTC), "no_openings")
    contact = VerifiedRecipient("test@example-co.com", "name:test user", "Test User",
                                "person", "https://example-co.com/team")
    with pytest.raises(OutreachHold, match="unsupported_template_slot"):
        render_approved({"subject": "{company_name.__class__}",
                         "body": "Test", "role_family": "product management"}, lead, contact)


def _mail_fake_setup(tmp_path, monkeypatch, *, collision_after_lookup=False,
                     real_sender=False):
    """A complete in-memory SMTP boundary; never starts a live connection."""
    from core import outreach_email as mail
    from core.outreach_contacts import LinkedInSnapshot
    from core.outreach_ledger import Ledger
    from datetime import timedelta
    when = datetime(2026, 11, 20, 10, tzinfo=UTC)
    lead = FundedNoOpening("a"*32, "Example Co", "example-co.com",
                           "https://example-co.com", (), "synthetic", when, "no_openings")
    contact = VerifiedRecipient("test@example-co.com", "name:test user", "Test User",
                                "person", "https://example-co.com/team")
    s = SimpleNamespace(mode="local", outreach_enabled=True, local_dir=str(tmp_path),
                        outreach_timezone="UTC", outreach_sender="sender@example-co.com",
                        outreach_smtp_host="smtp.example-co.com", outreach_smtp_port=587,
                        outreach_smtp_username="synthetic", outreach_smtp_password="synthetic",
                        outreach_sender_authorized=True, outreach_inbound_verified=True)
    Ledger(s).record_successful_poll(
        uidvalidity="3", monitored_address=s.outreach_sender, now=when)
    calls = []
    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            calls.append(("constructed", args, kwargs))
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def ehlo(self): pass
        def starttls(self, **kwargs): calls.append(("starttls", kwargs))
        def login(self, username, password): calls.append(("login", username, password))
        def send_message(self, message, **kwargs):
            calls.append(("message", message, kwargs))
            return {}
    source_count = [0]
    def read_source(*args, **kwargs):
        source_count[0] += 1
        if collision_after_lookup and source_count[0] > 2:
            from dataclasses import replace
            return replace(lead, homepage="https://changed.example-co.com")
        return lead
    monkeypatch.setattr(mail, "read_no_opening", read_source)
    monkeypatch.setattr(mail, "read_linkedin",
                        lambda *args, **kwargs: LinkedInSnapshot(frozenset(), frozenset()))
    if not real_sender:
        monkeypatch.setattr(mail, "_sender", lambda _: s.outreach_sender)
    monkeypatch.setattr(mail, "_official_page", lambda *args: (
        "https://example-co.com/team", "<a href='mailto:test@example-co.com'>Test User</a>"))
    monkeypatch.setattr(mail, "verify_published_contact", lambda **kwargs: contact)
    monkeypatch.setattr(mail, "company_application_clear", lambda *args: True)
    monkeypatch.setattr(mail, "approved_variant", lambda *args: {
        "id":"synthetic-v1","sha256":"a"*64,
        "subject":"Hello","body":"Synthetic approved email.","role_family":"PM"})
    def fake_render_approved(_variant, _lead, _contact, *, funding_fact=""):
        # The no-fact SMTP fixture must accept the same keyword-only
        # rendering contract as production. Any invented fact is a failure.
        assert funding_fact == ""
        return "Hello", "Synthetic approved email."

    monkeypatch.setattr(mail, "render_approved", fake_render_approved)
    monkeypatch.setattr(mail, "original_seed_pdf", lambda _: b"%PDF-1.4 synthetic")
    return s, when, FakeSMTP, calls


def _send_fake(mail, **kwargs):
    """All fake SMTP regressions use the persisted queue, never direct sends."""
    from core.outreach_ledger import Ledger
    settings=kwargs["settings"]
    now=kwargs["now"]
    intent=mail.enqueue_first(
        settings=settings,company_id=kwargs["company_id"],
        contact_page_url=kwargs["contact_page_url"],
        exact_address=kwargs["exact_address"],person_name=kwargs["person_name"],
        variant_id=kwargs["variant_id"],now=now)
    assert intent["state"]=="queued"
    claim=Ledger(settings).claim_queue(now=now)
    assert claim is not None and claim["id"]==intent["queue_id"]
    return mail.send_approved(
        settings=settings,queue_id=claim["id"],owner_token=claim["owner_token"],
        stores=kwargs["stores"],now=now,smtp_factory=kwargs["smtp_factory"])


def test_fake_smtp_uses_persisted_message_id_and_zero_pixel(tmp_path, monkeypatch):
    from core import outreach_email as mail
    s, now, factory, calls = _mail_fake_setup(tmp_path, monkeypatch)
    result = _send_fake(mail,
        settings=s, company_id="a"*32, contact_page_url="https://example-co.com/team",
        exact_address="test@example-co.com", person_name="Test User",
        variant_id="synthetic-v1", stores=object(), now=now,
        smtp_factory=factory)
    assert result["state"] == "confirmed_smtp_accepted"
    assert result["tracking"] == "not_tracked"
    sent = [item for item in calls if item[0] == "message"]
    assert len(sent) == 1
    message = sent[0][1]
    assert message["Message-ID"].startswith("<")
    assert message["In-Reply-To"] is None
    assert "http" not in message.get_body(preferencelist=("plain",)).get_content().lower()


def test_changed_funding_identity_blocks_fake_smtp_before_wire(tmp_path, monkeypatch):
    from core import outreach_email as mail
    s, now, factory, calls = _mail_fake_setup(
        tmp_path, monkeypatch, collision_after_lookup=True)
    with pytest.raises(OutreachHold, match="source_or_contact_changed_before_wire"):
        _send_fake(mail,
            settings=s, company_id="a"*32,
            contact_page_url="https://example-co.com/team",
            exact_address="test@example-co.com", person_name="Test User",
            variant_id="synthetic-v1", stores=object(), now=now,
            smtp_factory=factory)
    assert not calls


def test_owner_approval_change_before_wire_blocks_fake_smtp(tmp_path, monkeypatch):
    from core import outreach_email as mail
    s, now, factory, calls = _mail_fake_setup(tmp_path, monkeypatch)
    approvals = [0]
    def mutable_approval(*_args):
        approvals[0] += 1
        return {"id":"synthetic-v1",
                "sha256":("a" if approvals[0] <= 2 else "b")*64,
                "subject":"Hello","body":"Synthetic approved email.","role_family":"PM"}
    monkeypatch.setattr(mail, "approved_variant", mutable_approval)
    with pytest.raises(OutreachHold, match="approved_variant_or_resume_changed_before_wire"):
        _send_fake(mail,
            settings=s, company_id="a"*32,
            contact_page_url="https://example-co.com/team",
            exact_address="test@example-co.com", person_name="Test User",
            variant_id="synthetic-v1", stores=object(), now=now,
            smtp_factory=factory)
    assert approvals[0] == 3
    assert not calls


def test_absent_original_seed_never_passes_on_empty_sha(tmp_path, monkeypatch):
    from core.outreach_email import original_seed_pdf
    import agent.run
    pdf = tmp_path / "original.pdf"
    payload = b"%PDF-1.4 synthetic"
    pdf.write_bytes(payload)
    pdf.chmod(0o600)
    manifest = tmp_path / "resume.json"
    manifest.write_text(json.dumps({
        "pdf_path": pdf.name, "sha256": hashlib.sha256(payload).hexdigest(),
        "resume_seed": "empty-base-fingerprint"}))
    manifest.chmod(0o600)
    monkeypatch.setattr(agent.run, "_base_latex", lambda: "")
    monkeypatch.setattr(agent.run, "seed_fingerprint", lambda: "empty-base-fingerprint")
    settings = SimpleNamespace(local_dir=str(tmp_path),
                               outreach_resume_manifest=manifest.name)
    with pytest.raises(OutreachHold, match="current_original_seed_pdf_required"):
        original_seed_pdf(settings)


def test_fake_smtp_optional_pixel_only_with_operator_safe_ingress(tmp_path, monkeypatch):
    """No real SMTP or HTTPS listener; token URL appears only under opt-in."""
    from core import outreach_email as mail
    s, now, factory, calls = _mail_fake_setup(tmp_path, monkeypatch)
    s.outreach_pixel_enabled = True
    s.outreach_pixel_healthy = True
    s.outreach_pixel_ingress_logs_safe = True
    s.outreach_pixel_base = "https://pixels.example-co.com"
    # Pixel HTML must escape approved text rather than interpret it as markup.
    monkeypatch.setattr(mail, "render_approved",
                        lambda *_args, **_kwargs: (
                            "Hello", 'Synthetic <tag> & "body"'))
    result = _send_fake(mail,
        settings=s, company_id="a"*32,
        contact_page_url="https://example-co.com/team",
        exact_address="test@example-co.com", person_name="Test User",
        variant_id="synthetic-v1", stores=object(), now=now,
        smtp_factory=factory)
    assert result["tracking"] == "pixel_enabled"
    messages = [x[1] for x in calls if x[0] == "message"]
    assert len(messages) == 1
    email = messages[0]
    plain = email.get_body(preferencelist=("plain",)).get_content()
    image = email.get_body(preferencelist=("html",)).get_content()
    assert "pixels.example-co.com/o/" not in plain
    assert 'Synthetic <tag> & "body"' in plain
    assert "Synthetic &lt;tag&gt; &amp; &quot;body&quot;" in image
    assert "Synthetic <tag>" not in image
    assert 'src="https://pixels.example-co.com/o/' in image
    assert "test@example-co.com" not in image
    assert email["Message-ID"].startswith("<")
    from core.outreach_ledger import Ledger
    from core.outreach_contacts import LinkedInSnapshot
    report = Ledger(s).report("a"*32, LinkedInSnapshot(frozenset(), frozenset()),
                              now=now)
    assert report["attempts"][0]["tracking"] == "pixel_enabled"


def test_pixel_ingress_unverified_still_sends_without_tracking(tmp_path, monkeypatch):
    from core import outreach_email as mail
    s, now, factory, calls = _mail_fake_setup(tmp_path, monkeypatch)
    s.outreach_pixel_enabled = True
    s.outreach_pixel_healthy = True
    s.outreach_pixel_ingress_logs_safe = False
    s.outreach_pixel_base = "https://pixels.example-co.com"
    result = _send_fake(mail,
        settings=s, company_id="a"*32,
        contact_page_url="https://example-co.com/team",
        exact_address="test@example-co.com", person_name="Test User",
        variant_id="synthetic-v1", stores=object(), now=now,
        smtp_factory=factory)
    assert result["tracking"] == "not_tracked"
    message = [x[1] for x in calls if x[0] == "message"][0]
    assert message.get_body(preferencelist=("html",)) is None
    assert "pixel" not in message.get_body(preferencelist=("plain",)).get_content().lower()


def test_private_approved_funding_quote_requires_exact_public_witness(tmp_path, monkeypatch):
    """No live article fetch: the verified source and forged source are fakes."""
    from core import outreach_email as mail
    lead = FundedNoOpening(
        "a" * 32, "Example Co", "example-co.com",
        "https://example-co.com", (), "synthetic",
        datetime(2026, 11, 20, tzinfo=UTC), "no_openings")
    contact = VerifiedRecipient("test@example-co.com", "name:test user",
                                "Test User", "person", "https://example-co.com/team")
    quote = "Example Co raised a seed round."
    source_url = "https://news.example.test/example-co-seed"
    proof = {"company_id": lead.company_id, "source_url": source_url, "quote": quote}
    proof["sha256"] = hashlib.sha256(json.dumps(
        proof, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode()).hexdigest()
    private = tmp_path / "fact.json"
    private.write_text(json.dumps(proof))
    private.chmod(0o600)
    settings = SimpleNamespace(local_dir=str(tmp_path),
                               outreach_funding_fact_manifest=private.name)
    calls = []
    monkeypatch.setattr(mail, "_official_page", lambda *_args: (
        calls.append("article") or source_url,
        "<article>Example Co raised a seed round.</article>"))
    verified = mail.verified_funding_fact(
        settings, lead, datetime(2026, 11, 20, tzinfo=UTC))
    item = {"subject": "Hello {first_name}",
            "body": "Our interest follows this announcement: {funding_fact} "
                    "I work in {role_family} at {company_name}.",
            "role_family": "product management"}
    assert quote in mail.render_approved(item, lead, contact, funding_fact=verified)[1]
    assert calls == ["article"]
    with pytest.raises(OutreachHold, match="verified_funding_fact_or_approved_generic"):
        mail.render_approved(item, lead, contact)
    generic = {**item, "body": "Hello {first_name}, I work in {role_family}."}
    assert "I work in" in mail.render_approved(generic, lead, contact)[1]
    # A forged fact on a different page is not independently corroborated.
    monkeypatch.setattr(mail, "_official_page", lambda *_args: (
        source_url, "<article>Unrelated synthetic announcement.</article>"))
    with pytest.raises(OutreachHold, match="verified_private_funding_fact_required"):
        mail.verified_funding_fact(settings, lead, datetime(2026, 11, 20, tzinfo=UTC))
    private.write_text(json.dumps({**proof, "quote": "Example Co raised $999 billion."}))
    with pytest.raises(OutreachHold, match="verified_private_funding_fact_required"):
        mail.verified_funding_fact(settings, lead, datetime(2026, 11, 20, tzinfo=UTC))


def test_unnamed_official_organization_mailbox_requires_no_name_template(tmp_path,monkeypatch):
    """An officially published org inbox is not proof of a person's first name."""
    from core import outreach_email as mail
    from core.outreach_ledger import Ledger

    now=datetime(2026,11,20,10,tzinfo=UTC)
    lead=FundedNoOpening("a"*32,"Example Co","example-co.com",
                         "https://example-co.com",(),"synthetic",now,"no_openings")
    s=SimpleNamespace(mode="local",outreach_enabled=True,local_dir=str(tmp_path))
    page="https://example-co.com/contact"
    markup='<a href="mailto:hello@example-co.com">Contact</a>'
    monkeypatch.setattr(mail,"read_no_opening",lambda *args,**kwargs: lead)
    monkeypatch.setattr(mail,"_official_page",lambda *args: (page,markup))
    variant={"id":"team","sha256":"a"*64,"subject":"Hello {first_name}",
             "body":"Hi from {company_name}","role_family":"PM"}
    monkeypatch.setattr(mail,"approved_variant",lambda *args: variant)
    fields=dict(settings=s,company_id=lead.company_id,contact_page_url=page,
                exact_address="hello@example-co.com",person_name="",
                variant_id="team",now=now)
    with pytest.raises(OutreachHold,match="verified_name_or_approved_team_variant_required"):
        mail.enqueue_first(**fields)
    assert not (tmp_path/"outreach-ledger.sqlite3").exists()

    variant["subject"]="Hello"
    result=mail.enqueue_first(**fields)
    assert result["state"]=="queued"
    with Ledger(s)._tx() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]==0


def test_followup_mime_subject_exactly_threads_saved_unicode_root(tmp_path,monkeypatch):
    """An approved follow-up body must never replace the first logical Subject."""
    from datetime import timedelta
    from core import outreach_email as mail
    from core.outreach_ledger import Ledger

    settings,now,factory,calls=_mail_fake_setup(tmp_path,monkeypatch)
    settings.outreach_followups_auto=True
    settings.outreach_followup_stage_variants=(
        '{"1":{"named":"stage1-named","team":"stage1-team"}}')
    monkeypatch.setattr(mail,"approved_variant",lambda settings,variant_id,kind: {
        "id":variant_id,"sha256":"a"*64,"subject":"Unrelated follow-up",
        "body":"Owner-approved synthetic text.","role_family":"PM"})
    monkeypatch.setattr(mail,"render_approved",lambda item,*args,**kwargs: (
        "Résumé – exact original Subject" if item["id"]=="synthetic-v1"
        else "Unrelated follow-up","Synthetic approved body."))
    _send_fake(mail,settings=settings,company_id="a"*32,
               contact_page_url="https://example-co.com/team",
               exact_address="test@example-co.com",person_name="Test User",
               variant_id="synthetic-v1",stores=object(),now=now,smtp_factory=factory)
    later=now+timedelta(days=7)
    ledger=Ledger(settings)
    ledger.record_successful_poll(uidvalidity="3",
                                   monitored_address=settings.outreach_sender,now=later)
    due=ledger.due_followups(now=later,limit=1)[0]
    queued=mail.enqueue_due_followup(settings=settings,candidate=due,now=later)
    claimed=ledger.claim_queue(now=later)
    assert claimed and claimed["id"]==queued["queue_id"]
    mail.send_approved(settings=settings,queue_id=claimed["id"],
                       owner_token=claimed["owner_token"],stores=object(),
                       now=later,smtp_factory=factory)
    messages=[call[1] for call in calls if call[0]=="message"]
    assert len(messages)==2
    assert str(messages[0]["Subject"])=="Résumé – exact original Subject"
    assert str(messages[1]["Subject"])=="Re: Résumé – exact original Subject"
    assert str(messages[1]["In-Reply-To"])==str(messages[0]["Message-ID"])
    assert str(messages[1]["References"])==str(messages[0]["Message-ID"])


def test_preflight_crossing_1700_releases_definite_no_wire_without_smtp(tmp_path,monkeypatch):
    """The last clock check must not dispatch an earlier 16:59 preflight at 17:00."""
    from core import outreach_email as mail
    from core.outreach_ledger import Ledger

    settings,_,factory,calls=_mail_fake_setup(tmp_path,monkeypatch)
    near_close=datetime(2026,11,20,16,59,tzinfo=UTC)
    at_close=datetime(2026,11,20,17,0,tzinfo=UTC)
    ledger=Ledger(settings)
    ledger.record_successful_poll(uidvalidity="3",
                                   monitored_address=settings.outreach_sender,
                                   now=near_close)
    queued=mail.enqueue_first(
        settings=settings,company_id="a"*32,
        contact_page_url="https://example-co.com/team",
        exact_address="test@example-co.com",person_name="Test User",
        variant_id="synthetic-v1",now=near_close)
    claim=ledger.claim_queue(now=near_close)
    instants=iter((near_close,near_close,at_close))
    with pytest.raises(OutreachHold,match="outreach_business_window_closed"):
        mail.send_approved(settings=settings,queue_id=claim["id"],
                           owner_token=claim["owner_token"],stores=object(),
                           now=near_close,clock=lambda: next(instants,at_close),
                           smtp_factory=factory)
    assert not calls
    assert ledger.queue_status(queued["queue_id"])["state"]=="held"
    with ledger._tx() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts WHERE "
                            "state!='definite_pre_wire_failure'").fetchone()[0]==0
        assert conn.execute("SELECT active_attempt FROM email_dispatch").fetchone()[0] is None


def test_post_handoff_window_abort_releases_first_email_slot_for_repair(tmp_path, monkeypatch):
    """A successful handoff with no SMTP transport must permit the same-intent retry."""
    from core import outreach_email as mail
    from core.outreach_ledger import Ledger

    settings, _, factory, calls = _mail_fake_setup(tmp_path, monkeypatch)
    near_close = datetime(2026, 11, 20, 16, 59, tzinfo=UTC)
    at_close = datetime(2026, 11, 20, 17, tzinfo=UTC)
    reopened = datetime(2026, 11, 23, 10, tzinfo=UTC)
    ledger = Ledger(settings)
    ledger.record_successful_poll(
        uidvalidity="3", monitored_address=settings.outreach_sender, now=near_close)
    queued = mail.enqueue_first(
        settings=settings, company_id="a"*32,
        contact_page_url="https://example-co.com/team",
        exact_address="test@example-co.com", person_name="Test User",
        variant_id="synthetic-v1", now=near_close)
    queue_id = queued["queue_id"]
    claim = ledger.claim_queue(now=near_close)
    assert claim and claim["id"] == queue_id

    # The first three clocks cover preflight, reservation and begin_wire.
    # The fourth closes the business window after durable handoff, before SMTP.
    instants = iter((near_close, near_close, near_close, at_close))
    gap = SimpleNamespace(randint=lambda lower, upper: 180)
    with pytest.raises(OutreachHold, match="outreach_business_window_closed"):
        mail.send_approved(
            settings=settings, queue_id=queue_id,
            owner_token=claim["owner_token"], stores=object(),
            now=near_close, clock=lambda: next(instants, at_close),
            smtp_factory=factory, gap_rng=gap)
    assert not calls  # Not even the fake SMTP constructor was reached.
    status = ledger.queue_status(queue_id)
    assert status["state"] == "held"
    assert status["reason"] == "owner_no_wire_repair_required"
    with ledger._tx() as conn:
        original = conn.execute(
            "SELECT id,message_id,state FROM attempts WHERE queue_id=?",
            (queue_id,)).fetchone()
        assert original[2] == "definite_pre_wire_failure"
        assert conn.execute(
            "SELECT channel FROM people WHERE company_id=? AND person_key=?",
            ("a"*32, "name:test user")).fetchone() is None
        assert conn.execute(
            "SELECT active_attempt,last_handoff,next_not_before "
            "FROM email_dispatch WHERE id=1").fetchone() == (
                None, near_close.timestamp(), near_close.timestamp() + 180)

    # Only positively proved no-wire, owner-selected repair may re-admit it.
    with pytest.raises(OutreachHold, match="charged_or_unproved_wire"):
        ledger.repair_queue(
            queue_id, expected_version=status["version"], expected_state="held",
            variant_id="synthetic-v1", digest="a"*64)
    repaired = ledger.repair_queue(
        queue_id, expected_version=status["version"], expected_state="held",
        variant_id="synthetic-v1", digest="a"*64, owner_no_wire_proved=True,
        now=reopened)
    assert repaired["queue_id"] == queue_id
    ledger.record_successful_poll(
        uidvalidity="3", monitored_address=settings.outreach_sender, now=reopened)
    retry_claim = ledger.claim_queue(now=reopened)
    assert retry_claim and retry_claim["id"] == queue_id
    result = mail.send_approved(
        settings=settings, queue_id=queue_id,
        owner_token=retry_claim["owner_token"], stores=object(),
        now=reopened, smtp_factory=factory, gap_rng=gap)
    assert result["state"] == "confirmed_smtp_accepted"
    sent = [item for item in calls if item[0] == "message"]
    assert len(sent) == 1
    assert len([item for item in calls if item[0] == "constructed"]) == 1
    with ledger._tx() as conn:
        attempts = conn.execute(
            "SELECT id,message_id,state FROM attempts WHERE queue_id=? "
            "ORDER BY reserved_at", (queue_id,)).fetchall()
        assert len(attempts) == 2
        assert attempts[0] == original
        assert attempts[1][0] != original[0]
        assert attempts[1][1] != original[1]
        assert attempts[1][2] == "confirmed_smtp_accepted"
        assert conn.execute(
            "SELECT channel FROM people WHERE company_id=? AND person_key=?",
            ("a"*32, "name:test user")).fetchone() == ("email",)
    assert str(sent[0][1]["Message-ID"]) == attempts[1][1]


def test_owner_approval_rotated_after_mime_before_handoff_is_no_wire(tmp_path,monkeypatch):
    """A locally assembled message is not permission to send revoked approved text."""
    from core import outreach_email as mail
    from core.outreach_ledger import Ledger

    settings,now,factory,calls=_mail_fake_setup(tmp_path,monkeypatch)
    original=mail.approved_variant
    rotated=[False]

    def approval(*args):
        item=original(*args)
        return {**item,"sha256":("b" if rotated[0] else "a")*64}

    def finish_mime_setup():
        rotated[0]=True
        return object()

    monkeypatch.setattr(mail,"approved_variant",approval)
    monkeypatch.setattr(mail.ssl,"create_default_context",finish_mime_setup)
    with pytest.raises(OutreachHold,match="frozen_stage_approval_changed_before_wire"):
        _send_fake(mail,settings=settings,company_id="a"*32,
                   contact_page_url="https://example-co.com/team",
                   exact_address="test@example-co.com",person_name="Test User",
                   variant_id="synthetic-v1",stores=object(),now=now,
                   smtp_factory=factory)
    assert not calls
    with Ledger(settings)._tx() as conn:
        assert conn.execute("SELECT state FROM attempts").fetchone()[0] == (
            "definite_pre_wire_failure")
        assert conn.execute("SELECT active_attempt FROM email_dispatch").fetchone()[0] is None


def _imap_secret():
    """Entirely synthetic four-string record matching the local IMAP contract."""
    return {"host": "imap.gmail.com", "mailbox": "INBOX",
            "username": "sender@example-co.com", "password": "synthetic-only"}


def _secret_smtp_setup(tmp_path, monkeypatch, *, record=None, error=None):
    """Real sender guard, injected secret store, ordered ledger and TLS spies."""
    from core import outreach_email as mail
    from core.outreach_ledger import Ledger

    settings, now, fake_smtp, calls = _mail_fake_setup(
        tmp_path, monkeypatch, real_sender=True)
    events = []
    hooks = {"after_begin": None}

    class FakeSecrets:
        def __init__(self):
            self.reads = []
        def get_json(self, key):
            self.reads.append(key)
            events.append("imap_read")
            assert key == "imap"
            if error is not None:
                raise error
            return record

    secrets = FakeSecrets()
    stores = SimpleNamespace(secrets=secrets)
    original_reserve = Ledger.reserve_email
    original_begin = Ledger.begin_wire
    original_release = Ledger.release_claim

    def reserve(self, *args, **kwargs):
        events.append("reserve")
        return original_reserve(self, *args, **kwargs)

    def begin(self, *args, **kwargs):
        result = original_begin(self, *args, **kwargs)
        events.append("begin_wire")
        if hooks["after_begin"] is not None:
            hooks["after_begin"]()
        return result

    def release(self, *args, **kwargs):
        events.append(("release", args[2]))
        return original_release(self, *args, **kwargs)

    monkeypatch.setattr(Ledger, "reserve_email", reserve)
    monkeypatch.setattr(Ledger, "begin_wire", begin)
    monkeypatch.setattr(Ledger, "release_claim", release)

    def ssl_factory(*args, **kwargs):
        events.append("ssl")
        return fake_smtp(*args, **kwargs)

    def plain_factory(*args, **kwargs):
        events.append("plain")
        return fake_smtp(*args, **kwargs)

    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", ssl_factory)
    monkeypatch.setattr(mail.smtplib, "SMTP", plain_factory)
    return mail, settings, now, stores, calls, events, hooks


def _send_secret_fake(mail, settings, now, stores):
    return _send_fake(
        mail, settings=settings, company_id="a"*32,
        contact_page_url="https://example-co.com/team",
        exact_address="test@example-co.com", person_name="Test User",
        variant_id="synthetic-v1", stores=stores, now=now,
        smtp_factory=None)


@pytest.mark.parametrize("smtp_host,smtp_port", [
    ("", 465), ("smtp.other.invalid", 587),
])
def test_local_matching_imap_secret_uses_only_gmail_ssl_before_wire(
        tmp_path, monkeypatch, smtp_host, smtp_port):
    record = _imap_secret()
    mail, settings, now, stores, calls, events, _ = _secret_smtp_setup(
        tmp_path, monkeypatch, record=record)
    settings.outreach_smtp_host = smtp_host
    settings.outreach_smtp_port = smtp_port
    settings.outreach_smtp_username = ""
    settings.outreach_smtp_password = ""
    original_settings = dict(vars(settings))
    original_record = dict(record)

    result = _send_secret_fake(mail, settings, now, stores)

    assert result["state"] == "confirmed_smtp_accepted"
    assert events == ["imap_read", "reserve", "begin_wire", "ssl"]
    assert stores.secrets.reads == ["imap"]
    assert [x[1] for x in calls if x[0] == "constructed"] == [
        ("smtp.gmail.com", 465)]
    assert [x[1:] for x in calls if x[0] == "login"] == [
        ("sender@example-co.com", "synthetic-only")]
    assert not any(x[0] == "starttls" for x in calls)
    assert len([x for x in calls if x[0] == "message"]) == 1
    assert vars(settings) == original_settings
    assert record == original_record
    message = next(x[1] for x in calls if x[0] == "message")
    assert message["From"] == "sender@example-co.com"
    assert message["Reply-To"] == "sender@example-co.com"
    assert "synthetic-only" not in message.as_string()


@pytest.mark.parametrize("operation,field,value", [
    ("replace", None, None),
    ("replace", None, []),
    ("replace", None, "not-json-object"),
    ("replace", None, {"host": "imap.gmail.com"}),
    ("remove", "host", None),
    ("remove", "mailbox", None),
    ("remove", "username", None),
    ("remove", "password", None),
    ("set", "host", ""),
    ("set", "mailbox", ""),
    ("set", "username", ""),
    ("set", "password", ""),
    ("set", "host", "imap.other.invalid"),
    ("set", "username", "other@example-co.com"),
    ("set", "host", {"not": "string"}),
    ("set", "mailbox", ["INBOX"]),
    ("set", "username", 123),
    ("set", "password", {"synthetic": "invalid"}),
    ("set", "password", 123),
])
def test_bad_imap_record_holds_without_reservation_or_smtp(
        tmp_path, monkeypatch, operation, field, value):
    from core.outreach_ledger import Ledger
    record = _imap_secret()
    if operation == "replace":
        record = value
    elif operation == "remove":
        del record[field]
    else:
        record[field] = value
    mail, settings, now, stores, calls, events, _ = _secret_smtp_setup(
        tmp_path, monkeypatch, record=record)
    settings.outreach_smtp_username = settings.outreach_smtp_password = ""
    settings.outreach_smtp_host = ""
    with pytest.raises(OutreachHold, match="matching_local_imap_app_password_required_for_smtp"):
        _send_secret_fake(mail, settings, now, stores)
    assert stores.secrets.reads == ["imap"]
    assert events[0] == "imap_read"
    assert not any(x in events for x in ("reserve", "begin_wire", "ssl", "plain"))
    assert ("release", "matching_local_imap_app_password_required_for_smtp") in events
    assert not calls
    with Ledger(settings)._tx() as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_unreadable_imap_secret_raises_sanitized_pre_reservation_hold(
        tmp_path, monkeypatch):
    from core.outreach_ledger import Ledger
    mail, settings, now, stores, calls, events, _ = _secret_smtp_setup(
        tmp_path, monkeypatch, error=RuntimeError("private-synthetic-error-marker"))
    settings.outreach_smtp_username = settings.outreach_smtp_password = ""
    with pytest.raises(OutreachHold) as caught:
        _send_secret_fake(mail, settings, now, stores)
    assert str(caught.value) == "matching_local_imap_app_password_required_for_smtp"
    assert "private-synthetic-error-marker" not in str(caught.value)
    assert stores.secrets.reads == ["imap"]
    assert ("release", str(caught.value)) in events
    assert not any(x in events for x in ("reserve", "begin_wire", "ssl", "plain"))
    assert not calls
    with Ledger(settings)._tx() as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


@pytest.mark.parametrize("smtp_port", [465, 587])
def test_explicit_smtp_credentials_preserve_tls_and_never_read_imap(
        tmp_path, monkeypatch, smtp_port):
    mail, settings, now, stores, calls, events, _ = _secret_smtp_setup(
        tmp_path, monkeypatch, error=AssertionError("imap must not be read"))
    settings.outreach_smtp_host = "smtp.example-co.com"
    settings.outreach_smtp_port = smtp_port
    settings.outreach_smtp_username = "explicit@example-co.com"
    settings.outreach_smtp_password = "synthetic-explicit"
    result = _send_secret_fake(mail, settings, now, stores)
    assert result["state"] == "confirmed_smtp_accepted"
    assert stores.secrets.reads == []
    assert events == ["reserve", "begin_wire", "ssl" if smtp_port == 465 else "plain"]
    assert [x[1] for x in calls if x[0] == "constructed"] == [
        ("smtp.example-co.com", smtp_port)]
    assert [x[1:] for x in calls if x[0] == "login"] == [
        ("explicit@example-co.com", "synthetic-explicit")]
    assert len([x for x in calls if x[0] == "starttls"]) == (smtp_port == 587)


@pytest.mark.parametrize("username,password,host,port", [
    ("", "explicit-only", "smtp.example-co.com", 465),
    ("explicit-only", "", "smtp.example-co.com", 465),
    ("explicit-only", "synthetic-explicit", "", 465),
    ("explicit-only", "synthetic-explicit", "smtp.example-co.com", 2525),
])
def test_partial_or_invalid_explicit_smtp_holds_without_secret_read(
        tmp_path, monkeypatch, username, password, host, port):
    mail, settings, now, stores, calls, events, _ = _secret_smtp_setup(
        tmp_path, monkeypatch, error=AssertionError("imap must not be read"))
    settings.outreach_smtp_username, settings.outreach_smtp_password = username, password
    settings.outreach_smtp_host, settings.outreach_smtp_port = host, port
    with pytest.raises(OutreachHold, match="private_smtp_tls_configuration_required"):
        _send_secret_fake(mail, settings, now, stores)
    assert stores.secrets.reads == []
    assert not any(x in events for x in ("reserve", "begin_wire", "ssl", "plain"))
    assert not calls


@pytest.mark.parametrize("flag", ["outreach_sender_authorized", "outreach_inbound_verified"])
def test_real_initial_sender_guard_blocks_secret_and_smtp(
        tmp_path, monkeypatch, flag):
    mail, settings, now, stores, calls, events, _ = _secret_smtp_setup(
        tmp_path, monkeypatch, record=_imap_secret())
    settings.outreach_smtp_username = settings.outreach_smtp_password = ""
    setattr(settings, flag, False)
    with pytest.raises(OutreachHold, match="operator_must_verify_sender_and_inbound_identity"):
        _send_secret_fake(mail, settings, now, stores)
    assert stores.secrets.reads == []
    assert not any(x in events for x in ("reserve", "begin_wire", "ssl", "plain"))
    assert not calls


@pytest.mark.parametrize("change,reason", [
    ("outreach_sender", "sender_identity_changed_before_wire"),
    ("outreach_sender_authorized", "operator_must_verify_sender_and_inbound_identity"),
    ("outreach_inbound_verified", "operator_must_verify_sender_and_inbound_identity"),
])
def test_real_final_sender_recheck_stops_mutation_before_smtp(
        tmp_path, monkeypatch, change, reason):
    from core.outreach_ledger import Ledger
    mail, settings, now, stores, calls, events, _ = _secret_smtp_setup(
        tmp_path, monkeypatch, record=_imap_secret())
    settings.outreach_smtp_username = settings.outreach_smtp_password = ""
    def change_after_reservation():
        setattr(settings, change, "other@example-co.com" if change == "outreach_sender"
                else False)
        return object()
    monkeypatch.setattr(mail.ssl, "create_default_context", change_after_reservation)
    with pytest.raises(OutreachHold, match=reason):
        _send_secret_fake(mail, settings, now, stores)
    assert events[:2] == ["imap_read", "reserve"]
    assert "begin_wire" not in events
    assert "ssl" not in events and "plain" not in events
    assert stores.secrets.reads == ["imap"]
    assert not calls
    with Ledger(settings)._tx() as db:
        assert db.execute("SELECT state FROM attempts").fetchone()[0] == (
            "definite_pre_wire_failure")


@pytest.mark.parametrize("mode,enabled", [("local", False), ("remote", True)])
def test_disabled_or_nonlocal_never_uses_imap_or_constructs_smtp(
        tmp_path, monkeypatch, mode, enabled):
    mail, settings, now, stores, calls, events, _ = _secret_smtp_setup(
        tmp_path, monkeypatch, record=_imap_secret())
    settings.outreach_smtp_username = settings.outreach_smtp_password = ""
    settings.mode = mode
    settings.outreach_enabled = enabled
    with pytest.raises(OutreachHold, match="outreach_disabled_or_not_local"):
        _send_secret_fake(mail, settings, now, stores)
    assert stores.secrets.reads == []
    assert not events and not calls


def test_post_handoff_setting_mutation_cannot_redirect_frozen_smtp_secret(
        tmp_path, monkeypatch):
    """No endpoint or credential selection is allowed after the durable handoff."""
    record = _imap_secret()
    mail, settings, now, stores, calls, events, hooks = _secret_smtp_setup(
        tmp_path, monkeypatch, record=record)
    settings.outreach_smtp_username = settings.outreach_smtp_password = ""
    settings.outreach_smtp_host = "smtp.other.invalid"
    settings.outreach_smtp_port = 587

    def mutate_after_begin():
        settings.outreach_smtp_host = "malicious.invalid"
        settings.outreach_smtp_port = 587
        settings.outreach_smtp_username = "changed"
        settings.outreach_smtp_password = "changed"
        record["username"] = "other@example-co.com"
        record["password"] = "changed-after-handoff"

    hooks["after_begin"] = mutate_after_begin
    result = _send_secret_fake(mail, settings, now, stores)
    assert result["state"] == "confirmed_smtp_accepted"
    assert events == ["imap_read", "reserve", "begin_wire", "ssl"]
    assert stores.secrets.reads == ["imap"]
    assert [x[1] for x in calls if x[0] == "constructed"] == [
        ("smtp.gmail.com", 465)]
    assert [x[1:] for x in calls if x[0] == "login"] == [
        ("sender@example-co.com", "synthetic-only")]
    assert not any(x[0] == "starttls" for x in calls)
