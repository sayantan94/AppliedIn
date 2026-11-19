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


def _mail_fake_setup(tmp_path, monkeypatch, *, collision_after_lookup=False):
    """A complete in-memory SMTP boundary; never starts a live connection."""
    from core import outreach_email as mail
    from core.outreach_contacts import LinkedInSnapshot
    from core.outreach_ledger import Ledger
    from datetime import timedelta
    when = datetime(2026, 11, 20, 8, tzinfo=UTC)
    lead = FundedNoOpening("a"*32, "Example Co", "example-co.com",
                           "https://example-co.com", (), "synthetic", when, "no_openings")
    contact = VerifiedRecipient("test@example-co.com", "name:test user", "Test User",
                                "person", "https://example-co.com/team")
    s = SimpleNamespace(mode="local", outreach_enabled=True, local_dir=str(tmp_path),
                        outreach_timezone="UTC", outreach_sender="sender@example-co.com",
                        outreach_smtp_host="smtp.example-co.com", outreach_smtp_port=587,
                        outreach_smtp_username="synthetic", outreach_smtp_password="synthetic")
    Ledger(s).record_successful_poll(
        uidvalidity="3", monitored_address=s.outreach_sender, now=when)
    calls = []
    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            calls.append(("constructed", args, kwargs))
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def ehlo(self): pass
        def starttls(self, **kwargs): pass
        def login(self, *_): pass
        def send_message(self, message, **kwargs):
            calls.append(("message", message, kwargs))
            return {}
    source_count = [0]
    def read_source(*args, **kwargs):
        source_count[0] += 1
        if collision_after_lookup and source_count[0] > 1:
            from dataclasses import replace
            return replace(lead, homepage="https://changed.example-co.com")
        return lead
    monkeypatch.setattr(mail, "read_no_opening", read_source)
    monkeypatch.setattr(mail, "read_linkedin",
                        lambda *args, **kwargs: LinkedInSnapshot(frozenset(), frozenset()))
    monkeypatch.setattr(mail, "_sender", lambda _: s.outreach_sender)
    monkeypatch.setattr(mail, "_official_page", lambda *args: (
        "https://example-co.com/team", "<a href='mailto:test@example-co.com'>Test User</a>"))
    monkeypatch.setattr(mail, "verify_published_contact", lambda **kwargs: contact)
    monkeypatch.setattr(mail, "company_application_clear", lambda *args: True)
    monkeypatch.setattr(mail, "approved_variant", lambda *args: {
        "id": "synthetic-v1", "sha256": "a"*64})
    monkeypatch.setattr(mail, "render_approved", lambda *args: ("Hello", "Synthetic approved email."))
    monkeypatch.setattr(mail, "original_seed_pdf", lambda _: b"%PDF-1.4 synthetic")
    return s, when, FakeSMTP, calls


def test_fake_smtp_uses_persisted_message_id_and_zero_pixel(tmp_path, monkeypatch):
    from core import outreach_email as mail
    s, now, factory, calls = _mail_fake_setup(tmp_path, monkeypatch)
    result = mail.send_approved(
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
    with pytest.raises(OutreachHold, match="funding_identity_changed"):
        mail.send_approved(
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
        return {"id": "synthetic-v1", "sha256": ("a" if approvals[0] == 1 else "b") * 64}
    monkeypatch.setattr(mail, "approved_variant", mutable_approval)
    with pytest.raises(OutreachHold, match="approved_variant_or_resume_changed_before_wire"):
        mail.send_approved(
            settings=s, company_id="a"*32,
            contact_page_url="https://example-co.com/team",
            exact_address="test@example-co.com", person_name="Test User",
            variant_id="synthetic-v1", stores=object(), now=now,
            smtp_factory=factory)
    assert approvals[0] == 2
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
    result = mail.send_approved(
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
    result = mail.send_approved(
        settings=s, company_id="a"*32,
        contact_page_url="https://example-co.com/team",
        exact_address="test@example-co.com", person_name="Test User",
        variant_id="synthetic-v1", stores=object(), now=now,
        smtp_factory=factory)
    assert result["tracking"] == "not_tracked"
    message = [x[1] for x in calls if x[0] == "message"][0]
    assert message.get_body(preferencelist=("html",)) is None
    assert "pixel" not in message.get_body(preferencelist=("plain",)).get_content().lower()
