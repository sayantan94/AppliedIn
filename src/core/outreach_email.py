"""Approved-template SMTP lane with optional token-free fallback. No SMTP on import.

All irreversible network work follows the persisted reservation and pre-wire
stop/freshness checks. An exception after begin_wire is uncertain, never an
automatic retry. SMTP acceptance is not delivery or a human reply.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import smtplib
import ssl
import string
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

from core.outreach_contacts import (ContactHold, VerifiedRecipient, read_linkedin,
                                    normalize_email, verify_published_contact)
from core.outreach_ledger import Ledger, OutreachHold
from core.outreach_sources import read_no_opening
from core.outreach_tracking import register_pixel, pixel_base
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
                if name not in {"first_name", "company_name", "funding_fact", "role_family"} or spec or conversion:
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


def render_approved(item, lead, contact, *, funding_fact=""):
    def plain(value, limit):
        if not isinstance(value, str) or not value or len(value) > limit:
            raise OutreachHold("source_verified_slot_missing")
        if any(ord(c) < 32 or c in "{}<>" for c in value):
            raise OutreachHold("unsafe_verified_slot")
        return value
    if (not funding_fact and "{funding_fact}" in
            (item["subject"] + item["body"])):
        raise OutreachHold("verified_funding_fact_or_approved_generic_variant_required")
    fields = {"first_name":plain(contact.name.split()[0], 70) if contact.name else "",
              "company_name":plain(lead.name, 150),
              "funding_fact":plain(funding_fact, 180) if funding_fact else "",
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
    return sender


def _effective_smtp(settings, sender, stores):
    """Resolve once before reservation; no credentials or endpoint re-read after wire."""
    username = settings.outreach_smtp_username
    password = settings.outreach_smtp_password
    if username and password:
        host, port = settings.outreach_smtp_host, settings.outreach_smtp_port
        if (not isinstance(host, str)
                or not re.fullmatch(r"[A-Za-z0-9.-]{3,253}", host)
                or port not in (465, 587)):
            raise OutreachHold("private_smtp_tls_configuration_required")
        return host, port, username, password
    if username != "" or password != "" or settings.mode != "local":
        raise OutreachHold("private_smtp_tls_configuration_required")

    # Only the injected private store may supply a fully empty explicit pair.
    # Validate types beyond reply_ingest.local_credentials's truthiness check.
    try:
        record = stores.secrets.get_json("imap")
        valid = (isinstance(record, dict)
                 and all(isinstance(record.get(key), str) and record[key]
                         for key in ("host", "mailbox", "username", "password"))
                 and record["host"] == "imap.gmail.com"
                 and record["username"] == sender)
    except Exception:
        raise OutreachHold("matching_local_imap_app_password_required_for_smtp") from None
    if not valid:
        raise OutreachHold("matching_local_imap_app_password_required_for_smtp")
    return "smtp.gmail.com", 465, record["username"], record["password"]


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


def verified_funding_fact(settings, lead, now):
    """Optional exact public corroboration of an owner-approved private quote.

    #386 intentionally does not persist a quote or source article in its
    no-opening record. Never invent a funding statement from the status label.
    An operator may supply an immutable private manifest; verify its exact
    sentence against a bounded guarded HTTPS page, otherwise hold a fact-using
    variant and allow a *separately approved* no-fact variant.
    """
    relative = getattr(settings, "outreach_funding_fact_manifest", "")
    if not relative:
        return ""
    path = _private_file(settings, relative, max_bytes=8192)
    try:
        proof = json.loads(path.read_text("utf-8"))
        if not isinstance(proof, dict) or set(proof) != {
                "company_id", "source_url", "quote", "sha256"}:
            raise ValueError
        source, quote = proof["source_url"], proof["quote"]
        if (proof["company_id"] != lead.company_id
                or not isinstance(source, str) or not source.startswith("https://")
                or not isinstance(quote, str) or not 12 <= len(quote) <= 180
                or any(ord(char) < 32 or char in "{}<>" for char in quote)):
            raise ValueError
        signature = json.dumps(
            {"company_id": lead.company_id, "source_url": source, "quote": quote},
            sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        if hashlib.sha256(signature.encode()).hexdigest() != proof["sha256"]:
            raise ValueError
        # Source text must explicitly tie the asserted seed/round/financing
        # statement to the very same verified employer (or a verified alias).
        if not re.search(r"\b(?:seed|series a|financing|funding|raised)\b",
                         quote, re.I) or not any(
                re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", quote, re.I)
                for name in (lead.name, *lead.aliases)):
            raise ValueError
        final_url, body = _official_page(settings, source, now)
        if final_url != source or not isinstance(body, str):
            raise ValueError
        visible = " ".join(html.unescape(re.sub(r"<[^>]*>", " ", body)).split())
        if quote not in visible:
            raise ValueError
        return quote
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise OutreachHold("verified_private_funding_fact_required") from exc


def _stage_variant_id(settings, stage, contact):
    """Exact owner-configured named/team choice; never use legacy shared ID."""
    if not isinstance(stage,int) or not 1<=stage<=5:
        raise OutreachHold("followup_stage_invalid")
    try:
        mapping = json.loads(getattr(settings,"outreach_followup_stage_variants","") or "{}")
        if not isinstance(mapping,dict) or any(
                key not in {str(i) for i in range(1,6)} or
                not isinstance(v,dict) or set(v)!={"named","team"} or
                not all(isinstance(x,str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}",x)
                        for x in v.values())
                for key,v in mapping.items()):
            raise ValueError
        ids = [v[k] for v in mapping.values() for k in ("named","team")]
        if len(ids)!=len(set(ids)):
            raise ValueError
    except (ValueError,TypeError,AttributeError) as exc:
        raise OutreachHold("followup_stage_mapping_invalid") from exc
    is_org = contact.kind == "organization" and contact.person_key == "org" and not contact.name
    if not is_org and (contact.kind!="person" or not contact.name):
        raise OutreachHold("verified_named_or_organizational_contact_required")
    choice = mapping.get(str(stage),{}).get("team" if is_org else "named")
    if not choice:
        raise OutreachHold("followup_stage_approved_choice_missing")
    return choice


def enqueue_first(*, settings, company_id, contact_page_url, exact_address,
                  person_name, variant_id, now=None):
    """Protected local producer: proves contact/approval then only enqueues."""
    now = now or datetime.now(UTC)
    lead = read_no_opening(settings,company_id,now=now)
    url,markup = _official_page(settings,contact_page_url,now)
    contact = verify_published_contact(company_domain=lead.domain,source_url=url,
                                       html=markup,address=exact_address,name=person_name)
    variant = approved_variant(settings,variant_id,"first")
    # Reject no-name placeholders and unapproved dynamic slots at enqueue.
    fact = (verified_funding_fact(settings,lead,now)
            if "{funding_fact}" in (variant["subject"]+variant["body"]) else "")
    if not contact.name and "{first_name}" in (variant["subject"]+variant["body"]):
        raise OutreachHold("verified_name_or_approved_team_variant_required")
    render_approved(variant,lead,contact,funding_fact=fact)
    return Ledger(settings).enqueue_email(lead,contact,stage=0,
        variant_id=variant["id"],digest=variant["sha256"],now=now)


def enqueue_due_followup(*, settings, candidate, now=None):
    """Candidate originates from the definitively accepted prior stage only."""
    now = now or datetime.now(UTC)
    person = candidate["person_key"]
    org = person == "org"
    contact = VerifiedRecipient(
        candidate["recipient"],person,candidate["person_name"],
        "organization" if org else "person",candidate["contact_url"])
    reason = ""
    choice = digest = None
    try:
        choice = _stage_variant_id(settings,candidate["stage"],contact)
        variant = approved_variant(settings,choice,"followup")
        if not contact.name and "{first_name}" in (variant["subject"]+variant["body"]):
            raise OutreachHold("verified_name_or_approved_team_variant_required")
        digest = variant["sha256"]
    except OutreachHold as exc:
        choice = digest = None
        reason = str(exc)
    return Ledger(settings).enqueue_due_from_accepted(
        candidate,variant_id=choice,digest=digest,reason=reason,now=now)


def correct_queue_choice(*, settings, queue_id, expected_version,
                         expected_state, variant_id, owner_no_wire_proved=False):
    """Existing guarded POST mode; no send, new intent or automatic substitution."""
    ledger = Ledger(settings)
    q = ledger.queue_intent(queue_id)
    contact = VerifiedRecipient(q["recipient"],q["person_key"],q["contact_name"],
        "organization" if q["person_key"]=="org" else "person",q["contact_url"])
    if q["stage"]>0 and _stage_variant_id(settings,q["stage"],contact)!=variant_id:
        raise OutreachHold("stage_specific_owner_choice_required")
    item = approved_variant(settings,variant_id,
                            "first" if q["stage"]==0 else "followup")
    if not contact.name and "{first_name}" in (item["subject"]+item["body"]):
        raise OutreachHold("verified_name_or_approved_team_variant_required")
    return ledger.repair_queue(queue_id,expected_version=expected_version,
        expected_state=expected_state,variant_id=item["id"],digest=item["sha256"],
        owner_no_wire_proved=owner_no_wire_proved)


def _current_time(now, clock):
    instant = clock() if clock is not None else (now or datetime.now(UTC))
    if not isinstance(instant,datetime) or instant.tzinfo is None:
        raise OutreachHold("timezone_required")
    return instant.astimezone(UTC)


def _safe_recheck_reason(exc):
    if isinstance(exc,OutreachHold):
        return str(exc)
    if isinstance(exc,ContactHold):
        return "verified_contact_recheck_required"
    return "source_or_preflight_recheck_required"


def _terminal_stop(exc):
    return isinstance(exc,OutreachHold) and str(exc) in {
        "recorded_company_or_recipient_stop","existing_company_application",
        "cross_channel_person_already_reserved_or_contacted",
        "linkedin_already_contacted_person"}


def send_approved(*, settings, queue_id, owner_token, stores,
                  now=None, clock=None, smtp_factory=None, gap_rng=None):
    """Worker-only SMTP: durable claim, linked reserve, global final handoff.

    Direct unqueued sends no longer exist. Real SMTP occurs only after the
    shared SQLite fence commits; no SQLite transaction spans transport.
    """
    ledger = Ledger(settings)
    q = ledger.queue_intent(queue_id)
    if (q["state"]!="claimed" or not owner_token
            or q["owner_token"]!=owner_token or q["attempt_id"] is not None):
        raise OutreachHold("queue_sender_claim_required")
    ticket = None
    handed_off = False
    stage = q["stage"]
    kind = "first" if stage==0 else "followup"
    try:
        current = _current_time(now,clock)
        ledger._window(current)
        ledger._stage_enabled(stage)
        sender = _sender(settings)
        effective_smtp = _effective_smtp(settings, sender, stores)
        lead = read_no_opening(settings,q["company_id"],now=current)
        url,markup = _official_page(settings,q["contact_url"],current)
        contact = verify_published_contact(
            company_domain=lead.domain,source_url=url,html=markup,
            address=q["recipient"],name=q["contact_name"])
        if (contact.person_key!=q["person_key"] or contact.address!=q["recipient"]
                or contact.evidence_url!=q["contact_url"]):
            raise OutreachHold("queue_verified_contact_changed")
        if stage>0 and _stage_variant_id(settings,stage,contact)!=q["selected_variant"]:
            raise OutreachHold("stage_variant_mapping_changed_requires_owner_choice")
        variant = approved_variant(settings,q["selected_variant"],kind)
        if variant["sha256"]!=q["selected_digest"]:
            raise OutreachHold("frozen_stage_approval_changed_requires_owner_choice")
        fact = (verified_funding_fact(settings,lead,current)
                if "{funding_fact}" in (variant["subject"]+variant["body"]) else "")
        if not contact.name and "{first_name}" in (variant["subject"]+variant["body"]):
            raise OutreachHold("verified_name_or_approved_team_variant_required")
        logical_subject,body = render_approved(variant,lead,contact,funding_fact=fact)
        pdf = original_seed_pdf(settings)
        linkedin = read_linkedin(settings,company_id=lead.company_id,
                                 company_name=lead.name,company_domain=lead.domain)
        if not company_application_clear(stores,lead):
            raise OutreachHold("existing_company_application")
        # Complete all slow source/contact/approval/seed reads before the
        # reservation and final atomic SMTP handoff.
        prewire = _current_time(now,clock)
        current_lead = read_no_opening(settings,q["company_id"],now=prewire)
        fresh_url,fresh_html = _official_page(settings,q["contact_url"],prewire)
        fresh_contact = verify_published_contact(
            company_domain=current_lead.domain,source_url=fresh_url,html=fresh_html,
            address=q["recipient"],name=q["contact_name"])
        fresh_linkedin = read_linkedin(settings,company_id=lead.company_id,
            company_name=lead.name,company_domain=lead.domain)
        if current_lead!=lead or fresh_contact!=contact:
            raise OutreachHold("source_or_contact_changed_before_wire")
        if (contact.person_key in fresh_linkedin.people or
                ledger.report(lead.company_id,fresh_linkedin)["used"]>3):
            raise OutreachHold("linkedin_duplicate_or_capacity_changed_before_wire")
        if not company_application_clear(stores,lead):
            raise OutreachHold("existing_company_application")
        if (approved_variant(settings,q["selected_variant"],kind)["sha256"]!=variant["sha256"]
                or original_seed_pdf(settings)!=pdf):
            raise OutreachHold("approved_variant_or_resume_changed_before_wire")
        if stage>0 and _stage_variant_id(settings,stage,contact)!=q["selected_variant"]:
            raise OutreachHold("stage_variant_mapping_changed_requires_owner_choice")
        if fact and verified_funding_fact(settings,lead,prewire)!=fact:
            raise OutreachHold("verified_funding_fact_changed_before_wire")
        ticket = ledger.reserve_email(lead,contact,fresh_linkedin,no_application=True,
            resume_seed_proved=True,sender_proved=True,variant=variant["id"],
            digest=variant["sha256"],stage=stage,now=prewire,
            expanded_followups=getattr(settings,"outreach_followups_expanded",False),
            queue_id=queue_id,owner_token=owner_token,
            root_subject=logical_subject if stage==0 else None)
        # The root is persisted on the original stage-0 attempt, not inferred
        # from serialized or folded MIME headers or the follow-up template.
        subject = logical_subject if stage==0 else "Re: "+ticket["root_subject"]
        image_url,tracking_mode = "","not_tracked"
        try:
            image_url,tracking_mode = register_pixel(settings,ledger,ticket["id"])
        except (OSError,ValueError):
            image_url,tracking_mode = "","not_tracked"
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
        if image_url and pixel_base(settings)==image_url.split("/o/",1)[0]:
            msg.add_alternative(
                '<div style="white-space:pre-wrap">'+html.escape(body)+'</div>'+
                '<img src="'+image_url+'" width="1" height="1" alt="" />',
                subtype="html")
        else:
            tracking_mode="not_tracked"
            ledger.disable_pixel(ticket["id"])
        msg.add_attachment(pdf,maintype="application",subtype="pdf",filename="resume.pdf")
        tls_context = ssl.create_default_context()
        # The message was rendered locally, but mutable owner approvals and
        # application state can change while the message is assembled. Recheck
        # immediately before the ledger's final atomic handoff, never afterwards.
        if approved_variant(settings,q["selected_variant"],kind)["sha256"] != q["selected_digest"]:
            raise OutreachHold("frozen_stage_approval_changed_before_wire")
        if stage>0 and _stage_variant_id(settings,stage,contact)!=q["selected_variant"]:
            raise OutreachHold("stage_variant_mapping_changed_requires_owner_choice")
        if _sender(settings)!=sender:
            raise OutreachHold("sender_identity_changed_before_wire")
        if not company_application_clear(stores,lead):
            raise OutreachHold("existing_company_application")
        # The final transaction rechecks the current clock/window, stops,
        # inbound poll and follow-up flags, and persists the global fence.
        handoff = _current_time(now,clock)
        ledger.begin_wire(ticket["id"],source_checked=True,no_application=True,
                          linkedin_checked=True,now=handoff,gap_rng=gap_rng)
        handed_off = True
    except Exception as exc:
        if ticket is None:
            ledger.release_claim(queue_id,owner_token,_safe_recheck_reason(exc),
                                 cancel=_terminal_stop(exc),now=_current_time(now,clock))
        elif not handed_off:
            # begin_wire never committed: positively proven no SMTP transport.
            ledger.definite_no_wire(ticket["id"],owner_token=owner_token)
        raise
    # No source/network/PDF/template I/O after the durable final handoff.
    try:
        ledger._stage_enabled(stage)
        ledger._window(_current_time(now,clock))
        if settings.mode!="local" or not settings.outreach_enabled:
            raise OutreachHold("outreach_disabled_or_not_local")
    except Exception:
        # Nothing has touched SMTP yet, including connection construction.
        ledger.abort_handoff_before_transport(ticket["id"],owner_token,
                                               now=_current_time(now,clock))
        raise
    try:
        host, port, username, password = effective_smtp
        factory = smtp_factory or (smtplib.SMTP_SSL if port==465 else smtplib.SMTP)
        if port==465:
            connection_context=factory(host,port,timeout=20,context=tls_context)
        else:
            connection_context=factory(host,port,timeout=20)
        with connection_context as connection:
            if port==587:
                connection.ehlo()
                connection.starttls(context=tls_context)
                connection.ehlo()
            connection.login(username,password)
            refused=connection.send_message(msg,from_addr=sender,to_addrs=[contact.address])
            if refused:
                raise OutreachHold("smtp_recipient_not_definitively_accepted")
    except Exception as exc:
        ledger.finish_wire(ticket["id"],accepted=False,now=_current_time(now,clock))
        raise OutreachHold("smtp_wire_outcome_uncertain_reconcile_manually") from exc
    ledger.finish_wire(ticket["id"],accepted=True,now=_current_time(now,clock))
    return {"attempt_id":ticket["id"],"queue_id":queue_id,
            "state":"confirmed_smtp_accepted","tracking":tracking_mode,
            "variant":variant["id"]}
