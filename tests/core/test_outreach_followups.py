"""No pixel is a supported, deterministic seven-day schedule."""
from datetime import UTC, datetime, timedelta

from core.outreach_followups import due_for_band, earliest_due, within_stage_limit


def test_pixel_less_and_late_six_opens_do_not_postpone_earned_due():
    sent = datetime(2026, 11, 20, 8, tzinfo=UTC)
    first = due_for_band(sent, tracked=False)
    assert first == sent + timedelta(days=7)
    late = [sent + timedelta(days=6, hours=i) for i in range(6)]
    new = due_for_band(sent, opens=late, tracked=True)
    assert earliest_due(first, new) == first


def test_three_to_five_open_band_and_stage_cap():
    sent = datetime(2026, 11, 20, 8, tzinfo=UTC)
    opens = tuple(sent + timedelta(days=1, hours=i) for i in range(3))
    assert due_for_band(sent, opens=opens, tracked=True) == sent + timedelta(days=3)
    assert within_stage_limit(1, 0)
    assert within_stage_limit(2, 1)
    assert not within_stage_limit(3, 2)
    assert within_stage_limit(3, 2, expanded=True)
    assert not within_stage_limit(6, 5, expanded=True)


def test_qualified_opens_are_one_per_utc_day_and_latest_attempt_only(tmp_path):
    """A late 6+ event can never postpone an already earned S+7d stage."""
    from types import SimpleNamespace
    from core.outreach_ledger import Ledger
    from core.outreach_contacts import LinkedInSnapshot, VerifiedRecipient
    from core.outreach_sources import FundedNoOpening

    s = SimpleNamespace(mode="local", outreach_enabled=True, local_dir=str(tmp_path),
                        outreach_timezone="UTC", outreach_sender="sender@example-co.com",
                        outreach_followups_expanded=True)
    db = Ledger(s)
    sent = datetime(2026, 11, 20, 8, tzinfo=UTC)
    db.record_successful_poll(uidvalidity="2", monitored_address=s.outreach_sender, now=sent)
    lead = FundedNoOpening("a"*32, "Example Co", "example-co.com",
                           "https://example-co.com", (), "synthetic", sent, "no_openings")
    contact = VerifiedRecipient("test@example-co.com", "name:test user",
                                "Test User", "person", "https://example-co.com/team")
    attempt = db.reserve_email(
        lead, contact, LinkedInSnapshot(frozenset(), frozenset()),
        no_application=True, resume_seed_proved=True, sender_proved=True,
        variant="approved-v1", digest="a"*64, now=sent)
    token = "c"*64
    db.register_pixel(attempt["id"], token, now=sent)
    db.begin_wire(attempt["id"], source_checked=True, no_application=True,
                  linkedin_checked=True, now=sent)
    db.finish_wire(attempt["id"], accepted=True, now=sent)
    due = db.earn_due(lead.company_id, contact.person_key, 1, sent + timedelta(days=7))
    assert due == (sent + timedelta(days=7)).timestamp()
    for day in range(6):
        seen = sent + timedelta(days=6, hours=day * 25)
        db.record_pixel_open(token, "qualified", now=seen)
        db.record_pixel_open(token, "qualified", now=seen + timedelta(minutes=3))
    candidates = db.due_followups(now=sent + timedelta(days=14))
    assert len(candidates) == 1
    assert candidates[0]["stage"] == 1
    report = db.report(lead.company_id, LinkedInSnapshot(frozenset(), frozenset()),
                       now=sent + timedelta(days=14))
    persisted = report["earliest_earned_due"][0]["due_at"]
    assert persisted == due
    assert report["opens"]["qualified"] == 6


def test_earned_expansion_survives_later_unopened_followups(tmp_path):
    """Three qualified original opens permit stage 3 even if stages 1/2 had none."""
    from types import SimpleNamespace
    from core.outreach_ledger import Ledger
    from core.outreach_contacts import LinkedInSnapshot, VerifiedRecipient
    from core.outreach_sources import FundedNoOpening

    t0 = datetime(2026, 11, 20, 8, tzinfo=UTC)
    settings = SimpleNamespace(
        mode="local", outreach_enabled=True, local_dir=str(tmp_path),
        outreach_timezone="UTC", outreach_sender="sender@example-co.com",
        outreach_followups_expanded=True)
    db = Ledger(settings)
    linkedin = LinkedInSnapshot(frozenset(), frozenset())
    person = VerifiedRecipient("test@example-co.com", "name:test user",
                               "Test User", "person", "https://example-co.com/team")
    def lead(at):
        return FundedNoOpening("a"*32, "Example Co", "example-co.com",
                               "https://example-co.com", (), "synthetic",
                               at, "no_openings")
    for stage in range(3):
        at = t0 + timedelta(days=stage * 7)
        db.record_successful_poll(uidvalidity="2", monitored_address=settings.outreach_sender,
                                  now=at)
        ticket = db.reserve_email(
            lead(at), person, linkedin, no_application=True, resume_seed_proved=True,
            sender_proved=True, variant="approved", digest="a"*64,
            stage=stage, now=at, expanded_followups=True)
        if stage == 0:
            db.register_pixel(ticket["id"], "e"*64, now=at)
        db.begin_wire(ticket["id"], source_checked=True, no_application=True,
                      linkedin_checked=True, now=at)
        db.finish_wire(ticket["id"], accepted=True, now=at)
        if stage == 0:
            for day in (1, 2, 3):
                db.record_pixel_open("e"*64, "qualified", now=at + timedelta(days=day))
    t3 = t0 + timedelta(days=21)
    db.record_successful_poll(uidvalidity="2", monitored_address=settings.outreach_sender,
                              now=t3)
    due = db.due_followups(now=t3)
    assert len(due) == 1 and due[0]["stage"] == 3
    ticket = db.reserve_email(
        lead(t3), person, linkedin, no_application=True, resume_seed_proved=True,
        sender_proved=True, variant="approved", digest="a"*64,
        stage=3, now=t3, expanded_followups=True)
    assert ticket["stage"] == 3
