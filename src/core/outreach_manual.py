"""Private, owner-copy-only social targets. This module never sends messages."""
from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urlsplit

from core.outreach_contacts import (ContactHold, LinkedInSnapshot, VerifiedRecipient,
                                    person_key, read_linkedin)
from core.outreach_email import _official_page, approved_variant, render_approved
from core.outreach_ledger import Ledger, OutreachHold
from core.outreach_sources import SourceHold, read_no_opening
from tools.outreach_guard import company_application_clear


class _PublicLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = set()

    def handle_starttag(self, tag, attrs):
        if tag.casefold() == "a":
            self.links.update(v for k, v in attrs if k.casefold() == "href"
                              and isinstance(v, str) and len(v) <= 2048)


def _social_link(channel: str, target_url: str) -> bool:
    if channel not in {"linkedin", "discord", "telegram", "x"}:
        return False
    if not isinstance(target_url, str) or len(target_url) > 2048:
        return False
    try:
        u = urlsplit(target_url)
        if (u.scheme != "https" or not u.hostname or u.username or u.password
                or u.port not in (None, 443) or u.query or u.fragment
                or "%" in u.path or "\\" in u.path):
            return False
        path = u.path.strip("/")
        if channel == "linkedin":
            return (u.hostname in {"linkedin.com", "www.linkedin.com"}
                    and bool(re.fullmatch(r"(?:in|company)/[A-Za-z0-9_.-]{2,120}", path)))
        if channel == "discord":
            return (u.hostname in {"discord.gg", "discord.com"}
                    and bool(re.fullmatch(
                        r"(?:invite/)?[A-Za-z0-9_-]{3,100}" if u.hostname == "discord.gg"
                        else r"invite/[A-Za-z0-9_-]{3,100}", path)))
        if channel == "telegram":
            return (u.hostname in {"t.me", "telegram.me"}
                    and bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,60}", path))
                    and not path.casefold().endswith("bot"))
        return (u.hostname in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
                and bool(re.fullmatch(r"[A-Za-z0-9_]{1,15}(?:/status/[0-9]{5,25})?",
                                      path)))
    except (TypeError, ValueError):
        return False


def _lead(settings, company_id, now):
    # Source age is a warning for manual, not an automatic email gate.
    return read_no_opening(settings, company_id, now=now, require_fresh=False)


def _read_combined(settings, lead) -> LinkedInSnapshot:
    try:
        return read_linkedin(settings, company_id=lead.company_id,
                             company_name=lead.name, company_domain=lead.domain,
                             strict=False)
    except ContactHold as exc:
        # Unknown tracker capacity cannot be represented as an empty snapshot.
        raise OutreachHold("manual_linkedin_count_unavailable_reconcile_privately") from exc


def queue_target(*, settings, company_id: str, channel: str, target_url: str,
                 evidence_url: str, person_name: str, variant_id: str, now=None):
    """Only employer-published exact social links become copy-ready private rows."""
    now = now or datetime.now(UTC)
    lead = _lead(settings, company_id, now)
    if not _social_link(channel, target_url):
        raise OutreachHold("manual_social_url_unverified")
    url, html = _official_page(settings, evidence_url, now)
    if not isinstance(html, str) or len(html.encode("utf-8")) > 256000:
        raise OutreachHold("manual_evidence_page_unbounded")
    links = _PublicLinks()
    links.feed(html)
    if target_url not in links.links:
        raise OutreachHold("manual_link_not_published_by_employer")
    # An unverified profile-to-name join must *cost* a provisional slot.
    # Its stable key dedups repeat copies of the same public handle.
    identity = "provisional:manual:" + hashlib.sha256(
        (company_id + "|" + channel + "|" + target_url).encode()).hexdigest()[:32]
    verified_name = ""
    if isinstance(person_name, str) and person_name:
        try:
            key = person_key(person_name)
            if key[5:] in " ".join(re.sub(r"<[^>]+>", " ", html).casefold().split()):
                identity, verified_name = key, person_name.strip()
        except ContactHold:
            pass
    template = approved_variant(settings, variant_id, "manual")
    recipient = VerifiedRecipient("", identity, verified_name, "person", url)
    _, message = render_approved(template, lead, recipient)
    return Ledger(settings).manual_enqueue(
        company_id=company_id, channel=channel, person_key=identity,
        person_name=verified_name, target_url=target_url,
        evidence_url=url, message=message, variant_id=variant_id, now=now)


def manual_report(*, settings, company_id, now=None):
    now = now or datetime.now(UTC)
    lead = _lead(settings, company_id, now)
    linkedin = _read_combined(settings, lead)
    ledger = Ledger(settings)
    result = ledger.report(company_id, linkedin, now=now)
    age = now.astimezone(UTC) - lead.verified_at
    return {**result, "company_id":company_id, "company_name":lead.name,
            "source_age_hours":round(age.total_seconds() / 3600, 1),
            "source_stale_for_email":age > timedelta(days=7),
            "source_reason":lead.reason, "linkedin_warnings":sorted(linkedin.warnings),
            "manual_targets":ledger.manual_rows(company_id)}


def act_on_target(*, settings, company_id, target_id, version, action, stores, now=None):
    """At BOTH copy and mark, re-read stops, company applications and LinkedIn."""
    now = now or datetime.now(UTC)
    lead = _lead(settings, company_id, now)
    if not company_application_clear(stores, lead):
        raise OutreachHold("existing_company_application")
    linkedin = _read_combined(settings, lead)
    ledger = Ledger(settings)
    rows = [r for r in ledger.manual_rows(company_id) if r["id"] == target_id]
    if len(rows) != 1:
        raise OutreachHold("manual_target_not_found")
    row = rows[0]
    template = approved_variant(settings, row["variant_id"], "manual")
    recipient = VerifiedRecipient("", row["person_key"], row["person_name"],
                                  "person", row["evidence_url"])
    _, message = render_approved(template, lead, recipient)
    if message != row["message"]:
        raise OutreachHold("manual_approved_message_changed")
    return ledger.manual_action(target_id, version, linkedin, no_application=True,
                                action=action, now=now)


def owner_response(*, settings, company_id, target_id, trusted: bool, opt_out=False,
                   stores=None, now=None):
    """Owner-marked *verified* reply stops company; ambiguous reply holds email."""
    now = now or datetime.now(UTC)
    lead = _lead(settings, company_id, now)
    ledger = Ledger(settings)
    rows = [r for r in ledger.manual_rows(company_id) if r["id"] == target_id]
    if len(rows) != 1 or rows[0]["state"] != "owner_marked_sent":
        raise OutreachHold("manual_response_requires_marked_contact")
    if trusted is True:
        ledger.record_stop(company_id, kind="opt_out" if opt_out else "human_reply",
                           now=now)
    else:
        ledger.hold_company_email(company_id, "ambiguous_owner_social_reply", now=now)
    return {"held":not trusted, "company_stopped":bool(trusted)}


def owner_linkedin_reserve(*, settings, company_id, person_name, stores, now=None):
    """Reserve combined capacity BEFORE owner operates their separate channel."""
    now = now or datetime.now(UTC)
    lead = _lead(settings, company_id, now)
    if not company_application_clear(stores, lead):
        raise OutreachHold("existing_company_application")
    linkedin = _read_combined(settings, lead)
    try:
        key = person_key(person_name)
    except ContactHold as exc:
        raise OutreachHold("linkedin_verified_person_name_required") from exc
    return Ledger(settings).reserve_linkedin_slot(
        company_id, key, linkedin, no_application=True, now=now)
