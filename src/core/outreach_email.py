"""Approved-template, no-pixel SMTP lane. No SMTP during imports or tests.

All irreversible network work follows the persisted reservation and pre-wire
stop/freshness checks. An exception after begin_wire is uncertain, never an
automatic retry. SMTP acceptance is not delivery or a human reply.
"""
from __future__ import annotations

import hashlib
import json
import re
import smtplib
import ssl
import string
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

from core.outreach_contacts import (ContactHold, read_linkedin,
                                    normalize_email, verify_published_contact)
from core.outreach_ledger import Ledger, OutreachHold
from core.outreach_sources import read_no_opening
from tools.outreach_guard import company_application_clear


def _private_file(settings, relative, *, max_bytes):
    root = Path(settings.local_dir).resolve()
    proposed = root / relative
    path = proposed.resolve()
    if (not isinstance(relative, str) or not relative or proposed.is_symlink()
            or path == root or not path.is_relative_to(root) or not path.is_file()
            or path.stat().st_size > max_bytes or path.stat().st_mode & 0o077):
        raise OutreachHold("private_approved_artifact_missing_or_unsafe")
    return path


def approved_variant(settings, variant_id, kind):
    """The operator approves the whole immutable variant digest in private JSON.

    The writer may propose copy, but editing the approved text invalidates
    its stored digest. No model free prose is accepted by this renderer.
    """
    path = _private_file(settings, settings.outreach_approvals_file, max_bytes=100000)
    try:
        doc = json.loads(path.read_text("utf-8"))
        entries = doc["variants"]
        if not isinstance(entries, list) or len(entries) > 100:
            raise ValueError
        candidates = [v for v in entries if v.get("id") == variant_id and v.get("kind") == kind]
        if len(candidates) != 1:
            raise ValueError
        item = candidates[0]
        if item.get("approved") is not True or set(item) != {
                "id", "kind", "subject", "body", "role_family", "sha256", "approved"}:
            raise ValueError
        if not isinstance(item["subject"], str) or not 1 <= len(item["subject"]) <= 160:
            raise ValueError
        if not isinstance(item["body"], str) or not 1 <= len(item["body"]) <= 5000:
            raise ValueError
        if not isinstance(item["role_family"], str) or not 2 <= len(item["role_family"]) <= 70:
            raise ValueError
        if not isinstance(item["id"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", item["id"]):
            raise ValueError
        digest = hashlib.sha256(json.dumps(
            {k:item[k] for k in ("id","kind","subject","body","role_family")},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        if digest != item["sha256"]:
            raise ValueError
        for part in (item["subject"], item["body"], item["role_family"]):
            if any(ord(c) < 32 and c not in "\n\t" for c in part):
                raise ValueError
        return item
    except (ValueError, KeyError, TypeError, UnicodeError, OSError) as exc:
        raise OutreachHold("approved_immutable_template_required") from exc


def _render(source, fields, *, subject=False):
    names = set()
    fmt = string.Formatter()
    try:
        for literal, name, spec, conversion in fmt.parse(source):
            if name is not None:
                if name not in {"first_name", "company_name", "role_family"} or spec or conversion:
                    raise ValueError
                names.add(name)
        result = source.format_map(fields)
    except (KeyError, ValueError, AttributeError) as exc:
        raise OutreachHold("unapproved_or_unsupported_template_slot") from exc
    if subject and ("\r" in result or "\n" in result):
        raise OutreachHold("unsafe_subject_header")
    if len(result) > (180 if subject else 6500):
        raise OutreachHold("rendered_template_too_long")
    return result


def render_approved(item, lead, contact):
    def plain(value, limit):
        if not isinstance(value, str) or not value or len(value) > limit:
            raise OutreachHold("source_verified_slot_missing")
        if any(ord(c) < 32 or c in "{}<>" for c in value):
            raise OutreachHold("unsafe_verified_slot")
        return value
    fields = {"first_name":plain(contact.name.split()[0], 70) if contact.name else "",
              "company_name":plain(lead.name, 150),
              "role_family":plain(item["role_family"], 70)}
    return (_render(item["subject"], fields, subject=True),
            _render(item["body"], fields))


def original_seed_pdf(settings):
    """A private manifest binds PDF digest AND unchanged live base seed."""
    path = _private_file(settings, settings.outreach_resume_manifest, max_bytes=4096)
    try:
        manifest = json.loads(path.read_text("utf-8"))
        if set(manifest) != {"pdf_path", "sha256", "resume_seed"}:
            raise ValueError
        pdf_path = _private_file(settings, manifest["pdf_path"], max_bytes=10_000_000)
        if pdf_path.suffix.lower() != ".pdf":
            raise ValueError
        from agent.run import seed_fingerprint, _base_latex
        # The fingerprint of an absent base.tex is still a nonempty SHA. Never
        # accept that empty-content hash as a verified original resume seed.
        base = _base_latex()
        current = seed_fingerprint()
        if not base.strip() or not current or manifest["resume_seed"] != current:
            raise ValueError
        data = pdf_path.read_bytes()
        if (not data.startswith(b"%PDF-") or not re.fullmatch(r"[a-f0-9]{64}", manifest["sha256"])
                or hashlib.sha256(data).hexdigest() != manifest["sha256"]):
            raise ValueError
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise OutreachHold("current_original_seed_pdf_required") from exc


def _sender(settings):
    sender = normalize_email(getattr(settings, "outreach_sender", ""))
    if (not getattr(settings, "outreach_sender_authorized", False)
            or not getattr(settings, "outreach_inbound_verified", False)):
        raise OutreachHold("operator_must_verify_sender_and_inbound_identity")
    if (not isinstance(settings.outreach_smtp_host, str) or
            not re.fullmatch(r"[A-Za-z0-9.-]{3,253}", settings.outreach_smtp_host)
            or settings.outreach_smtp_port not in (465, 587)
            or not settings.outreach_smtp_username or not settings.outreach_smtp_password):
        raise OutreachHold("private_smtp_tls_configuration_required")
    return sender


def _official_page(settings, url, now):
    """Only #386's HTTPS/public-IP/redirect/byte-guarded fetch transport."""
    from discovery.funding import GuardedClient, State
    state = State(Path(settings.local_dir) / "outreach-http-budget.yaml")
    try:
        with GuardedClient(state=state, settings=settings, now=now) as client:
            response = client.get(url, follow_redirects=True)
            response.raise_for_status()
            return str(response.request.url), response.text
    except Exception as exc:
        raise OutreachHold("official_https_contact_page_unavailable") from exc


def send_approved(*, settings, company_id, contact_page_url, exact_address,
                  person_name, variant_id, stores, stage=0, now=None,
                  smtp_factory=None, expand_followups=False):
    """Synchronous guarded first/follow-up send; never invoked by default.

    The caller cannot supply pre-approved arbitrary body, résumé PDF, or a
    pre-accepted contact object; each is revalidated here. No tracking pixel.
    """
    now = now or datetime.now(UTC)
    if settings.mode != "local" or not settings.outreach_enabled:
        raise OutreachHold("outreach_disabled_or_not_local")
    sender = _sender(settings)
    lead = read_no_opening(settings, company_id, now=now)
    url, html = _official_page(settings, contact_page_url, now)
    contact = verify_published_contact(company_domain=lead.domain, source_url=url,
                                        html=html, address=exact_address, name=person_name)
    linkedin = read_linkedin(settings, company_id=lead.company_id,
                             company_name=lead.name, company_domain=lead.domain)
    if not company_application_clear(stores, lead):
        raise OutreachHold("existing_company_application")
    kind = "first" if stage == 0 else "followup"
    variant = approved_variant(settings, variant_id, kind)
    subject, body = render_approved(variant, lead, contact)
    pdf = original_seed_pdf(settings)
    ledger = Ledger(settings)
    ticket = ledger.reserve_email(
        lead, contact, linkedin, no_application=True, resume_seed_proved=True,
        sender_proved=True, variant=variant["id"], digest=variant["sha256"],
        stage=stage, now=now, expanded_followups=expand_followups)
    msg = EmailMessage()
    msg["From"] = sender
    msg["Reply-To"] = sender
    msg["To"] = contact.address
    msg["Subject"] = subject
    msg["Message-ID"] = ticket["message_id"]
    if ticket["in_reply_to"]:
        msg["In-Reply-To"] = ticket["in_reply_to"]
        msg["References"] = " ".join(ticket["references"])
    msg.set_content(body)
    msg.add_attachment(pdf, maintype="application", subtype="pdf",
                       filename="resume.pdf")
    try:
        # Always acquire fresh source and independent read-only LinkedIn state
        # immediately before attempting the external commit.
        # A slow contact-page fetch must not smuggle an old poll/source
        # timestamp past the immediate pre-SMTP boundary. Injected fake SMTP
        # keeps the explicitly supplied frozen clock for offline tests only.
        prewire_now = datetime.now(UTC) if smtp_factory is None else now
        current = read_no_opening(settings, company_id, now=prewire_now)
        if current != lead:
            raise OutreachHold("funding_identity_changed_before_wire")
        latest_linkedin = read_linkedin(
            settings, company_id=lead.company_id,
            company_name=lead.name, company_domain=lead.domain)
        if (contact.person_key in latest_linkedin.people
                or ledger.report(lead.company_id, latest_linkedin)["used"] > 3):
            raise OutreachHold("linkedin_duplicate_or_capacity_changed_before_wire")
        if not company_application_clear(stores, lead):
            raise OutreachHold("existing_company_application")
        # Approval and resume are mutable private files, even though a
        # previously approved version/digest is immutable. Do not send a stale
        # rendered message if either approval or original seed changed during
        # the guarded contact lookup.
        if (approved_variant(settings, variant_id, kind)["sha256"] != variant["sha256"]
                or original_seed_pdf(settings) != pdf):
            raise OutreachHold("approved_variant_or_resume_changed_before_wire")
        ledger.begin_wire(ticket["id"], source_checked=True, no_application=True,
                          linkedin_checked=True, now=prewire_now)
    except Exception:
        # Only reserved/pre-wire state is provably no-wire here.
        ledger.definite_no_wire(ticket["id"])
        raise
    try:
        factory = smtp_factory
        if factory is None:
            factory = smtplib.SMTP_SSL if settings.outreach_smtp_port == 465 else smtplib.SMTP
        if settings.outreach_smtp_port == 465:
            connection_context = factory(settings.outreach_smtp_host,
                                         settings.outreach_smtp_port, timeout=20,
                                         context=ssl.create_default_context())
        else:
            connection_context = factory(settings.outreach_smtp_host,
                                         settings.outreach_smtp_port, timeout=20)
        with connection_context as connection:
            if settings.outreach_smtp_port == 587:
                connection.ehlo()
                connection.starttls(context=ssl.create_default_context())
                connection.ehlo()
            connection.login(settings.outreach_smtp_username,
                             settings.outreach_smtp_password)
            refused = connection.send_message(msg, from_addr=sender,
                                              to_addrs=[contact.address])
            if refused:
                # A partial/misleading acknowledgement is not proven no-wire.
                raise OutreachHold("smtp_recipient_not_definitively_accepted")
    except Exception as exc:
        ledger.finish_wire(ticket["id"], accepted=False)
        raise OutreachHold("smtp_wire_outcome_uncertain_reconcile_manually") from exc
    ledger.finish_wire(ticket["id"], accepted=True)
    return {"attempt_id":ticket["id"], "state":"confirmed_smtp_accepted",
            "tracking":"not_tracked", "variant":variant["id"]}
