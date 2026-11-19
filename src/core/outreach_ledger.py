"""Private SQLite admission ledger for the local, default-OFF outreach email lane.

Transactions linearize *recorded* stops, budget and person reservations.
SMTP, inbound mail and independently operated LinkedIn are not atomic with
this database. A wire timeout is uncertain and is never retried blindly.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.outreach_contacts import LinkedInSnapshot, VerifiedRecipient, normalize_email
from core.outreach_followups import due_for_band
from core.outreach_sources import FundedNoOpening

RAMP = (5, 7, 9, 10, 12, 14, 15, 17, 19, 20)
STATES = frozenset({"reserved", "in_flight", "confirmed_smtp_accepted",
                    "definite_pre_wire_failure", "uncertain"})


class OutreachHold(ValueError):
    """A deterministic refusal with an operator-remediable reason."""


def _time(now=None):
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise OutreachHold("timezone_required")
    return now.astimezone(UTC)


class Ledger:
    """Use one connection per operation, SQLite WAL and BEGIN IMMEDIATE."""

    def __init__(self, settings):
        if (getattr(settings, "mode", None) != "local"
                or not getattr(settings, "outreach_enabled", False)):
            raise OutreachHold("outreach_disabled_or_not_local")
        self.settings = settings
        root = Path(settings.local_dir)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = root / "outreach-ledger.sqlite3"
        if self.path.is_symlink():
            raise OutreachHold("unsafe_private_ledger_path")
        with self._tx() as db:
            for statement in """
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS people (
                    company_id TEXT NOT NULL, person_key TEXT NOT NULL,
                    channel TEXT NOT NULL, provisional INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (company_id, person_key));
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, company_id TEXT NOT NULL,
                    person_key TEXT NOT NULL, recipient TEXT NOT NULL,
                    contact_url TEXT NOT NULL, contact_name TEXT NOT NULL,
                    message_id TEXT NOT NULL UNIQUE,
                    stage INTEGER NOT NULL, state TEXT NOT NULL,
                    approved_variant TEXT NOT NULL, approved_digest TEXT NOT NULL,
                    reserved_at REAL NOT NULL, accepted_at REAL, day TEXT NOT NULL,
                    prior_id TEXT, references_json TEXT NOT NULL,
                    tracking_mode TEXT NOT NULL DEFAULT 'not_tracked',
                    UNIQUE (company_id, person_key, stage, message_id));
                CREATE INDEX IF NOT EXISTS attempts_day ON attempts(day, state);
                CREATE INDEX IF NOT EXISTS attempts_company ON attempts(company_id, person_key);
                CREATE TABLE IF NOT EXISTS stops (
                    company_id TEXT NOT NULL, person_key TEXT NOT NULL,
                    kind TEXT NOT NULL, observed_at REAL NOT NULL,
                    PRIMARY KEY (company_id, person_key));
                CREATE TABLE IF NOT EXISTS email_holds (
                    company_id TEXT PRIMARY KEY, reason TEXT NOT NULL, observed_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS human_responses (
                    company_id TEXT NOT NULL, person_key TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    PRIMARY KEY(company_id, person_key));
                CREATE TABLE IF NOT EXISTS bounces (
                    attempt_id TEXT PRIMARY KEY, observed_at REAL NOT NULL,
                    FOREIGN KEY(attempt_id) REFERENCES attempts(id));
                CREATE TABLE IF NOT EXISTS inbound (
                    uidvalidity TEXT NOT NULL, uid INTEGER NOT NULL, classification TEXT NOT NULL,
                    PRIMARY KEY (uidvalidity, uid));
                CREATE TABLE IF NOT EXISTS pixel_tokens (
                    token_sha TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL, FOREIGN KEY(attempt_id) REFERENCES attempts(id));
                CREATE TABLE IF NOT EXISTS pixel_events (
                    token_sha TEXT NOT NULL, observed_at REAL NOT NULL,
                    utc_day INTEGER NOT NULL, classification TEXT NOT NULL,
                    PRIMARY KEY(token_sha, utc_day, classification));
                CREATE TABLE IF NOT EXISTS manual_targets (
                    id TEXT PRIMARY KEY, company_id TEXT NOT NULL,
                    channel TEXT NOT NULL, person_key TEXT NOT NULL,
                    person_name TEXT NOT NULL, target_url TEXT NOT NULL,
                    evidence_url TEXT NOT NULL, message TEXT NOT NULL,
                    variant_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'ready',
                    version INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
                    marked_at REAL, CHECK (state IN ('ready','copied','owner_marked_sent')));
                CREATE INDEX IF NOT EXISTS manual_by_company
                    ON manual_targets(company_id, state);
                CREATE TABLE IF NOT EXISTS earned_due (
                    company_id TEXT NOT NULL, person_key TEXT NOT NULL,
                    stage INTEGER NOT NULL, due_at REAL NOT NULL,
                    PRIMARY KEY(company_id, person_key, stage));
                CREATE TABLE IF NOT EXISTS expanded_followups (
                    company_id TEXT NOT NULL, person_key TEXT NOT NULL,
                    earned_at REAL NOT NULL, PRIMARY KEY(company_id, person_key));
            """.split(";"):
                if statement.strip():
                    db.execute(statement)
        os.chmod(self.path, 0o600)

    @contextmanager
    def _tx(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @staticmethod
    def _meta(db, key):
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _set_meta(db, key, value):
        db.execute("INSERT INTO meta(key,value) VALUES (?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, str(value)))

    def _day(self, db, now):
        tz_name = getattr(self.settings, "outreach_timezone", "UTC")
        try:
            zone = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise OutreachHold("campaign_timezone_invalid") from exc
        prior_zone = self._meta(db, "timezone")
        if prior_zone and prior_zone != tz_name:
            raise OutreachHold("campaign_timezone_immutable")
        self._set_meta(db, "timezone", tz_name)
        day = now.astimezone(zone).date()
        first = self._meta(db, "first_day")
        if not first:
            self._set_meta(db, "first_day", day.isoformat())
            first = day.isoformat()
        from datetime import date
        elapsed = (day - date.fromisoformat(first)).days
        if elapsed < 0:
            raise OutreachHold("campaign_clock_rollback")
        return day.isoformat(), min(20, RAMP[min(elapsed, len(RAMP)-1)])

    def _fresh_poll(self, db, now):
        raw = self._meta(db, "poll")
        if not raw:
            raise OutreachHold("inbound_poll_required")
        poll = json.loads(raw)
        age = now.timestamp() - poll["completed_at"]
        if (age < 0 or age > 900 or not poll.get("uidvalidity")
                or poll.get("monitored_address") !=
                normalize_email(getattr(self.settings, "outreach_sender", ""))):
            raise OutreachHold("inbound_poll_stale_or_unverified")

    def invalidate_poll(self):
        """A failed/incomplete IMAP pass invalidates the prior freshness witness.

        Never reuse an earlier successful pass after an observed failure; only
        a later complete UIDVALIDITY-scoped pass may permit outbound mail.
        """
        with self._tx() as db:
            self._set_meta(db, "poll", "")

    def record_successful_poll(self, *, uidvalidity, monitored_address, now=None):
        """Call only after a complete, exception-free UIDVALIDITY-scoped IMAP pass."""
        now = _time(now)
        if not re.fullmatch(r"[1-9]\d*", str(uidvalidity)):
            raise OutreachHold("imap_uidvalidity_unverified")
        monitored = normalize_email(monitored_address)
        if monitored != normalize_email(getattr(self.settings, "outreach_sender", "")):
            raise OutreachHold("inbound_sender_identity_unverified")
        with self._tx() as db:
            self._set_meta(db, "poll", json.dumps({
                "uidvalidity": str(uidvalidity), "monitored_address": monitored,
                "completed_at": now.timestamp()}))

    def _stopped(self, db, company_id, person_key):
        rows = db.execute("SELECT person_key FROM stops WHERE company_id=? "
                          "AND person_key IN ('*', ?)", (company_id, person_key)).fetchall()
        if rows:
            raise OutreachHold("recorded_company_or_recipient_stop")
        if db.execute("SELECT 1 FROM email_holds WHERE company_id=?", (company_id,)).fetchone():
            raise OutreachHold("company_email_held_for_review")
        if self._meta(db, "global_email_pause") == "1":
            raise OutreachHold("global_email_bounce_pause")

    def reserve_email(self, lead: FundedNoOpening, contact: VerifiedRecipient,
                      linkedin: LinkedInSnapshot, *, no_application: bool,
                      resume_seed_proved: bool, sender_proved: bool,
                      variant: str, digest: str, stage=0, now=None,
                      expanded_followups=False):
        """One atomic admission. Caller must re-read sources at pre-wire.

        Distinct first-email person keys are matched to the read-only LinkedIn
        snapshot on every reservation. Separate company-level org mailbox is
        limited to one first attempt but is not misreported as a named person.
        """
        now = _time(now)
        if (not isinstance(lead, FundedNoOpening) or
                not isinstance(contact, VerifiedRecipient) or
                not isinstance(linkedin, LinkedInSnapshot) or
                not no_application or not resume_seed_proved or not sender_proved):
            raise OutreachHold("source_contact_application_or_sender_proof_missing")
        if (lead.verified_at.tzinfo is None or lead.verified_at > now
                or now - lead.verified_at > timedelta(days=7)):
            raise OutreachHold("funded_no_opening_recheck_stale")
        if (contact.kind not in {"person", "organization"}
                or (contact.kind == "organization") != (contact.person_key == "org")
                or normalize_email(contact.address).rsplit("@", 1)[1] != lead.domain):
            raise OutreachHold("verified_contact_required")
        if not isinstance(variant, str) or not 1 <= len(variant) <= 80:
            raise OutreachHold("approved_variant_missing")
        if not re.fullmatch(r"[a-f0-9]{64}", str(digest)):
            raise OutreachHold("approved_variant_digest_missing")
        if type(stage) is not int or stage < 0 or stage > 5:
            raise OutreachHold("invalid_followup_stage")
        with self._tx() as db:
            self._fresh_poll(db, now)
            self._stopped(db, lead.company_id, contact.person_key)
            day, day_cap = self._day(db, now)
            charged = db.execute("SELECT COUNT(*) FROM attempts WHERE day=? "
                                 "AND state != 'definite_pre_wire_failure'", (day,)).fetchone()[0]
            if charged >= day_cap:
                raise OutreachHold("email_daily_ramp_or_hard_cap")
            # An organizational mailbox may be publicly aliased to a named
            # contact (or two purported names may share one exact address).
            # Neither an org slot nor a new person key permits a second first
            # contact to the same actual mailbox. Hold ambiguous overlap.
            if stage == 0 and db.execute(
                    "SELECT 1 FROM attempts WHERE company_id=? AND recipient=? "
                    "AND person_key!=? AND stage=0 "
                    "AND state!='definite_pre_wire_failure'",
                    (lead.company_id, contact.address, contact.person_key)).fetchone():
                raise OutreachHold("recipient_alias_or_org_overlap_requires_review")
            existing = db.execute(
                "SELECT stage,state,recipient,message_id,accepted_at,id,tracking_mode FROM attempts "
                "WHERE company_id=? AND person_key=? AND state != 'definite_pre_wire_failure' "
                "ORDER BY stage, reserved_at",
                (lead.company_id, contact.person_key)).fetchall()
            existing_keys = {row[0] for row in db.execute(
                "SELECT person_key FROM people WHERE company_id=?", (lead.company_id,))}
            linked = set(linkedin.people)
            if (contact.person_key != "org" and (len(existing_keys | linked |
                    {contact.person_key}) > 3)):
                raise OutreachHold("hard_company_three_person_cap")
            if stage == 0:
                if contact.person_key != "org" and contact.person_key in existing_keys:
                    raise OutreachHold("cross_channel_person_already_reserved_or_contacted")
                if contact.person_key != "org" and contact.person_key in linked:
                    raise OutreachHold("linkedin_already_contacted_person")
                if existing:
                    raise OutreachHold("duplicate_first_contact")
                references, prior_id = [], None
            else:
                if (not existing or any(row[1] != "confirmed_smtp_accepted" for row in existing)
                        or len(existing) != stage
                        or [row[0] for row in existing] != list(range(stage))
                        or not all(row[2] == contact.address for row in existing)):
                    raise OutreachHold("followup_thread_not_definitively_accepted")
                opens = self._qualified_opens(db, existing[-1][5])
                if stage > 2 and (not expanded_followups or not
                        self._ever_qualified_three(db, lead.company_id, contact.person_key)):
                    raise OutreachHold("followup_expansion_requires_three_qualified_opens")
                previous_at = datetime.fromtimestamp(existing[-1][4], UTC)
                if now - previous_at < timedelta(hours=48):
                    raise OutreachHold("followup_minimum_spacing")
                due = db.execute(
                    "SELECT due_at FROM earned_due WHERE company_id=? AND person_key=? AND stage=?",
                    (lead.company_id, contact.person_key, stage)).fetchone()
                computed = due_for_band(
                    previous_at, opens=opens,
                    tracked=existing[-1][6] == "pixel_enabled").timestamp()
                earned = min(due[0], computed) if due else computed
                db.execute("INSERT INTO earned_due VALUES (?,?,?,?) "
                           "ON CONFLICT(company_id,person_key,stage) "
                           "DO UPDATE SET due_at=min(earned_due.due_at,excluded.due_at)",
                           (lead.company_id, contact.person_key, stage, earned))
                if now.timestamp() < earned:
                    raise OutreachHold("followup_not_yet_due")
                references = [row[3] for row in existing]
                prior_id = references[-1]
            if contact.person_key == "org":
                prior_org = db.execute("SELECT 1 FROM attempts WHERE company_id=? AND person_key='org' "
                                       "AND stage=0 AND state != 'definite_pre_wire_failure'",
                                       (lead.company_id,)).fetchone()
                if prior_org and stage == 0:
                    raise OutreachHold("organizational_mailbox_already_contacted")
            if contact.person_key != "org":
                db.execute("INSERT OR IGNORE INTO people(company_id,person_key,channel) "
                           "VALUES (?,?,'email')", (lead.company_id, contact.person_key))
            attempt_id = uuid.uuid4().hex
            sender = normalize_email(getattr(self.settings, "outreach_sender", ""))
            domain = sender.rsplit("@", 1)[1]
            message_id = "<" + uuid.uuid4().hex + "." + uuid.uuid4().hex + "@" + domain + ">"
            db.execute("""INSERT INTO attempts
                (id,company_id,person_key,recipient,contact_url,contact_name,
                 message_id,stage,state,approved_variant,approved_digest,
                 reserved_at,day,prior_id,references_json)
                VALUES (?,?,?,?,?,?,?,?,'reserved',?,?,?,?,?,?)""",
                (attempt_id, lead.company_id, contact.person_key, contact.address,
                 contact.evidence_url, contact.name, message_id, stage,
                 variant, digest, now.timestamp(), day, prior_id,
                 json.dumps(references)))
        return {"id": attempt_id, "message_id": message_id, "to": contact.address,
                "in_reply_to": prior_id, "references": references,
                "stage": stage, "variant": variant, "digest": digest,
                "tracking_mode": "not_tracked"}

    def begin_wire(self, attempt_id, *, source_checked: bool,
                   no_application: bool, linkedin_checked: bool, now=None):
        """Last local stop/freshness check before irreversible SMTP work."""
        now = _time(now)
        if not (source_checked and no_application and linkedin_checked):
            raise OutreachHold("prewire_source_application_or_linkedin_unverified")
        with self._tx() as db:
            row = db.execute("SELECT company_id,person_key,state FROM attempts WHERE id=?",
                             (attempt_id,)).fetchone()
            if row is None or row[2] != "reserved":
                raise OutreachHold("attempt_not_reserved")
            self._fresh_poll(db, now)
            self._stopped(db, row[0], row[1])
            db.execute("UPDATE attempts SET state='in_flight' WHERE id=?", (attempt_id,))

    def finish_wire(self, attempt_id, *, accepted: bool, now=None):
        """Only an unambiguous SMTP 250 acceptance is accepted; otherwise uncertain."""
        now = _time(now)
        with self._tx() as db:
            row = db.execute("SELECT state FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None or row[0] != "in_flight":
                raise OutreachHold("wire_attempt_not_in_flight")
            db.execute("UPDATE attempts SET state=?, accepted_at=? WHERE id=?",
                       ("confirmed_smtp_accepted" if accepted else "uncertain",
                        now.timestamp() if accepted else None, attempt_id))

    def definite_no_wire(self, attempt_id):
        """Only permitted for reserved attempts *before* begin_wire."""
        with self._tx() as db:
            row = db.execute("SELECT company_id,person_key,state FROM attempts WHERE id=?",
                             (attempt_id,)).fetchone()
            if not row or row[2] != "reserved":
                raise OutreachHold("cannot_prove_no_wire_after_begin")
            db.execute("UPDATE attempts SET state='definite_pre_wire_failure' WHERE id=?",
                       (attempt_id,))
            db.execute("DELETE FROM pixel_events WHERE token_sha IN "
                       "(SELECT token_sha FROM pixel_tokens WHERE attempt_id=?)",
                       (attempt_id,))
            db.execute("DELETE FROM pixel_tokens WHERE attempt_id=?", (attempt_id,))
            if row[1] != "org" and not db.execute(
                    "SELECT 1 FROM attempts WHERE company_id=? AND person_key=? "
                    "AND state!='definite_pre_wire_failure'",
                    (row[0], row[1])).fetchone():
                db.execute("DELETE FROM people WHERE company_id=? AND person_key=? "
                           "AND channel='email'", (row[0], row[1]))


    def earn_due(self, company_id, person_key, stage, calculated):
        """Persist earliest earned due even when a later quota check holds."""
        if calculated.tzinfo is None or not 1 <= stage <= 5:
            raise OutreachHold("followup_due_invalid")
        with self._tx() as db:
            db.execute("INSERT INTO earned_due VALUES (?,?,?,?) "
                       "ON CONFLICT(company_id,person_key,stage) "
                       "DO UPDATE SET due_at=min(earned_due.due_at,excluded.due_at)",
                       (company_id, person_key, stage, calculated.timestamp()))
            return db.execute("SELECT due_at FROM earned_due WHERE "
                              "company_id=? AND person_key=? AND stage=?",
                              (company_id, person_key, stage)).fetchone()[0]

    def due_followups(self, *, now=None, limit=10):
        """Only complete accepted threads, not uncertain/recovered sends."""
        now = _time(now)
        limit = max(1, min(int(limit), 25))
        max_stage = 5 if getattr(self.settings, "outreach_followups_expanded", False) else 2
        with self._tx() as db:
            rows = db.execute("""
                SELECT a.company_id,a.person_key,a.recipient,a.contact_url,
                       a.contact_name,a.stage,a.accepted_at,a.id,a.tracking_mode
                FROM attempts a
                WHERE a.state='confirmed_smtp_accepted'
                  AND a.stage < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM attempts b
                      WHERE b.company_id=a.company_id
                        AND b.person_key=a.person_key
                        AND b.state!='definite_pre_wire_failure'
                        AND b.stage>a.stage)
                ORDER BY a.accepted_at ASC LIMIT 100
            """, (max_stage,)).fetchall()
        result = []
        for company, person, recipient, url, name, stage, accepted_at, attempt_id, mode in rows:
            with self._tx() as db:
                opens = self._qualified_opens(db, attempt_id)
            if stage >= 2:
                with self._tx() as db:
                    if not self._ever_qualified_three(db, company, person):
                        continue
            accepted = datetime.fromtimestamp(accepted_at, UTC)
            earned = self.earn_due(
                company, person, stage+1,
                due_for_band(accepted, opens=opens, tracked=mode == "pixel_enabled"))
            if earned <= now.timestamp():
                result.append({"company_id": company, "person_key": person,
                               "recipient": recipient, "contact_url": url,
                               "person_name": name, "stage": stage+1})
            if len(result) >= limit:
                break
        return result

    def clear_reviewed_email_hold(self, company_id, *, reviewed=False):
        if reviewed is not True:
            raise OutreachHold("owner_review_required_before_email_reenable")
        with self._tx() as db:
            db.execute("DELETE FROM email_holds WHERE company_id=?", (company_id,))

    def reenable_global_email(self, *, owner_reviewed=False):
        """Explicit repair only; preserve historical bounce evidence."""
        if owner_reviewed is not True:
            raise OutreachHold("owner_review_required_before_email_reenable")
        with self._tx() as db:
            self._set_meta(db, "global_email_pause", "0")

    def hold_orphaned_attempts(self):
        """Call on process startup, not during concurrent sends; never auto-replay."""
        with self._tx() as db:
            db.execute("UPDATE attempts SET state='uncertain' "
                       "WHERE state IN ('reserved','in_flight')")

    def record_stop(self, company_id, *, person_key="*", kind="human_reply", now=None):
        now = _time(now)
        if kind not in {"human_reply", "opt_out", "application", "rejection", "hard_bounce"}:
            raise OutreachHold("invalid_stop_classification")
        with self._tx() as db:
            db.execute("INSERT INTO stops VALUES (?,?,?,?) "
                       "ON CONFLICT(company_id,person_key) DO NOTHING",
                       (company_id, person_key, kind, now.timestamp()))

    def record_matched_human_response(self, attempt_id, *, opt_out=False, now=None):
        """Atomically stop the company and dedup the human-response numerator.

        An uncertain attempted send may still receive a real reply (stop it),
        but it cannot enter the accepted-first-email response numerator.
        Every accepted follow-up belongs to its original first-email thread.
        """
        now = _time(now)
        with self._tx() as db:
            row = db.execute(
                "SELECT company_id,person_key FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise OutreachHold("human_response_unknown_thread")
            company, person = row
            db.execute("INSERT INTO stops VALUES (?,?,?,?) "
                       "ON CONFLICT(company_id,person_key) DO NOTHING",
                       (company, "*", "opt_out" if opt_out else "human_reply",
                        now.timestamp()))
            accepted_first = db.execute(
                "SELECT 1 FROM attempts WHERE company_id=? AND person_key=? "
                "AND stage=0 AND state='confirmed_smtp_accepted'",
                (company, person)).fetchone()
            if accepted_first:
                db.execute("INSERT OR IGNORE INTO human_responses VALUES (?,?,?)",
                           (company, person, now.timestamp()))

    def hold_company_email(self, company_id, reason="ambiguous_inbound", now=None):
        now = _time(now)
        with self._tx() as db:
            db.execute("INSERT INTO email_holds VALUES (?,?,?) "
                       "ON CONFLICT(company_id) DO NOTHING",
                       (company_id, reason, now.timestamp()))

    def attempt_by_message_id(self, message_id):
        with self._tx() as db:
            row = db.execute("SELECT id,company_id,person_key,recipient,state,stage "
                             "FROM attempts WHERE message_id=?", (message_id,)).fetchone()
        return dict(zip(("id","company_id","person_key","recipient","state","stage"),row)) if row else None

    def by_recipient(self, email_address):
        with self._tx() as db:
            rows = db.execute("SELECT DISTINCT company_id FROM attempts WHERE recipient=? "
                              "AND state!='definite_pre_wire_failure'",
                              (normalize_email(email_address),)).fetchall()
        return [row[0] for row in rows]

    def record_inbound_uid(self, uidvalidity, uid, classification):
        if not re.fullmatch(r"[1-9]\d*", str(uidvalidity)) or type(uid) is not int or uid <= 0:
            raise OutreachHold("inbound_uid_unverified")
        with self._tx() as db:
            cursor = db.execute("INSERT OR IGNORE INTO inbound VALUES (?,?,?)",
                                (str(uidvalidity), uid, classification))
            return cursor.rowcount == 1

    def confirm_hard_bounce(self, attempt_id, *, now=None):
        now = _time(now)
        with self._tx() as db:
            row = db.execute("SELECT company_id,person_key,state FROM attempts WHERE id=?",
                             (attempt_id,)).fetchone()
            if not row or row[2] != "confirmed_smtp_accepted":
                raise OutreachHold("bounce_attempt_not_accepted")
            new = db.execute("INSERT OR IGNORE INTO bounces VALUES (?,?)",
                             (attempt_id, now.timestamp())).rowcount
            if not new:
                return False
            db.execute("INSERT OR IGNORE INTO stops VALUES (?,?,?,?)",
                       (row[0], row[1], "hard_bounce", now.timestamp()))
            db.execute("INSERT OR IGNORE INTO email_holds VALUES (?,?,?)",
                       (row[0], "confirmed_hard_bounce_review_required", now.timestamp()))
            count = db.execute("SELECT COUNT(*) FROM bounces WHERE observed_at>=?",
                               ((now - timedelta(days=7)).timestamp(),)).fetchone()[0]
            cohort = db.execute("SELECT COUNT(*) FROM attempts WHERE "
                                "state='confirmed_smtp_accepted' AND accepted_at>=?",
                                ((now - timedelta(days=30)).timestamp(),)).fetchone()[0]
            bounced_cohort = db.execute(
                "SELECT COUNT(*) FROM bounces AS b JOIN attempts AS a ON b.attempt_id=a.id "
                "WHERE a.state='confirmed_smtp_accepted' AND a.accepted_at>=?",
                ((now - timedelta(days=30)).timestamp(),)).fetchone()[0]
            if count >= 3 or (cohort >= 30 and bounced_cohort / cohort >= 0.03):
                self._set_meta(db, "global_email_pause", "1")
        return True


    @staticmethod
    def _ever_qualified_three(db, company_id, person_key):
        """Persisted expansion never resets when old diagnostic opens expire."""
        if db.execute(
                "SELECT 1 FROM expanded_followups WHERE company_id=? AND person_key=?",
                (company_id, person_key)).fetchone():
            return True
        row = db.execute("""
            SELECT 1 FROM pixel_events e
              JOIN pixel_tokens t ON t.token_sha=e.token_sha
              JOIN attempts a ON a.id=t.attempt_id
            WHERE a.company_id=? AND a.person_key=?
              AND a.state='confirmed_smtp_accepted'
              AND e.classification='qualified'
            GROUP BY a.id HAVING COUNT(*)>=3 LIMIT 1
        """, (company_id, person_key)).fetchone()
        if row is not None:
            db.execute("INSERT OR IGNORE INTO expanded_followups VALUES (?,?,?)",
                       (company_id, person_key, datetime.now(UTC).timestamp()))
        return row is not None

    @staticmethod
    def _qualified_opens(db, attempt_id):
        rows = db.execute(
            """SELECT e.observed_at FROM pixel_events e
               JOIN pixel_tokens t ON t.token_sha=e.token_sha
               WHERE t.attempt_id=? AND e.classification='qualified'
               ORDER BY e.observed_at""", (attempt_id,)).fetchall()
        return [datetime.fromtimestamp(r[0], UTC) for r in rows]

    def register_pixel(self, attempt_id: str, token: str, *, now=None):
        """Each attempt gets an independent opaque token before SMTP wire."""
        now = _time(now)
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
            raise OutreachHold("pixel_token_invalid")
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._tx() as db:
            row = db.execute(
                "SELECT state FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not row or row[0] != "reserved":
                raise OutreachHold("pixel_attempt_not_reserved")
            db.execute("INSERT INTO pixel_tokens VALUES (?,?,?)",
                       (digest, attempt_id, now.timestamp()))
            db.execute("UPDATE attempts SET tracking_mode='pixel_enabled' WHERE id=?",
                       (attempt_id,))

    def disable_pixel(self, attempt_id: str):
        """Revoke the optional token before wire if ingress is unsafe."""
        with self._tx() as db:
            state = db.execute("SELECT state FROM attempts WHERE id=?",
                               (attempt_id,)).fetchone()
            if not state or state[0] != "reserved":
                raise OutreachHold("pixel_change_after_wire_forbidden")
            db.execute("DELETE FROM pixel_events WHERE token_sha IN "
                       "(SELECT token_sha FROM pixel_tokens WHERE attempt_id=?)",
                       (attempt_id,))
            db.execute("DELETE FROM pixel_tokens WHERE attempt_id=?", (attempt_id,))
            db.execute("UPDATE attempts SET tracking_mode='not_tracked' WHERE id=?",
                       (attempt_id,))

    def record_pixel_open(self, token: str, classification: str, *, now=None):
        """Synchronous FULL WAL commit precedes the handler's HTTP 200."""
        now = _time(now)
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
            return False
        if classification not in {"qualified", "scanner", "unknown"}:
            raise OutreachHold("pixel_classification_invalid")
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._tx() as db:
            match = db.execute("SELECT 1 FROM pixel_tokens WHERE token_sha=?",
                               (digest,)).fetchone()
            if match is None:
                return False
            # A proxy/scanner can fetch endlessly: keep just one per category
            # per fixed UTC 24h bucket, so metrics cannot be inflated.
            db.execute("INSERT OR IGNORE INTO pixel_events VALUES (?,?,?,?)",
                       (digest, now.timestamp(), int(now.timestamp() // 86400),
                        classification))
            if classification == "qualified":
                qualified = db.execute(
                    "SELECT COUNT(*) FROM pixel_events WHERE token_sha=? "
                    "AND classification='qualified'", (digest,)).fetchone()[0]
                if qualified >= 3:
                    accepted = db.execute("""
                        SELECT a.company_id,a.person_key FROM attempts a
                        JOIN pixel_tokens t ON t.attempt_id=a.id
                        WHERE t.token_sha=? AND a.state='confirmed_smtp_accepted'
                    """, (digest,)).fetchone()
                    if accepted is not None:
                        db.execute(
                            "INSERT OR IGNORE INTO expanded_followups VALUES (?,?,?)",
                            (*accepted, now.timestamp()))
            # Bound diagnostics while retaining enough for follow-up schedules.
            db.execute("DELETE FROM pixel_events WHERE observed_at<?",
                       ((now - timedelta(days=90)).timestamp(),))
            return True

    def manual_enqueue(self, *, company_id, channel, person_key, person_name,
                       target_url, evidence_url, message, variant_id, now=None):
        """Private draft. No slot is reserved until the owner requests copy."""
        now = _time(now)
        if (channel not in {"linkedin", "discord", "telegram", "x"}
                or not isinstance(person_key, str) or not person_key
                or len(person_key) > 180 or not isinstance(message, str)
                or not 1 <= len(message) <= 4000 or not isinstance(variant_id, str)
                or not variant_id or len(variant_id) > 80
                or not isinstance(person_name, str) or len(person_name) > 100
                or not isinstance(target_url, str) or len(target_url) > 2048
                or not isinstance(evidence_url, str) or len(evidence_url) > 2048):
            raise OutreachHold("manual_target_evidence_invalid")
        ident = uuid.uuid4().hex
        with self._tx() as db:
            db.execute("""INSERT INTO manual_targets
                (id,company_id,channel,person_key,person_name,target_url,
                 evidence_url,message,variant_id,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (ident, company_id, channel, person_key, person_name,
                 target_url, evidence_url, message, variant_id, now.timestamp()))
        return {"id": ident, "version": 1}

    def manual_action(self, row_id, version, linkedin: LinkedInSnapshot, *,
                      no_application: bool, action: str, now=None):
        """Copy and mark both recheck known slots atomically.

        A copy consumes a provisional slot even when the identity is unresolved.
        Mark reuses the exact same slot, never double charges. Nothing is sent.
        """
        now = _time(now)
        if action not in {"copy", "mark"} or type(version) is not int:
            raise OutreachHold("manual_action_invalid")
        if not isinstance(linkedin, LinkedInSnapshot) or not no_application:
            raise OutreachHold("manual_linkedin_or_application_unverified")
        with self._tx() as db:
            row = db.execute(
                "SELECT company_id,person_key,state,version,message FROM manual_targets WHERE id=?",
                (row_id,)).fetchone()
            if row is None:
                raise OutreachHold("manual_row_not_found")
            company, person, state, current, message = row
            if current != version:
                raise OutreachHold("manual_row_version_conflict")
            if db.execute("SELECT 1 FROM stops WHERE company_id=? "
                          "AND person_key IN ('*', ?)", (company, person)).fetchone():
                raise OutreachHold("recorded_company_or_recipient_stop")
            if action == "copy" and state not in {"ready", "copied"}:
                raise OutreachHold("manual_copy_after_sent_not_allowed")
            if action == "copy" and state == "ready":
                if person in linkedin.people:
                    raise OutreachHold("linkedin_already_contacted_person")
                owner = db.execute(
                    "SELECT channel FROM people WHERE company_id=? AND person_key=?",
                    (company, person)).fetchone()
                if owner is not None and owner[0] != "manual":
                    raise OutreachHold("cross_channel_person_already_reserved_or_contacted")
                previous = db.execute(
                    "SELECT 1 FROM manual_targets WHERE company_id=? AND person_key=? "
                    "AND id!=? AND state!='ready'",
                    (company, person, row_id)).fetchone()
                if previous:
                    raise OutreachHold("manual_person_already_copied_on_other_row")
            if action == "mark" and state not in {"copied", "owner_marked_sent"}:
                raise OutreachHold("manual_must_copy_before_mark")
            local = {r[0] for r in db.execute(
                "SELECT person_key FROM people WHERE company_id=?", (company,))}
            linked = set(linkedin.people)
            if person not in local | linked and len(local | linked) >= 3:
                raise OutreachHold("hard_company_three_person_cap")
            if len(local | linked | {person}) > 3:
                raise OutreachHold("hard_company_three_person_cap")
            if person not in linked:
                db.execute("INSERT OR IGNORE INTO people VALUES (?,?,?,?)",
                           (company, person, "manual",
                            int(person.startswith("provisional:"))))
            next_state = "copied" if action == "copy" else "owner_marked_sent"
            if state != next_state:
                db.execute("UPDATE manual_targets SET state=?,version=version+1,"
                           "marked_at=CASE WHEN ?='mark' THEN ? ELSE marked_at END "
                           "WHERE id=?", (next_state, action, now.timestamp(), row_id))
                current += 1
            return {"id": row_id, "version": current, "state": next_state,
                    "message": message if action == "copy" else None}

    def reserve_linkedin_slot(self, company_id, person_key, linkedin: LinkedInSnapshot,
                              *, no_application: bool, now=None):
        """Operator reserves before independent LinkedIn send; tracker stays read-only."""
        _time(now)
        if not isinstance(linkedin, LinkedInSnapshot) or not no_application:
            raise OutreachHold("linkedin_coordination_unavailable")
        if not isinstance(person_key, str) or not person_key.startswith(
                ("name:", "provisional:")):
            raise OutreachHold("linkedin_person_identity_unverified")
        with self._tx() as db:
            if db.execute("SELECT 1 FROM stops WHERE company_id=? "
                          "AND person_key IN ('*', ?)", (company_id, person_key)).fetchone():
                raise OutreachHold("recorded_company_or_recipient_stop")
            local = {r[0] for r in db.execute(
                "SELECT person_key FROM people WHERE company_id=?", (company_id,))}
            linked = set(linkedin.people)
            if len(local | linked | {person_key}) > 3:
                raise OutreachHold("hard_company_three_person_cap")
            prior = db.execute(
                "SELECT channel FROM people WHERE company_id=? AND person_key=?",
                (company_id, person_key)).fetchone()
            if person_key in linked:
                # The private reader now sees the separately recorded send:
                # the reservation counts once and is never sent again here.
                if prior and prior[0] == "linkedin-reserved":
                    return {"reserved":False, "reconciled":True,
                            "remaining":3-len(local | linked)}
                raise OutreachHold("linkedin_already_contacted_person")
            if prior and prior[0] == "email":
                raise OutreachHold("cross_channel_person_already_contacted")
            db.execute("INSERT OR IGNORE INTO people VALUES (?,?,?,?)",
                       (company_id, person_key, "linkedin-reserved",
                        int(person_key.startswith("provisional:"))))
            return {"reserved": True, "remaining": 3-len(local | linked | {person_key})}

    def manual_rows(self, company_id):
        with self._tx() as db:
            rows = db.execute("""SELECT id,channel,person_name,target_url,evidence_url,
                                        message,variant_id,person_key,state,version,created_at
                                 FROM manual_targets WHERE company_id=?
                                 ORDER BY created_at DESC LIMIT 100""",
                              (company_id,)).fetchall()
        fields = ("id","channel","person_name","target_url","evidence_url",
                  "message","variant_id","person_key","state","version","created_at")
        return [dict(zip(fields, row)) for row in rows]

    def report(self, company_id, linkedin: LinkedInSnapshot, *, now=None):
        now = _time(now)
        with self._tx() as db:
            people_rows = db.execute(
                "SELECT person_key,provisional FROM people WHERE company_id=?",
                (company_id,)).fetchall()
            named = {r[0] for r in people_rows}
            provisional = {r[0] for r in people_rows if r[1]}
            attempts = db.execute("SELECT stage,state,approved_variant,tracking_mode "
                                  "FROM attempts WHERE company_id=?",
                                  (company_id,)).fetchall()
            stop = db.execute("SELECT kind FROM stops WHERE company_id=? AND person_key='*'",
                              (company_id,)).fetchone()
            held = db.execute("SELECT reason FROM email_holds WHERE company_id=?",
                              (company_id,)).fetchone()
            global_pause = self._meta(db, "global_email_pause") == "1"
            first_accepted = db.execute(
                "SELECT COUNT(*) FROM attempts WHERE company_id=? AND stage=0 "
                "AND state='confirmed_smtp_accepted'", (company_id,)).fetchone()[0]
            human_replies = db.execute(
                "SELECT COUNT(*) FROM human_responses WHERE company_id=?",
                (company_id,)).fetchone()[0]
            due_rows = db.execute(
                "SELECT person_key,stage,due_at FROM earned_due WHERE company_id=?",
                (company_id,)).fetchall()
            opening_rows = db.execute("""SELECT e.classification,COUNT(*)
                FROM pixel_events e JOIN pixel_tokens t ON t.token_sha=e.token_sha
                JOIN attempts a ON a.id=t.attempt_id
                WHERE a.company_id=? GROUP BY e.classification""",
                (company_id,)).fetchall()
            bounced = db.execute("""SELECT COUNT(*) FROM bounces b
                JOIN attempts a ON a.id=b.attempt_id
                WHERE a.company_id=?""", (company_id,)).fetchone()[0]
            # Inspecting a report cannot start the immutable ramp clock.
            from datetime import date
            zone = ZoneInfo(getattr(self.settings, "outreach_timezone", "UTC"))
            ramp_day = now.astimezone(zone).date().isoformat()
            first = self._meta(db, "first_day")
            elapsed = max(0, (date.fromisoformat(ramp_day) - date.fromisoformat(first)).days) if first else 0
            ramp_limit = RAMP[min(elapsed, len(RAMP)-1)]
            day_used = db.execute("SELECT COUNT(*) FROM attempts WHERE day=? "
                                  "AND state!='definite_pre_wire_failure'",
                                  (ramp_day,)).fetchone()[0]
        people = named | set(linkedin.people)
        return {"used":len(people), "provisional":len(provisional - set(linkedin.people)),
                "remaining":max(0, 3-len(people)), "hard_limit":3,
                "ramp_day":ramp_day, "ramp_limit":ramp_limit, "email_quota_used":day_used,
                "bounced_attempts":bounced,
                "opens":dict(opening_rows),
                "earliest_earned_due":[{"person_key":p,"stage":n,"due_at":d}
                                       for p,n,d in due_rows],
                "accepted_first_emails":first_accepted,
                "matched_human_threads":human_replies,
                "human_reply_rate":(human_replies / first_accepted
                                    if first_accepted else None),
                "company_stop":stop[0] if stop else None,
                "company_email_hold":held[0] if held else None,
                "global_email_pause":global_pause,
                "attempts":[{"stage":r[0],"state":r[1],
                             "approved_variant":r[2],"tracking":r[3]} for r in attempts]}
