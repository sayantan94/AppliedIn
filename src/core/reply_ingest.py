"""Offline-testable, local-only recruiter reply metadata ingestion."""
from __future__ import annotations

import email
import email.policy
import email.utils
import json
import os
import re
import threading
import uuid
from pathlib import Path

from core.models import Status
from core.imap_readonly import IMAPReadError, ReadOnlyIMAP

WRITE_LOCK = threading.Lock()
STATE_NAME = "reply-ingest.json"


def enabled() -> bool:
    return os.environ.get("APPLIEDIN_IMAP_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def poll_interval() -> int | None:
    raw = os.environ.get("APPLIEDIN_IMAP_POLL_INTERVAL_SEC", "")
    try:
        value = int(raw)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def local_credentials(settings, secrets=None) -> dict | None:
    if getattr(settings, "mode", "") != "local":
        return None
    if secrets is None:
        from core.stores import make_stores
        secrets = make_stores(settings).secrets
    try:
        value = secrets.get_json("imap")
        if not isinstance(value, dict) or not all(value.get(k) for k in ("host", "mailbox", "username", "password")):
            return None
        return value
    except Exception:
        return None


def _normalized(value: str) -> str:
    return " ".join((value or "").casefold().split())


def _security_code_subject(subject: str) -> bool:
    """An application-specific one-time authentication code, not a coding test."""
    value = _normalized(subject)
    code_phrase = re.search(
        r"\b(?:security|authentication|verification|one[\s-]?time)\b[\s:._-]+"
        r"(?:\w+[\s:._-]+){0,2}(?:code|passcode|password)\b", value
    ) or re.search(r"\b(?:otp|one[\s-]?time[\s-]+password)\b", value)
    return bool(re.search(r"\b(?:application|applying)\b", value) and code_phrase)


def match_message(headers, candidates: list[dict], aliases: list[dict]) -> str:
    """Fail closed unless one eligible pk has one recorded, uncollided submitted use."""
    msg = email.message_from_bytes(headers, policy=email.policy.default)
    recipient_values = []
    for key in ("to", "cc", "delivered-to"):
        recipient_values.extend(email.utils.getaddresses(msg.get_all(key, [])))
    addressed = {address.strip().casefold() for _, address in recipient_values if address.strip()}
    if not addressed:
        return "UNASSIGNED"

    def email_of(alias):
        return str(alias.get("email") or "").strip().casefold()

    matched = [alias for alias in aliases if email_of(alias) in addressed]
    if not matched:
        return "UNASSIGNED"
    # All addressed recorded aliases must agree, including conflicting ledger rows
    # for the same email; no title/sender/company fallback can repair ambiguity.
    signatures = {
        (email_of(a), str(a.get("id") or ""),
         str(a.get("company") or "").strip().casefold(),
         tuple(sorted(str(pk) for pk in (a.get("used_pks") or []))))
        for a in matched
    }
    if len(signatures) != 1:
        return "UNASSIGNED"
    address, alias_id, company, raw_uses = next(iter(signatures))
    if not address or not alias_id or not company:
        return "UNASSIGNED"
    # rotation's IDs derive from local parts only, and record_submission writes
    # to the first matching ID. Even a singleton ledger use is unsafe on collision.
    if len({email_of(a) for a in aliases if str(a.get("id") or "") == alias_id}) != 1:
        return "UNASSIGNED"
    uses = set(raw_uses)
    if len(uses) != 1 or not next(iter(uses)):
        return "UNASSIGNED"
    pk = next(iter(uses))
    # Another alias recording this pk contradicts its provenance; checking after
    # eligible filtering would hide historical contradictions.
    if any(pk in {str(p) for p in (a.get("used_pks") or [])}
           for a in aliases if str(a.get("id") or "") != alias_id):
        return "UNASSIGNED"
    eligible = []
    for row in candidates:
        status = row.get("status")
        if getattr(status, "value", status) not in (Status.APPLIED.value, Status.APPLIED_MANUAL.value):
            continue
        if str(row.get("pk") or "") == pk:
            eligible.append(row)
    if len(eligible) != 1:
        return "UNASSIGNED"
    row = eligible[0]
    if str(row.get("company") or "").strip().casefold() != company:
        return "UNASSIGNED"
    profile = str(row.get("profile_id") or "")
    if profile and profile != alias_id:
        return "UNASSIGNED"
    return pk

def _parse_headers(raw: bytes) -> tuple[dict, bool]:
    empty = {"date": "", "sender": "", "subject": "", "message_id": ""}
    if not isinstance(raw, bytes) or not raw.strip() or b"\x00" in raw:
        return empty, True
    # The standard parser tolerates arbitrary garbage as body text. Demand a
    # syntactically recognizable header block before admitting an IMAP UID.
    lines = raw.replace(b"\r\n", b"\n").split(b"\n")
    seen = False
    for line in lines:
        if not line:
            break
        if line[:1] in (b" ", b"\t"):
            if not seen:
                return empty, True
        elif not re.match(rb"^[\x21-\x39\x3b-\x7e]+:", line):
            return empty, True
        else:
            seen = True
    if not seen:
        return empty, True
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:
        return empty, True

    def safe(name):
        try:
            return str(msg.get(name, ""))
        except Exception:
            return ""

    date, sender = safe("date"), safe("from")
    try:
        parsed = email.utils.parsedate_to_datetime(date) if date else None
        date = parsed.isoformat() if parsed else ""
    except Exception:
        date = ""
    address = email.utils.parseaddr(sender)[1] if sender else ""
    parse_failed = bool(sender and "@" not in address)
    if parse_failed:
        sender = ""
    return ({"date": date, "sender": sender, "subject": safe("subject"),
             "message_id": safe("message-id")}, parse_failed)

def _valid_message_id(value: str) -> bool:
    # A bare or malformed ID cannot authenticate a later exact-UID retry.
    return bool(isinstance(value, str) and re.fullmatch(
        r"<[^<>\s@]+@[^<>\s@]+>", value.strip()))


def _decode_text_prefix(headers: bytes, fragment: bytes) -> str:
    """Return inline text only; MIME headers and attachment parts stay out of the model input."""
    try:
        message = email.message_from_bytes(headers + fragment, policy=email.policy.default)
        texts = []
        def collect(part):
            if (part.get_content_disposition() == "attachment" or part.get_filename()
                    or part.get_content_type() == "message/rfc822"):
                return
            if part.is_multipart():
                for child in part.iter_parts():
                    collect(child)
                return
            if part.get_content_type() not in ("text/plain", "text/html"):
                return
            value = part.get_content()
            if isinstance(value, str) and value:
                texts.append(value)
        collect(message)
        return "\n".join(texts)
    except Exception:
        return ""


def _state_path(settings) -> Path:
    return Path(settings.local_dir) / STATE_NAME


def _read_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError("reply ingest state must be a JSON object")
    return value

def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as output:
        json.dump(state, output, separators=(",", ":"))
        output.flush()
        os.fsync(output.fileno())
    os.replace(tmp, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_records(settings) -> list[dict]:
    with WRITE_LOCK:
        state = _read_state(_state_path(settings))
        rows = [row for account in state.get("accounts", {}).values()
                for row in account.get("records", [])]
        return [{k: row.get(k) for k in ("record_id", "date", "sender", "subject", "message_id", "label", "pk")}
                for row in rows]


def assign(record_id: str, pk: str, settings) -> str:
    """Claim one unassigned record under the same lock as ingest commits."""
    with WRITE_LOCK:
        path = _state_path(settings)
        state = _read_state(path)
        found = None
        for account in state.get("accounts", {}).values():
            for record in account.get("records", []):
                if record.get("record_id") == record_id:
                    found = record
                    break
            if found is not None:
                break
        if found is None:
            return "not_found"
        if found.get("pk") == pk:
            return "assigned"
        if found.get("pk") != "UNASSIGNED":
            return "conflict"
        found["pk"] = pk
        _write_state(path, state)
        return "assigned"

def _account_key(creds: dict) -> str:
    return json.dumps([str(creds["host"]).casefold(), str(creds["username"]), str(creds["mailbox"])], separators=(",", ":"))


def _generation(validity) -> int | None:
    raw = str(validity or "").strip()
    if not raw.isascii() or not raw.isdecimal():
        return None
    number = int(raw)
    return number if number > 0 else None


def _account_state(path: Path, key: str, validity: str) -> tuple[dict, bool]:
    """Only a verified strictly newer generation may reset scan state."""
    incoming = _generation(validity)
    with WRITE_LOCK:
        state = _read_state(path)
        accounts = state.setdefault("accounts", {})
        account = accounts.get(key, {})
        established = _generation(account.get("uidvalidity"))
        if incoming is None or (established is not None and incoming < established):
            return account, False
        if established is None or incoming > established:
            account.setdefault("processed", [])
            account.setdefault("message_ids", [])
            account.setdefault("records", [])
            account.update(uidvalidity=str(incoming), last_uid=0, scan_pending=True,
                           uid_floor=None, unresolved_uids=[])
            accounts[key] = account
            _write_state(path, state)
        elif "scan_pending" not in account:
            account["scan_pending"] = not bool(account.get("last_uid"))
            accounts[key] = account
            _write_state(path, state)
        return account, bool(account.get("scan_pending"))


def _processed(path: Path, key: str, validity: str, uid: int) -> bool:
    with WRITE_LOCK:
        account = _read_state(path).get("accounts", {}).get(key, {})
        return (_generation(validity) is not None
                and _generation(account.get("uidvalidity")) == _generation(validity)
                and f"{validity}:{uid}" in account.get("processed", []))


def _set_first_floor(path: Path, key: str, validity: str, selected: list[int]) -> int | None:
    with WRITE_LOCK:
        state = _read_state(path)
        account = state.get("accounts", {}).get(key)
        if (not account or _generation(validity) is None
                or _generation(account.get("uidvalidity")) != _generation(validity)):
            return None
        if account.get("uid_floor") is None:
            floor = min(selected) - 1 if selected else 0
            account["uid_floor"] = floor
            account["last_uid"] = max(int(account.get("last_uid", 0)), floor)
            # Mark the entire admitted first window before attempting any UID:
            # a partial scan must not lose an unvisited UID if SEARCH later omits it.
            account["unresolved_uids"] = sorted(set(account.get("unresolved_uids", [])) | set(selected))
            _write_state(path, state)
        # Overlapping first scans must use the floor that actually won the write.
        return int(account["uid_floor"])


def _mark_unresolved(path: Path, key: str, validity: str, uid: int) -> None:
    with WRITE_LOCK:
        state = _read_state(path)
        account = state.get("accounts", {}).get(key)
        if (not account or _generation(validity) is None
                or _generation(account.get("uidvalidity")) != _generation(validity)):
            return
        if f"{validity}:{uid}" in account.get("processed", []):
            return
        holes = set(account.get("unresolved_uids", []))
        if uid not in holes:
            account["unresolved_uids"] = sorted(holes | {uid})
            _write_state(path, state)


def _commit_uid(path: Path, key: str, validity: str, uid: int,
                record: dict, message_id: str) -> bool:
    """Commit dedup, record, and exact private retry link in one locked write."""
    if _generation(validity) is None or type(uid) is not int or uid <= 0:
        return False
    with WRITE_LOCK:
        state = _read_state(path)
        account = state.get("accounts", {}).get(key)
        if not account or _generation(account.get("uidvalidity")) != _generation(validity):
            return False
        # Selection may predate another first scan's floor; fence at commit too.
        floor = account.get("uid_floor")
        if floor is not None and uid <= int(floor):
            return False
        identity = f"{validity}:{uid}"
        processed = set(account.get("processed", []))
        if identity in processed:
            return False
        message_ids = set(account.get("message_ids", []))
        duplicate = bool(message_id and message_id in message_ids)
        if not duplicate:
            account.setdefault("records", []).append(record)
            account.setdefault("record_uids", {})[record["record_id"]] = {
                "uid": uid, "uidvalidity": validity,
            }
        account["processed"] = sorted(processed | {identity})
        if message_id:
            message_ids.add(message_id)
        account["message_ids"] = sorted(message_ids)
        account["unresolved_uids"] = sorted(
            hole for hole in set(account.get("unresolved_uids", [])) if hole != uid)
        _write_state(path, state)
        return not duplicate


def _advance_cursor(path: Path, key: str, validity: str, uid: int) -> None:
    with WRITE_LOCK:
        state = _read_state(path)
        account = state.get("accounts", {}).get(key)
        if (not account or _generation(validity) is None
                or _generation(account.get("uidvalidity")) != _generation(validity)):
            return
        prefix = f"{validity}:"
        processed_uids = [
            int(item[len(prefix):]) for item in account.get("processed", [])
            if isinstance(item, str) and item.startswith(prefix)
            and item[len(prefix):].isdecimal()
        ]
        target = max([int(account.get("last_uid", 0)), uid, *processed_uids])
        witness_removed = False
        if f"{validity}:{uid}" in account.get("processed", []):
            before = account.get("unresolved_uids", [])
            after = [h for h in before if h != uid]
            if len(after) != len(before):
                account["unresolved_uids"] = after
                witness_removed = True
        floor = int(account.get("uid_floor") or 0)
        holes = [h for h in account.get("unresolved_uids", [])
                 if isinstance(h, int) and h > floor]
        if holes:
            target = min(target, min(holes) - 1)
        target = max(floor, target)
        if target > int(account.get("last_uid", 0)) or witness_removed:
            account["last_uid"] = max(int(account.get("last_uid", 0)), target)
            _write_state(path, state)


def _finish_scan(path: Path, key: str, validity: str) -> None:
    with WRITE_LOCK:
        state = _read_state(path)
        account = state.get("accounts", {}).get(key)
        if (account and _generation(validity) is not None
                and _generation(account.get("uidvalidity")) == _generation(validity)
                and account.get("uid_floor") is not None and account.get("scan_pending")):
            account["scan_pending"] = False
            _write_state(path, state)


def _retry_candidates(path: Path, key: str, validity: str) -> list[tuple[str, int, str]]:
    """Snapshot old unclassified records before new admissions in this poll."""
    with WRITE_LOCK:
        account = _read_state(path).get("accounts", {}).get(key, {})
        if _generation(validity) is None or _generation(account.get("uidvalidity")) != _generation(validity):
            return []
        links = account.get("record_uids", {})
        result = []
        for row in account.get("records", []):
            if row.get("label") != "unclassified" or not _valid_message_id(row.get("message_id")):
                continue
            link = links.get(row.get("record_id")) if isinstance(links, dict) else None
            if not isinstance(link, dict):
                continue
            uid = link.get("uid")
            if (type(uid) is int and uid > 0 and
                    _generation(link.get("uidvalidity")) == _generation(validity)):
                result.append((row["record_id"], uid, row["message_id"]))
        return result


def _promote_label(path: Path, key: str, validity: str, record_id: str,
                   uid: int, message_id: str, label: str) -> None:
    if label == "unclassified":
        return
    with WRITE_LOCK:
        state = _read_state(path)
        account = state.get("accounts", {}).get(key)
        if not account or _generation(validity) is None or _generation(account.get("uidvalidity")) != _generation(validity):
            return
        link = account.get("record_uids", {}).get(record_id)
        if link != {"uid": uid, "uidvalidity": validity}:
            return
        for row in account.get("records", []):
            if (row.get("record_id") == record_id and row.get("label") == "unclassified"
                    and row.get("message_id") == message_id):
                # Preserve manually assigned pk and every other public field.
                row["label"] = label
                _write_state(path, state)
                return


def _usable_prefix(text: str) -> bool:
    # Replacement-only decode results are not readable evidence for model success.
    return bool(text and text.replace("\ufffd", "").strip())


def _classify(fields: dict, fragment: str, classifier) -> str:
    from core.reply_classifier import validated_label
    if _security_code_subject(fields["subject"]):
        return "other"
    if not _usable_prefix(fragment) or classifier is None:
        return "unclassified"
    try:
        return validated_label(classifier(fields, fragment))
    except Exception:
        return "unclassified"



def _cold_outreach_message(ledger, settings, headers: bytes, prefix: bytes,
                           validity: str, uid: int) -> None:
    """Use full headers and <=4096 body bytes; never silently classify a DSN.

    This separate cold-email witness does not alter existing application-alias
    matching, reply labels or classifier output. The sender's domain alone
    never correlates a company.
    """
    import email.policy
    from email.utils import getaddresses
    from core.outreach_contacts import normalize_email

    msg = email.message_from_bytes(headers, policy=email.policy.default)
    body = prefix[:4096].decode("utf-8", "replace") if isinstance(prefix, bytes) else ""
    sender_list = [address.strip().casefold() for _, address in
                   getaddresses(msg.get_all("from", [])) if address]
    recipient_list = [address.strip().casefold() for _, address in
                      getaddresses(msg.get_all("to", []) + msg.get_all("cc", []) +
                                   msg.get_all("delivered-to", [])) if address]
    sender = sender_list[0] if len(sender_list) == 1 else ""
    sender_ok = sender and sender.count("@") == 1
    references = re.findall(r"<[^<>\s@]+@[^<>\s@]+>", str(msg.get("references", "")))
    parent = re.findall(r"<[^<>\s@]+@[^<>\s@]+>", str(msg.get("in-reply-to", "")))
    candidate = None
    if len(parent) == 1:
        candidate = ledger.attempt_by_message_id(parent[0])
    elif not parent and references:
        candidate = ledger.attempt_by_message_id(references[-1])
    # DSN bodies can contain a bounded original Message-ID even without a
    # useful In-Reply-To; only a unique exact persisted attempt is accepted.
    if candidate is None and not parent:
        by_body = [ledger.attempt_by_message_id(mid) for mid in set(
            re.findall(r"<[^<>\s@]+@[^<>\s@]+>", body))]
        by_body = [item for item in by_body if item is not None]
        if len(by_body) == 1:
            candidate = by_body[0]

    automatic = (str(msg.get("auto-submitted", "")).casefold() not in ("", "no")
                 or bool(msg.get("list-id")) or bool(msg.get("list-unsubscribe"))
                 or str(msg.get("precedence", "")).casefold() in {"bulk", "list", "junk"}
                 or "out of office" in str(msg.get("subject", "")).casefold()
                 or "automatic reply" in str(msg.get("subject", "")).casefold())
    indicators = (str(msg.get_content_type()).casefold() == "multipart/report"
                  and "delivery-status" in str(msg.get("content-type", "")).casefold()
                  or bool(msg.get("x-failed-recipients"))
                  or sender.split("@")[0] in {"postmaster", "mailer-daemon"})
    listed_providers = {s.strip().casefold() for s in
                        getattr(settings, "outreach_dsn_sender_addresses", "").split(",")
                        if s.strip()}
    provider_witness = (bool(getattr(settings, "outreach_provider_dsn_verified", False))
                        and sender in listed_providers)
    possible_opt_out = bool(re.search(
        r"\b(?:unsubscribe|do not (?:email|contact|write)|stop emailing)\b", body, re.I))

    if candidate:
        company = candidate["company_id"]
        monitored = normalize_email(getattr(settings, "outreach_sender", ""))
        addressed = monitored in recipient_list
        if indicators:
            # Never turn soft/unknown MIME, untrusted sender or generic
            # multipart/report into a confirmed permanent bounce.
            failed = [addr.strip().casefold() for _, addr in
                      getaddresses(msg.get_all("x-failed-recipients", []))]
            permanent = bool(re.search(r"(?:Status:\s*5\.\d\.\d|"
                                       r"Diagnostic-Code:\s*smtp;\s*5\d\d)", body, re.I))
            exact_recipient = candidate["recipient"] in failed or bool(
                re.search(r"(?<![\w.@])" + re.escape(candidate["recipient"]) +
                          r"(?![\w.@])", body, re.I))
            if (provider_witness and permanent and exact_recipient
                    and candidate["state"] == "confirmed_smtp_accepted"):
                ledger.confirm_hard_bounce(candidate["id"])
                classification = "confirmed_hard_bounce"
            else:
                ledger.hold_company_email(company, "suspected_dsn_needs_review")
                classification = "suspected_dsn"
        elif automatic:
            classification = "automatic_not_human"
        elif sender_ok and sender == candidate["recipient"] and addressed:
            ledger.record_stop(company, kind="opt_out" if possible_opt_out else "human_reply")
            classification = "matched_human_stop"
        else:
            ledger.hold_company_email(company, "ambiguous_reply_or_opt_out")
            classification = "suspected_reply"
    elif sender_ok:
        # An exact previous destination may identify a company for *holding*
        # email, but never authorizes recording a human reply without threading.
        companies = ledger.by_recipient(sender)
        if len(companies) == 1:
            ledger.hold_company_email(companies[0], "unthreaded_possible_reply")
        classification = "unmatched_or_automatic"
    else:
        classification = "unmatched"
    ledger.record_inbound_uid(validity, uid, classification)


def ingest_pass(settings, credentials: dict | None = None, stores=None, transport=None,
                classifier=None) -> int:
    if settings.mode != "local":
        return 0
    creds = credentials or local_credentials(settings)
    if not creds:
        return 0
    if stores is None:
        from core.stores import make_stores
        stores = make_stores(settings)
    from core.rotation import aliases
    candidates = [*stores.tracking.query_status(Status.APPLIED),
                  *stores.tracking.query_status(Status.APPLIED_MANUAL)]
    alias_snapshot = aliases()
    client = transport or ReadOnlyIMAP(creds["host"], creds["username"], creds["password"], creds["mailbox"])
    path = _state_path(settings)
    key = _account_key(creds)
    cold_ledger = None
    if getattr(settings, "outreach_enabled", False):
        from core.outreach_ledger import Ledger
        cold_ledger = Ledger(settings)
    inserted_count = 0
    try:
        incoming = _generation(getattr(client, "uidvalidity", None))
        if incoming is None:
            return 0
        validity = str(incoming)
        account, first_scan = _account_state(path, key, validity)
        if _generation(account.get("uidvalidity")) != incoming:
            return 0
        retries = _retry_candidates(path, key, validity)
        floor = account.get("uid_floor")
        if first_scan:
            found = sorted({uid for uid in client.search_uids("ALL")
                            if type(uid) is int and uid > 0})
            # A persisted floor survives a partial/crashed initial scan.
            uids = found[-200:] if floor is None else [uid for uid in found if uid > floor]
            committed_floor = _set_first_floor(path, key, validity, uids)
            # The winning scan may have established a higher floor during SEARCH.
            uids = [uid for uid in uids if committed_floor is not None and uid > committed_floor]
        else:
            cursor = int(account.get("last_uid", 0))
            floor = int(floor or 0)
            holes = [uid for uid in account.get("unresolved_uids", [])
                     if type(uid) is int and uid > floor]
            start = max(floor + 1, min(cursor + 1, min(holes)) if holes else cursor + 1)
            uids = [uid for uid in client.search_uids(f"UID {start}:*")
                    if type(uid) is int and uid >= start]
        for uid in sorted(set(uids)):
            if _processed(path, key, validity, uid):
                _advance_cursor(path, key, validity, uid)
                continue
            try:
                raw = client.fetch_headers(uid)
                fields, invalid = _parse_headers(raw)
            except Exception:
                raw, fields, invalid = b"", {}, True
            if invalid:
                _mark_unresolved(path, key, validity, uid)
                continue
            try:
                pk = match_message(raw, candidates, alias_snapshot)
            except Exception:
                pk = "UNASSIGNED"
            prefix = b""
            if _security_code_subject(fields["subject"]):
                label = "other"
            else:
                try:
                    prefix = client.fetch_text_prefix(uid)
                except Exception:
                    prefix = b""
                decoded = _decode_text_prefix(raw, prefix[:4096]) if isinstance(prefix, bytes) and prefix else ""
                if not _usable_prefix(decoded) and not _valid_message_id(fields["message_id"]):
                    _mark_unresolved(path, key, validity, uid)
                    continue
                label = _classify(fields, decoded, classifier)
            if cold_ledger is not None:
                try:
                    _cold_outreach_message(cold_ledger, settings, raw, prefix, validity, uid)
                except Exception:
                    # Leave the UID unresolved for a retry. Never announce a
                    # fresh inbound watermark after missing a potential stop.
                    _mark_unresolved(path, key, validity, uid)
                    continue
            record = {"record_id": str(uuid.uuid4()), **fields, "label": label, "pk": pk}
            admitted = _commit_uid(path, key, validity, uid, record, fields["message_id"])
            if admitted:
                inserted_count += 1
            _advance_cursor(path, key, validity, uid)
        _finish_scan(path, key, validity)

        # Only records present before this poll may retry. The exact UID and
        # generation are private; duplicate Message-IDs never create links.
        for record_id, uid, expected_mid in retries:
            try:
                raw = client.fetch_headers(uid)
                fields, invalid = _parse_headers(raw)
                if invalid or not _valid_message_id(fields["message_id"]) or fields["message_id"] != expected_mid:
                    continue
                prefix = client.fetch_text_prefix(uid)
                decoded = _decode_text_prefix(raw, prefix[:4096]) if isinstance(prefix, bytes) and prefix else ""
                if not _usable_prefix(decoded):
                    continue
                label = _classify(fields, decoded, classifier)
                _promote_label(path, key, validity, record_id, uid, expected_mid, label)
            except Exception:
                # Mail/provider errors are never logged or persisted as metadata.
                continue
        if cold_ledger is not None:
            with WRITE_LOCK:
                account_after = _read_state(path).get("accounts", {}).get(key, {})
                complete = (not account_after.get("scan_pending") and
                            not account_after.get("unresolved_uids") and
                            _generation(account_after.get("uidvalidity")) == incoming)
            if complete and getattr(settings, "outreach_inbound_verified", False):
                cold_ledger.record_successful_poll(
                    uidvalidity=validity, monitored_address=settings.outreach_sender)
    finally:
        client.logout()
    return inserted_count


def _production_classifier(_fields: dict, text: str) -> str:
    from core.reply_classifier import classify
    return classify(text)

def run_poller(stop_event=None) -> None:
    import logging
    import time
    from core.config import get_settings
    from core.stores import make_stores
    logger = logging.getLogger(__name__)
    while stop_event is None or not stop_event.is_set():
        try:
            settings = get_settings()
            creds = local_credentials(settings)
            if settings.mode == "local" and enabled() and creds:
                ingest_pass(settings, creds, make_stores(settings), classifier=_production_classifier)
        except Exception:
            logger.warning("IMAP poll failed; will retry on next interval")
        interval = poll_interval() or 60
        if stop_event is None:
            time.sleep(interval)
        elif stop_event.wait(interval):
            break


def secure_secrets_file(path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (not path.is_file() or path.is_symlink()):
        raise OSError("unsafe secrets file")
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    os.chmod(path, 0o600)
    if path.stat().st_mode & 0o077:
        raise OSError("secrets file permissions are not private")
