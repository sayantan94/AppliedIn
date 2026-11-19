"""Exact published contact evidence and private LinkedIn read-only projection.

The original owner-private LinkedIn schema is not in this repository. Require
an operator-supplied private field mapping; never guess it or write its file.
"""
from __future__ import annotations
import hashlib
import json
import re
from dataclasses import dataclass
from email.utils import parseaddr
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from discovery.funding import registrable


class ContactHold(ValueError):
    pass


def normalize_email(value):
    if not isinstance(value, str) or not 3 < len(value) <= 254:
        raise ContactHold("invalid_exact_address")
    normalized = parseaddr(value)[1].lower()
    if normalized != value.strip().lower() or not re.fullmatch(
            r"[a-z0-9.!#$%&'*+/=?^_\x60{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}", normalized):
        raise ContactHold("invalid_exact_address")
    return normalized


def person_key(name):
    if not isinstance(name, str):
        raise ContactHold("person_name_unverified")
    normalized = " ".join(name.casefold().split())
    if not 5 <= len(normalized) <= 100 or len(normalized.split()) < 2:
        raise ContactHold("person_name_unverified")
    return "name:" + normalized


def profile_key(value):
    try:
        p = urlsplit(value.strip())
        parts = unquote(p.path).strip("/").split("/")
        if (p.scheme != "https" or p.hostname not in {"linkedin.com", "www.linkedin.com"}
                or p.username or p.password or p.query or p.fragment or
                len(parts) != 2 or parts[0] != "in" or not re.fullmatch(
                    r"[a-zA-Z0-9_.-]{2,120}", parts[1])):
            raise ContactHold("linkedin_profile_unverified")
        return "linkedin:" + parts[1].casefold()
    except (AttributeError, ValueError) as exc:
        raise ContactHold("linkedin_profile_unverified") from exc


class _PublishedPersonBinding(HTMLParser):
    """Bind a mailto to the name in its own smallest rendered container.

    A name somewhere else on a team page or beside another mailbox is not
    evidence for this person's exact recipient. Icon-only and unscoped names
    remain held until the operator finds a stronger publication.
    """
    CONTAINERS = frozenset({"div", "li", "p", "section", "article", "tr", "td"})

    def __init__(self, target):
        super().__init__(convert_charrefs=True)
        self.target = target
        self.stack = []
        self.anchors = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        if tag in self.CONTAINERS:
            self.stack.append({"tag": tag, "text": [], "emails": set()})
        if tag == "a":
            links = [str(v) for k, v in attrs
                     if k.casefold() == "href" and isinstance(v, str)]
            mail = links[0].split("?", 1)[0][7:].casefold() if (
                len(links) == 1 and links[0].casefold().startswith("mailto:")) else ""
            parent = self.stack[-1] if self.stack else None
            if mail and parent is not None:
                parent["emails"].add(mail)
            self.current = {"mail": mail, "parent": parent, "text": []}

    def handle_data(self, data):
        if self.stack:
            frame = self.stack[-1]
            if sum(map(len, frame["text"])) < 500:
                frame["text"].append(data[:500])
        if self.current is not None and sum(map(len, self.current["text"])) < 500:
            self.current["text"].append(data[:500])

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag == "a" and self.current is not None:
            self.anchors.append(self.current)
            self.current = None
        if tag in self.CONTAINERS and self.stack:
            if self.stack[-1]["tag"] == tag:
                self.stack.pop()
            else:
                # Malformed nested structures cannot establish a precise link.
                self.stack.clear()

    def binds(self, full_name):
        required = " ".join(full_name.casefold().split())
        boundary = re.compile(r"(?<!\\w)" + re.escape(required) + r"(?!\\w)")
        for anchor in self.anchors:
            if anchor["mail"] != self.target:
                continue
            label = " ".join(" ".join(anchor["text"]).casefold().split())
            if boundary.search(label):
                return True
            parent = anchor["parent"]
            if parent is not None and parent["emails"] == {self.target}:
                context = " ".join(" ".join(parent["text"]).casefold().split())
                if len(context) <= 400 and boundary.search(context):
                    return True
        return False


@dataclass(frozen=True)
class VerifiedRecipient:
    address: str
    person_key: str
    name: str
    kind: str
    evidence_url: str


def verify_published_contact(*, company_domain, source_url, html, address, name=""):
    """Require an official employer HTTPS page and exactly one literal mailto.

    The caller supplies bounded HTML obtained through #386's guarded HTTPS
    public-IP reader; there is no inferred email-pattern/RCPT fallback.
    """
    p = urlsplit(source_url)
    if (p.scheme != "https" or p.username or p.password or p.fragment
            or p.port not in (None, 443) or registrable(source_url) != company_domain):
        raise ContactHold("contact_source_not_verified_official")
    address = normalize_email(address)
    if address.rsplit("@", 1)[1] != company_domain:
        raise ContactHold("contact_domain_mismatch")
    if not isinstance(html, str) or len(html.encode("utf-8")) > 256000:
        raise ContactHold("contact_source_unbounded")
    payload = unescape(html)
    hits = list(re.finditer(r"<a\b[^>]{0,400}href\s*=\s*['\"]mailto:"
                           + re.escape(address) + r"(?:\?[^'\"]*)?['\"][^>]{0,400}>",
                           payload, re.I))
    if len(hits) != 1:
        raise ContactHold("exact_address_publication_missing")
    if name:
        key = person_key(name)
        witness = _PublishedPersonBinding(address)
        witness.feed(payload)
        if not witness.binds(name):
            raise ContactHold("named_person_exact_address_binding_missing")
        return VerifiedRecipient(address, key, name.strip(), "person", source_url)
    if address.split("@", 1)[0] not in {
            "career", "careers", "job", "jobs", "hello", "contact", "talent", "recruiting"}:
        raise ContactHold("not_organizational_inbox")
    return VerifiedRecipient(address, "org", "", "organization", source_url)


@dataclass(frozen=True)
class LinkedInSnapshot:
    people: frozenset[str]
    profiles: frozenset[str]
    warnings: frozenset[str] = frozenset()


def read_linkedin(settings, *, company_id, company_name, company_domain, strict=True):
    """No implicit schema. Configured JSON keys describe the existing source."""
    source, mapping = (getattr(settings, "outreach_linkedin_path", ""),
                       getattr(settings, "outreach_linkedin_fields", ""))
    if not source or not mapping:
        raise ContactHold("linkedin_reader_requires_private_schema_mapping")
    root = Path(settings.local_dir).resolve()
    source_path = root / source
    path = source_path.resolve()
    if (not path.is_relative_to(root) or path == root or source_path.is_symlink()
            or not path.is_file() or path.stat().st_size > 2_000_000):
        raise ContactHold("linkedin_readonly_source_unsafe_or_missing")
    try:
        fields = json.loads(mapping)
        required = {"company_id", "company_name", "domain", "person_name", "profile", "status"}
        if (not isinstance(fields, dict) or set(fields) != required
                or any(not isinstance(v, str) or not re.fullmatch(
                    r"[A-Za-z][A-Za-z0-9_]{0,50}", v) for v in fields.values())):
            raise ValueError
        records = json.loads(path.read_text("utf-8"))
        if not isinstance(records, list) or len(records) > 10000:
            raise ValueError
    except (OSError, ValueError, UnicodeError) as exc:
        raise ContactHold("linkedin_private_source_unreadable") from exc
    people, profiles, warnings = set(), set(), set()
    for row in records:
        if not isinstance(row, dict):
            raise ContactHold("linkedin_row_invalid")
        status = row.get(fields["status"])
        if status not in {"contacted", "reserved", "uncertain", "not_contacted"}:
            raise ContactHold("linkedin_state_unmapped")
        same_id = row.get(fields["company_id"]) == company_id
        same_domain = row.get(fields["domain"]) == company_domain
        same_name = str(row.get(fields["company_name"], "")).casefold() == company_name.casefold()
        if not (same_id or same_domain or same_name):
            continue
        conflict = (not (same_id or same_domain)
                    or row.get(fields["company_id"]) not in (None, "", company_id)
                    or row.get(fields["domain"]) not in (None, "", company_domain))
        if conflict and strict:
            raise ContactHold("linkedin_employer_join_ambiguous")
        if status == "not_contacted":
            continue
        name, profile = row.get(fields["person_name"]), row.get(fields["profile"])
        try:
            identity = person_key(name)
            if profile:
                profiles.add(profile_key(profile))
        except ContactHold:
            if strict:
                raise
            identity = None
        if conflict or identity is None:
            if strict:
                raise ContactHold("linkedin_name_join_missing")
            # Conservatively count uncertain existing contacts rather than
            # pretending an unmapped record frees a company slot.
            raw_key = json.dumps(row, sort_keys=True, ensure_ascii=True)
            identity = "provisional:linkedin:" + hashlib.sha256(
                raw_key.encode("utf-8")).hexdigest()[:32]
            warnings.add("linkedin_identity_or_employer_join_unresolved")
        people.add(identity)
    return LinkedInSnapshot(frozenset(people), frozenset(profiles),
                            frozenset(warnings))
