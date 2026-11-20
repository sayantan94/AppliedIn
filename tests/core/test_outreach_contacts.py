"""No guessed person/address relation or silent LinkedIn schema fallback."""
import json
from types import SimpleNamespace

import pytest

from core.outreach_contacts import (ContactHold, read_linkedin,
                                    verify_published_contact)


def test_exact_official_mailto_with_full_name_only():
    page = "<div>Test User <a href='mailto:test@example-co.com'>Contact</a></div>"
    contact = verify_published_contact(
        company_domain="example-co.com", source_url="https://example-co.com/team",
        html=page, address="test@example-co.com", name="Test User")
    assert contact.person_key == "name:test user"
    with pytest.raises(ContactHold, match="binding_missing"):
        verify_published_contact(
            company_domain="example-co.com", source_url="https://example-co.com/team",
            html=page, address="test@example-co.com", name="Different Person")
    with pytest.raises(ContactHold, match="publication_missing"):
        verify_published_contact(
            company_domain="example-co.com", source_url="https://example-co.com/team",
            html=page, address="guessed@example-co.com", name="Test User")


def test_private_linkedin_reader_never_guesses_and_never_writes(tmp_path):
    s = SimpleNamespace(local_dir=str(tmp_path), outreach_linkedin_path="",
                        outreach_linkedin_fields="")
    with pytest.raises(ContactHold, match="requires_private_schema_mapping"):
        read_linkedin(s, company_id="a"*32, company_name="Example Co",
                      company_domain="example-co.com")
    path = tmp_path / "existing-linkedin.json"
    original = [{
        "org": "a"*32, "label": "Example Co", "domain": "example-co.com",
        "name": "Test User", "url": "https://linkedin.com/in/test-user",
        "state": "contacted",
    }]
    payload = json.dumps(original)
    path.write_text(payload)
    s.outreach_linkedin_path = path.name
    s.outreach_linkedin_fields = json.dumps({
        "company_id": "org", "company_name": "label", "domain": "domain",
        "person_name": "name", "profile": "url", "status": "state"})
    snapshot = read_linkedin(s, company_id="a"*32, company_name="Example Co",
                             company_domain="example-co.com")
    assert snapshot.people == {"name:test user"}
    assert path.read_text() == payload
    original[0]["state"] = "unknown"
    path.write_text(json.dumps(original))
    with pytest.raises(ContactHold, match="state_unmapped"):
        read_linkedin(s, company_id="a"*32, company_name="Example Co",
                      company_domain="example-co.com")


def test_named_mailto_requires_same_small_employer_published_container():
    """Nearby unrelated names and other mailto links cannot bind a person."""
    common = dict(company_domain="example-co.com",
                  source_url="https://example-co.com/team",
                  address="test@example-co.com", name="Test User")
    ambiguous = (
        "<div>Test User <a href='mailto:other@example-co.com'>Other Person</a>"
        "<a href='mailto:test@example-co.com'>Contact</a></div>"
    )
    with pytest.raises(ContactHold, match="binding_missing"):
        verify_published_contact(html=ambiguous, **common)
    sibling = (
        "<div>Test User</div>"
        "<div>Other Person <a href='mailto:test@example-co.com'>Contact</a></div>"
    )
    with pytest.raises(ContactHold, match="binding_missing"):
        verify_published_contact(html=sibling, **common)
    exact = (
        "<div>Other Person <a href='mailto:other@example-co.com'>Other</a></div>"
        "<div>Test User <a href='mailto:test@example-co.com'>Contact</a></div>"
    )
    assert verify_published_contact(html=exact, **common).person_key == "name:test user"


def test_colliding_linkedin_names_refuse_auto_and_cost_distinct_manual_slots(tmp_path):
    """Two independently verified /in/ URLs cannot merge on the same full name."""
    rows = [{
        "org":"a"*32, "label":"Example Co", "domain":"example-co.com",
        "name":"Test User", "url":f"https://linkedin.com/in/test-user-{suffix}",
        "state":"contacted",
    } for suffix in ("one", "two")]
    path = tmp_path / "existing-linkedin.json"
    path.write_text(json.dumps(rows))
    settings = SimpleNamespace(local_dir=str(tmp_path),
        outreach_linkedin_path=path.name, outreach_linkedin_fields=json.dumps({
            "company_id":"org", "company_name":"label", "domain":"domain",
            "person_name":"name", "profile":"url", "status":"state"}))
    keys = dict(company_id="a"*32, company_name="Example Co",
                company_domain="example-co.com")
    with pytest.raises(ContactHold, match="linkedin_name_profile_collision_requires_review"):
        read_linkedin(settings, **keys)
    observed = read_linkedin(settings, strict=False, **keys)
    assert len(observed.profiles) == 2
    assert len(observed.people) == 2
    assert all(p.startswith("provisional:linkedin:") for p in observed.people)
    assert "linkedin_name_profile_collision_requires_reconciliation" in observed.warnings
    # One duplicate tracker row is not a third distinct person.
    path.write_text(json.dumps(rows + [rows[0]]))
    repeated = read_linkedin(settings, strict=False, **keys)
    assert repeated.people == observed.people
