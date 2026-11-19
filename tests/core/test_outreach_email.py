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
