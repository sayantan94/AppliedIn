"""#393 synthetic, offline queue, pacing, crash and fairness invariants."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from core.outreach_contacts import LinkedInSnapshot, VerifiedRecipient
from core.outreach_ledger import Ledger, OutreachHold
from core.outreach_sources import FundedNoOpening

T = datetime(2026, 11, 20, 10, tzinfo=UTC)
EMPTY = LinkedInSnapshot(frozenset(), frozenset())


def cfg(tmp_path, **overrides):
    values = dict(mode="local", outreach_enabled=True, local_dir=str(tmp_path),
                  outreach_timezone="UTC", outreach_sender="sender@example-co.com",
                  outreach_followups_auto=True, outreach_followups_expanded=False)
    values.update(overrides)
    return SimpleNamespace(**values)


def lead(char, now=T):
    return FundedNoOpening(char * 32, "Example Co", "example-co.com",
                           "https://example-co.com", (), "synthetic", now, "no_openings")


def contact(name="Test User", address="test@example-co.com"):
    return VerifiedRecipient(address, "name:" + name.lower(), name,
                             "person", "https://example-co.com/team")


def poll(db, now=T):
    db.record_successful_poll(uidvalidity="12", monitored_address="sender@example-co.com",
                              now=now)


def intent(db, char, name, addr, *, now=T, stage=0, approved="approved"):
    return db.enqueue_email(lead(char, now), contact(name, addr), stage=stage,
                            variant_id=approved, digest="a" * 64, now=now)


def reserve(db, char, name, addr, claim, *, now=T, stage=0, subject="First Subject"):
    return db.reserve_email(
        lead(char,now), contact(name,addr), EMPTY, no_application=True,
        resume_seed_proved=True, sender_proved=True,
        variant="approved", digest="a"*64, stage=stage,
        now=now, expanded_followups=False,
        queue_id=claim["id"], owner_token=claim["owner_token"],
        root_subject=subject if stage==0 else None)


class FixedGap:
    def __init__(self, value):
        self.value = value

    def randint(self, lower, upper):
        assert (lower,upper)==(180,600)
        return self.value


def begin(db, attempt, now=T, gap=180):
    db.begin_wire(attempt["id"], source_checked=True, no_application=True,
                  linkedin_checked=True, now=now, gap_rng=FixedGap(gap))


def test_enqueue_is_not_a_reservation_and_dedupes_stable_identity(tmp_path):
    db = Ledger(cfg(tmp_path))
    first = intent(db,"a","Test User","test@example-co.com")
    repeated = intent(db,"a","Test User","test@example-co.com")
    assert first == repeated and first["state"]=="queued"
    with pytest.raises(OutreachHold,match="queue_recipient_identity_changed"):
        intent(db,"a","Test User","changed@example-co.com")
    with db._tx() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]==0
        assert conn.execute("SELECT COUNT(*) FROM email_queue").fetchone()[0]==1
        assert conn.execute("SELECT active_attempt,next_not_before FROM email_dispatch"
                            ).fetchone()==(None,None)


@pytest.mark.parametrize("gap", [180, 297, 600])
def test_global_atomic_fence_distinct_queue_rows_and_persisted_gap(tmp_path, gap):
    s=cfg(tmp_path)
    db=Ledger(s)
    poll(db)
    ids=[
        intent(db,"a","Test User","test@example-co.com")["queue_id"],
        intent(db,"b","Other User","other@example-co.com")["queue_id"]]
    a=db.claim_queue(now=T)
    b=db.claim_queue(now=T)
    # Identical fake timestamps do not define which UUID sorts first.
    claims={a["id"]:a,b["id"]:b}
    assert set(claims)==set(ids)
    first=reserve(db,"a","Test User","test@example-co.com",claims[ids[0]])
    second=reserve(db,"b","Other User","other@example-co.com",claims[ids[1]])
    # Handoff 1 is immediately eligible; SQLite commits before fake network.
    begin(db,first,gap=gap)
    with Ledger(s)._tx() as other_connection:
        assert other_connection.execute("SELECT active_attempt FROM email_dispatch"
                                        ).fetchone()[0]==first["id"]
    with pytest.raises(OutreachHold,match="another_smtp_wire"):
        begin(Ledger(s),second,gap=180)
    db.finish_wire(first["id"],accepted=True,now=T)
    restarted=Ledger(s)
    with pytest.raises(OutreachHold,match="dispatch_gap_not_elapsed"):
        begin(restarted,second,now=T+timedelta(seconds=gap-1))
    begin(restarted,second,now=T+timedelta(seconds=gap),gap=600)
    restarted.finish_wire(second["id"],accepted=False,now=T+timedelta(seconds=gap))
    assert restarted.queue_status(ids[1])["state"]=="uncertain"
    with restarted._tx() as conn:
        state=conn.execute("SELECT active_attempt,last_handoff,next_not_before "
                           "FROM email_dispatch").fetchone()
        assert state==(None,(T+timedelta(seconds=gap)).timestamp(),
                       (T+timedelta(seconds=gap+600)).timestamp())


def test_crash_boundaries_have_no_claim_steal_and_no_wire_replay(tmp_path):
    s=cfg(tmp_path)
    db=Ledger(s)
    poll(db)
    q=intent(db,"a","Test User","test@example-co.com")["queue_id"]
    claim=db.claim_queue(now=T)
    db.hold_orphaned_attempts()
    assert Ledger(s).queue_status(q)["state"]=="claimed"
    with pytest.raises(OutreachHold,match="positive_owner_termination"):
        db.recover_claim_with_owner_proof(q,expected_version=2)
    db.recover_claim_with_owner_proof(q,expected_version=2,owner_inactive_proved=True,now=T)
    claim=db.claim_queue(now=T+timedelta(minutes=5))
    assert claim and claim["id"]==q
    ticket=reserve(db,"a","Test User","test@example-co.com",claim,
                   now=T+timedelta(minutes=5))
    db.hold_orphaned_attempts()
    assert db.queue_status(q)["state"]=="reserved"
    with pytest.raises(OutreachHold,match="queue_version_or_state_changed"):
        db.repair_queue(q,expected_version=db.queue_status(q)["version"],
                        expected_state="held",variant_id="approved",digest="a"*64)
    db.definite_no_wire(ticket["id"],owner_token=claim["owner_token"])
    status=db.queue_status(q)
    assert status["state"]=="held"
    with pytest.raises(OutreachHold,match="charged_or_unproved"):
        db.repair_queue(q,expected_version=status["version"],expected_state="held",
                        variant_id="approved",digest="a"*64)
    repaired=db.repair_queue(q,expected_version=status["version"],expected_state="held",
                             variant_id="approved",digest="a"*64,owner_no_wire_proved=True,
                             now=T+timedelta(minutes=5))
    assert repaired["queue_id"]==q
    claim2=db.claim_queue(now=T+timedelta(minutes=5))
    second=reserve(db,"a","Test User","test@example-co.com",claim2,
                   now=T+timedelta(minutes=5))
    assert second["message_id"]!=ticket["message_id"]
    begin(db,second,now=T+timedelta(minutes=5))
    db.hold_orphaned_attempts()
    assert db.queue_status(q)["state"]=="in_flight"
    another=intent(db,"b","Other User","other@example-co.com",
                   now=T+timedelta(minutes=7))["queue_id"]
    assert Ledger(s).claim_queue(now=T+timedelta(minutes=7)) is None
    assert db.queue_status(q)["state"]=="in_flight"
    blocked=db.queue_status(another)
    assert blocked["state"]=="held"
    assert blocked["reason"]=="another_smtp_wire_unresolved"
    assert blocked["next_action"]=="wait_for_global_smtp_fence"


def test_versioned_no_name_hold_repair_and_permanent_stop(tmp_path):
    db=Ledger(cfg(tmp_path))
    q=db.enqueue_email(lead("a"),contact(),stage=0,reason="approved_variant_missing")
    status=db.queue_status(q["queue_id"])
    assert status["state"]=="held"
    assert "selected_variant" not in status and "recipient" not in status
    with pytest.raises(OutreachHold,match="queue_version_or_state_changed"):
        db.repair_queue(q["queue_id"],expected_version=99,expected_state="held",
                        variant_id="approved",digest="a"*64)
    fixed=db.repair_queue(q["queue_id"],expected_version=status["version"],
                          expected_state="held",variant_id="approved",digest="a"*64,now=T)
    assert fixed["queue_id"]==q["queue_id"]
    claim=db.claim_queue(now=T)
    db.release_claim(q["queue_id"],claim["owner_token"],
                     "recorded_company_or_recipient_stop",cancel=True,now=T)
    with pytest.raises(OutreachHold,match="queue_version_or_state_changed"):
        db.repair_queue(q["queue_id"],expected_version=db.queue_status(q["queue_id"])["version"],
                        expected_state="held",variant_id="approved",digest="a"*64)


@pytest.mark.parametrize("hour,minute,weekday,allowed", [
    (8,59,4,False),(9,0,4,True),(16,59,4,True),(17,0,4,False),
    (12,0,5,False),(12,0,6,False)])
def test_business_window_exact_boundaries(tmp_path,hour,minute,weekday,allowed):
    db=Ledger(cfg(tmp_path))
    # Nov 20 2026 is Friday; move to Saturday/Sunday only in the weekend cases.
    when=datetime(2026,11,20+max(0,weekday-4),hour,minute,tzinfo=UTC)
    if allowed:
        db._window(when)
    else:
        with pytest.raises(OutreachHold,match="business_window_closed"):
            db._window(when)


def test_dst_configured_zone_and_invalid_zone_hold(tmp_path):
    db=Ledger(cfg(tmp_path,outreach_timezone="America/New_York"))
    # UTC instants convert to unambiguous local wall-clock times across DST.
    db._window(datetime(2026,11,20,15,tzinfo=UTC))
    with pytest.raises(OutreachHold,match="business_window_closed"):
        db._window(datetime(2026,11,20,22,tzinfo=UTC))
    for raw in ("No/Such_Zone",""):
        broken=Ledger(cfg(tmp_path/outreach_timezone_test_root(raw),
                          outreach_timezone=raw))
        with pytest.raises(OutreachHold,match="outreach_timezone"):
            broken._window(T)


def outreach_timezone_test_root(value):
    return "missing" if not value else "invalid"


def test_first_logical_subject_and_followup_references_survive_restart(tmp_path):
    db=Ledger(cfg(tmp_path))
    poll(db)
    intent(db,"a","Test User","test@example-co.com")
    claimed=db.claim_queue(now=T)
    first=reserve(db,"a","Test User","test@example-co.com",claimed,
                  subject="Résumé – original Subject")
    begin(db,first)
    db.finish_wire(first["id"],accepted=True,now=T)
    later=T+timedelta(days=7)
    poll(db,later)
    intent(db,"a","Test User","test@example-co.com",now=later,stage=1)
    claimed2=Ledger(db.settings).claim_queue(now=later)
    second=reserve(db,"a","Test User","test@example-co.com",claimed2,
                   now=later,stage=1)
    assert second["root_subject"]=="Résumé – original Subject"
    assert second["in_reply_to"]==first["message_id"]
    assert second["references"]==[first["message_id"]]


def test_more_than_100_older_queued_stages_do_not_starve_later_eligible(tmp_path):
    db=Ledger(cfg(tmp_path))
    accepted=T-timedelta(days=7)
    with db._tx() as conn:
        for i in range(131):
            key=f"{i:032x}"
            attempt=f"accepted-{i}"
            recipient=f"test-{i}@example-co.com"
            conn.execute("""INSERT INTO attempts
                (id,company_id,person_key,recipient,contact_url,contact_name,
                 message_id,stage,state,approved_variant,approved_digest,
                 reserved_at,accepted_at,day,prior_id,references_json)
                VALUES (?,?,?,?,?,?,?,0,'confirmed_smtp_accepted',?,?,?,?,?,?,?)""",
                (attempt,key,"name:test user",recipient,"https://example-co.com/team",
                 "Test User",f"<test-{i}@example-co.com>","approved","a"*64,
                 accepted.timestamp(),accepted.timestamp(),accepted.date().isoformat(),
                 None,"[]"))
            if i<130:
                conn.execute("""INSERT INTO email_queue
                    (id,company_id,person_key,recipient,contact_url,contact_name,
                     stage,state,reason,created_at,updated_at,next_check_at)
                    VALUES (?,?,?,?,?,?,1,'held','stage_variant_missing',?,?,?)""",
                    (f"{i:032x}",key,"name:test user",recipient,
                     "https://example-co.com/team","Test User",
                     accepted.timestamp(),accepted.timestamp(),accepted.timestamp()))
    result=db.due_followups(now=T,limit=1)
    assert len(result)==1
    assert result[0]["company_id"]==f"{130:032x}"
    assert result[0]["stage"]==1


def test_recorded_stop_cancels_unattempted_intents_before_window(tmp_path):
    """A permanent stop cannot wait for a future business window to take effect."""
    db=Ledger(cfg(tmp_path))
    one=intent(db,"a","Test User","test@example-co.com")["queue_id"]
    two=intent(db,"a","Other User","other@example-co.com")["queue_id"]
    db.record_stop("a"*32,kind="application",now=T)
    for queue_id in (one,two):
        status=db.queue_status(queue_id)
        assert status["state"]=="canceled"
        assert status["reason"]=="recorded_company_or_recipient_stop"
        with pytest.raises(OutreachHold,match="queue_version_or_state_changed"):
            db.repair_queue(queue_id,expected_version=status["version"],
                            expected_state="held",variant_id="approved",digest="a"*64)
    with db._tx() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]==0


def test_due_followup_producer_freezes_named_variant_without_smtp(tmp_path,monkeypatch):
    """Accepted predecessor and stage mapping can create an uncharged queue row."""
    from core import outreach_email as mail

    s=cfg(tmp_path,outreach_followup_stage_variants=(
        '{"1":{"named":"stage1-named","team":"stage1-team"}}'))
    db=Ledger(s)
    poll(db)
    intent(db,"a","Test User","test@example-co.com")
    first_claim=db.claim_queue(now=T)
    first=reserve(db,"a","Test User","test@example-co.com",first_claim)
    begin(db,first)
    db.finish_wire(first["id"],accepted=True,now=T)
    later=T+timedelta(days=7)
    candidate=db.due_followups(now=later,limit=1)[0]
    monkeypatch.setattr(mail,"approved_variant",lambda settings,variant_id,kind: {
        "id":variant_id,"sha256":"b"*64,"subject":"Unused follow-up subject",
        "body":"Approved synthetic text.","role_family":"PM"})
    result=mail.enqueue_due_followup(settings=s,candidate=candidate,now=later)
    q=db.queue_intent(result["queue_id"])
    assert result["state"]=="queued"
    assert q["stage"]==1
    assert q["selected_variant"]=="stage1-named"
    assert q["selected_digest"]=="b"*64
    with db._tx() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]==1


def test_corrupt_global_dispatch_metadata_is_a_visible_hold(tmp_path):
    """Corruption must fail closed with an inspectable private next action."""
    db=Ledger(cfg(tmp_path))
    queue_id=intent(db,"a","Test User","test@example-co.com")["queue_id"]
    with db._tx() as conn:
        conn.execute("UPDATE email_dispatch SET last_handoff=?,next_not_before=?",
                     (T.timestamp()-100,T.timestamp()-90))
    assert db.claim_queue(now=T) is None
    status=db.queue_status(queue_id)
    assert status["state"]=="held"
    assert status["reason"]=="dispatch_clock_or_metadata_invalid"
    assert status["next_action"]=="owner_reconcile_dispatch_metadata"
