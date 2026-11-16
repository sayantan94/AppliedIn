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
        return (_generation(account.get("uidvalidity")) == _generation(validity)
                and f"{validity}:{uid}" in account.get("processed", []))


def _set_first_floor(path: Path, key: str, validity: str, selected: list[int]) -> None:
    with WRITE_LOCK:
        state = _read_state(path)
        account = state.get("accounts", {}).get(key)
        if not account or _generation(account.get("uidvalidity")) != _generation(validity):
            return
        if account.get("uid_floor") is None:
            floor = min(selected) - 1 if selected else 0
            account["uid_floor"] = floor
            account["last_uid"] = max(int(account.get("last_uid", 0)), floor)
            # Mark the entire admitted first window before attempting any UID:
            # a partial scan must not lose an unvisited UID if SEARCH later omits it.
            account["unresolved_uids"] = sorted(set(account.get("unresolved_uids", [])) | set(selected))
            _write_state(path, state)


def _mark_unresolved(path: Path, key: str, validity: str, uid: int) -> None:
    with WRITE_LOCK:
        state = _read_state(path)
        account = state.get("accounts", {}).get(key)
        if not account or _generation(account.get("uidvalidity")) != _generation(validity):
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
    if _generation(validity) is None or not isinstance(uid, int) or uid <= 0:
        return False
    with WRITE_LOCK:
        state = _read_state(path)
        account = state.get("accounts", {}).get(key)
        if not account or _generation(account.get("uidvalidity")) != _generation(validity):
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
        if not account or _generation(account.get("uidvalidity")) != _generation(validity):
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


def _classify(fields: dict, fragment: str, classifier) -> str:
    from core.reply_classifier import validated_label
    if _security_code_subject(fields["subject"]):
        return "other"
    if not fragment.strip() or classifier is None:
        return "unclassified"
    try:
        return validated_label(classifier(fields, fragment))
    except Exception:
        return "unclassified"


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
            _set_first_floor(path, key, validity, uids)
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
            if _security_code_subject(fields["subject"]):
                label = "other"
            else:
                try:
                    prefix = client.fetch_text_prefix(uid)
                except Exception:
                    prefix = b""
                decoded = _decode_text_prefix(raw, prefix[:4096]) if isinstance(prefix, bytes) and prefix else ""
                if not decoded.strip() and not _valid_message_id(fields["message_id"]):
                    _mark_unresolved(path, key, validity, uid)
                    continue
                label = _classify(fields, decoded, classifier)
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
                if not decoded.strip():
                    continue
                label = _classify(fields, decoded, classifier)
                _promote_label(path, key, validity, record_id, uid, expected_mid, label)
            except Exception:
                # Mail/provider errors are never logged or persisted as metadata.
                continue
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
