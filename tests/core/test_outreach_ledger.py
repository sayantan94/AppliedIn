"""#387 offline invariants: recorded stops, exact caps and uncertain wire."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from core.outreach_contacts import LinkedInSnapshot, VerifiedRecipient
from core.outreach_ledger import Ledger, OutreachHold
from core.outreach_sources import FundedNoOpening

T = datetime(2026, 11, 20, 8, tzinfo=UTC)
EMPTY = LinkedInSnapshot(frozenset(), frozenset())


def settings(tmp_path):
    return SimpleNamespace(mode="local", outreach_enabled=True,
                           local_dir=str(tmp_path), outreach_timezone="UTC",
                           outreach_sender="sender@example-co.com")


def lead(key, now=T):
    return FundedNoOpening(key * 32, "Example Co", "example-co.com",
                           "https://example-co.com", (), "test", now, "no_openings")


def contact(name, address=None):
    return VerifiedRecipient(address or name.split()[0].lower() + "@example-co.com",
                             "name:" + name.lower(), name, "person",
                             "https://example-co.com/team")


def reserve(db, l, c, now=T, linkedin=EMPTY, stage=0):
    return db.reserve_email(
        l, c, linkedin, no_application=True, resume_seed_proved=True,
        sender_proved=True, variant="owner-approved",
        digest="a"*64, stage=stage, now=now)


def poll(db, at=T):
    db.record_successful_poll(uidvalidity="12", monitored_address="sender@example-co.com",
                              now=at)


def accept(db, item, at=T):
    db.begin_wire(item["id"], source_checked=True, no_application=True,
                  linkedin_checked=True, now=at)
    db.finish_wire(item["id"], accepted=True, now=at)


def test_default_off_and_stale_inbound_hold(tmp_path):
    s = settings(tmp_path)
    s.outreach_enabled = False
    with pytest.raises(OutreachHold, match="disabled"):
        Ledger(s)
    s.outreach_enabled = True
    db = Ledger(s)
    with pytest.raises(OutreachHold, match="inbound_poll_required"):
        reserve(db, lead("a"), contact("Test User"))
    poll(db, T - timedelta(minutes=16))
    with pytest.raises(OutreachHold, match="inbound_poll_stale"):
        reserve(db, lead("a"), contact("Test User"))


def test_linkedin_two_plus_email_third_refuse_fourth_and_duplicate(tmp_path):
    db = Ledger(settings(tmp_path))
    poll(db)
    linked = LinkedInSnapshot(frozenset({"name:alpha person", "name:beta person"}),
                              frozenset())
    a = reserve(db, lead("a"), contact("Test User"), linkedin=linked)
    assert a["tracking_mode"] == "not_tracked"
    with pytest.raises(OutreachHold, match="three_person_cap"):
        reserve(db, lead("a"), contact("Fourth Person"), linkedin=linked)
    with pytest.raises(OutreachHold, match="duplicate_first_contact"):
        reserve(db, lead("a"), contact("Test User"), linkedin=linked)
    with pytest.raises(OutreachHold, match="linkedin_already_contacted_person"):
        reserve(db, lead("b"), contact("Alpha Person"), linkedin=linked)


def test_stop_before_reservation_and_after_reservation_before_wire(tmp_path):
    db = Ledger(settings(tmp_path))
    poll(db)
    l = lead("a")
    db.record_stop(l.company_id, kind="opt_out", now=T)
    with pytest.raises(OutreachHold, match="recorded_company"):
        reserve(db, l, contact("Test User"))
    db2 = Ledger(settings(tmp_path / "other"))
    poll(db2)
    ticket = reserve(db2, l, contact("Test User"))
    db2.record_stop(l.company_id, kind="human_reply", now=T)
    with pytest.raises(OutreachHold, match="recorded_company"):
        db2.begin_wire(ticket["id"], source_checked=True,
                       no_application=True, linkedin_checked=True, now=T)


def test_no_wire_releases_day_capacity_but_wire_unknown_never_replays(tmp_path):
    db = Ledger(settings(tmp_path))
    poll(db)
    l = lead("a")
    c = contact("Test User")
    before = reserve(db, l, c)
    db.definite_no_wire(before["id"])
    next_send = reserve(db, l, c)
    assert next_send["message_id"] != before["message_id"]
    db.begin_wire(next_send["id"], source_checked=True,
                  no_application=True, linkedin_checked=True, now=T)
    db.hold_orphaned_attempts()
    with pytest.raises(OutreachHold, match="duplicate_first_contact"):
        reserve(db, l, c)
    with pytest.raises(OutreachHold, match="followup_thread_not_definitively"):
        reserve(db, l, c, stage=1, now=T)


def test_accepted_followup_new_message_id_and_exact_thread_chain(tmp_path):
    db = Ledger(settings(tmp_path))
    poll(db)
    l, c = lead("a"), contact("Test User")
    first = reserve(db, l, c)
    accept(db, first)
    later = T + timedelta(days=7, seconds=1)
    poll(db, later)
    second = reserve(db, lead("a", later), c, stage=1, now=later)
    assert second["message_id"] != first["message_id"]
    assert second["in_reply_to"] == first["message_id"]
    assert second["references"] == [first["message_id"]]
    accept(db, second, at=later)
    later2 = later + timedelta(days=7, seconds=1)
    poll(db, later2)
    third = reserve(db, lead("a", later2), c, stage=2, now=later2)
    assert third["references"] == [first["message_id"], second["message_id"]]
    assert third["in_reply_to"] == second["message_id"]


def test_ramp_and_hard_bounce_company_vs_global(tmp_path):
    db = Ledger(settings(tmp_path))
    poll(db)
    items = []
    for i, name in enumerate(("Alpha Person", "Beta Person", "Gamma Person")):
        ticket = reserve(db, lead(chr(97+i)), contact(name))
        accept(db, ticket)
        items.append(ticket)
    assert db.confirm_hard_bounce(items[0]["id"], now=T)
    private = db.report(lead("a").company_id, EMPTY)
    assert private["attempts"][0]["hard_bounced"] is True
    assert private["attempts"][0]["recipient_stop"] == "hard_bounce"
    assert private["attempts"][0]["approved_variant"] == "owner-approved"
    assert not db.report(lead("b").company_id, EMPTY)["global_email_pause"]
    with pytest.raises(OutreachHold, match="company_email_held"):
        reserve(db, lead("a"), contact("Another Person"))
    assert not db.confirm_hard_bounce(items[0]["id"], now=T)
    db.confirm_hard_bounce(items[1]["id"], now=T)
    assert not db.report(lead("c").company_id, EMPTY)["global_email_pause"]
    db.confirm_hard_bounce(items[2]["id"], now=T)
    assert db.report(lead("c").company_id, EMPTY)["global_email_pause"]
    with pytest.raises(OutreachHold, match="global_email_bounce_pause"):
        reserve(db, lead("d"), contact("Another Person"))


def test_company_day_one_ramp_5_including_pending(tmp_path):
    db = Ledger(settings(tmp_path))
    poll(db)
    for i in range(5):
        reserve(db, lead(chr(97+i)), contact("Test User"))
    with pytest.raises(OutreachHold, match="daily_ramp"):
        reserve(db, lead("f"), contact("Test User"))


def test_failed_imap_pass_revokes_earlier_fresh_poll(tmp_path):
    """A recent successful poll is not enough after a later failed pass."""
    db = Ledger(settings(tmp_path))
    poll(db)
    db.invalidate_poll()
    with pytest.raises(OutreachHold, match="inbound_poll_required"):
        reserve(db, lead("a"), contact("Test User"))
    poll(db)
    ticket = reserve(db, lead("a"), contact("Test User"))
    assert ticket["stage"] == 0

def test_reply_ingest_python_syntax_without_import_side_effects():
    """Catch invalid cold-reply branches even if IMAP module import is broken."""
    import ast
    from pathlib import Path

    source_path = Path(__file__).resolve().parents[2] / "src/core/reply_ingest.py"
    ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))


def test_company_organizational_alias_cannot_hide_named_recipient_overlap(tmp_path):
    """One published organizational inbox cannot be re-contacted as a person."""
    db = Ledger(settings(tmp_path))
    poll(db)
    l = lead("a")
    organization = VerifiedRecipient(
        "careers@example-co.com", "org", "", "organization",
        "https://example-co.com/contact")
    reserve(db, l, organization)
    overlap = VerifiedRecipient(
        "careers@example-co.com", "name:recruiter person",
        "Recruiter Person", "person", "https://example-co.com/team")
    with pytest.raises(OutreachHold, match="recipient_alias_or_org_overlap"):
        reserve(db, l, overlap)
    # The alias must not consume a second first-contact or another quota slot.
    # The report's daily quota is keyed to the requested campaign day,
    # not the wall-clock day on which CI happens to read it.
    assert db.report(l.company_id, EMPTY, now=T)["email_quota_used"] == 1
    assert len(db.report(l.company_id, EMPTY, now=T)["attempts"]) == 1


def test_two_verified_person_names_sharing_address_are_held_for_reconciliation(tmp_path):
    db = Ledger(settings(tmp_path))
    poll(db)
    l = lead("a")
    reserve(db, l, contact("First Person", address="shared@example-co.com"))
    with pytest.raises(OutreachHold, match="recipient_alias_or_org_overlap"):
        reserve(db, l, contact("Second Person", address="shared@example-co.com"))


def test_all_ten_campaign_ramp_days_and_later_hard_twenty(tmp_path):
    """Synthetic reservations only; no sender, mailbox or transport is opened."""
    from core.outreach_ledger import RAMP
    db = Ledger(settings(tmp_path))
    assert RAMP == (5, 7, 9, 10, 12, 14, 15, 17, 19, 20)
    for elapsed, cap in enumerate((*RAMP, 20)):
        current = T + timedelta(days=elapsed)
        poll(db, current)
        for i in range(cap):
            key = f"{elapsed * 100 + i + 1:032x}"
            l = FundedNoOpening(key, "Example Co", "example-co.com",
                                "https://example-co.com", (), "synthetic",
                                current, "no_openings")
            reserve(db, l, contact("Test User"), now=current)
        extra = FundedNoOpening(
            f"{elapsed * 100 + 99:032x}", "Example Co", "example-co.com",
            "https://example-co.com", (), "synthetic", current, "no_openings")
        with pytest.raises(OutreachHold, match="email_daily_ramp_or_hard_cap"):
            reserve(db, extra, contact("Test User"), now=current)


def test_bounce_ratio_pauses_at_thirtieth_acceptance_not_at_twenty_ninth(tmp_path):
    """The >=30 denominator threshold is checked on both DSN and later SMTP ACK."""
    db = Ledger(settings(tmp_path))
    tickets = []
    idx = 0
    for elapsed, count in enumerate((5, 7, 9, 8)):
        current = T + timedelta(days=elapsed)
        poll(db, current)
        for _ in range(count):
            idx += 1
            lead_row = FundedNoOpening(
                f"{idx:032x}", "Example Co", "example-co.com",
                "https://example-co.com", (), "synthetic",
                current, "no_openings")
            ticket = reserve(db, lead_row, contact("Test User"), now=current)
            accept(db, ticket, at=current)
            tickets.append(ticket)
    assert len(tickets) == 29
    at = T + timedelta(days=3)
    assert db.confirm_hard_bounce(tickets[0]["id"], now=at)
    assert not db.report(lead("b").company_id, EMPTY)["global_email_pause"]
    poll(db, at)
    last = FundedNoOpening(
        f"{30:032x}", "Example Co", "example-co.com",
        "https://example-co.com", (), "synthetic", at, "no_openings")
    thirtieth = reserve(db, last, contact("Test User"), now=at)
    accept(db, thirtieth, at=at)
    assert db.report(lead("b").company_id, EMPTY)["global_email_pause"]
    with pytest.raises(OutreachHold, match="global_email_bounce_pause"):
        reserve(db, lead("z", at), contact("Test User"), now=at)
