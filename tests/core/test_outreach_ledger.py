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
