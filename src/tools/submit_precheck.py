"""Offline-characterizable submit precheck.

This module does not submit applications. The installed legacy entrypoint is
an operator-owned, ignored file; until post-merge adoption it remains unchanged.
All operator-specific data lives exclusively in precheck-data.json under the
precheck data root, never in tracked source.

Important: the historical ignored CLI's exact argument and event contract is
not available in the public repository. Adoption requires operator-side
compatibility verification before this template may replace that CLI.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


# These five buckets are intentionally empty in public source. The operator
# must create the ignored JSON from the existing local constants *before*
# installing the shim. An absent/malformed JSON file fails closed.
PUBLIC_DEFAULTS: dict[str, object] = {
    "company_caps": {},
    "company_aliases": {},
    "title_aliases": {},
    "historical_applications": [],
    "blocked_job_ids": [],
}
DEFAULT_COMPANY_CAP = 5
CAP_WINDOW = timedelta(days=180)
_APPLIED = frozenset(("applied", "applied_manual"))
_SUBMIT_EVIDENCE = frozenset((
    "submit_clicked", "submit_clicked_at", "submit_click_at",
    "submit_attempted_at", "submission_attempted_at", "submitted_at",
    "form_submitted_at", "application_submitted_at", "confirmation_id",
    "possible_submission", "submit_hold",
))
_PLACEHOLDER_TITLES = frozenset((
    "", "unknown", "untitled", "job", "not provided", "n a", "n/a",
))


class PrecheckUnavailable(Exception):
    """A missing dependency or invalid configuration must never mean allow."""


@dataclass(frozen=True)
class Policy:
    company_caps: dict[str, int]
    company_aliases: dict[str, str]
    title_aliases: dict[str, str]
    historical_applications: tuple[dict, ...]
    blocked_job_ids: frozenset[str]


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str


def _key(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", normalized)).strip()


def _company_key(value: object, policy: Policy) -> str:
    key = _key(value)
    return _key(policy.company_aliases.get(key, key))


def _title_key(value: object, policy: Policy) -> str:
    key = _key(value)
    return _key(policy.title_aliases.get(key, key))


def _root(legacy_dir: Path | None) -> Path:
    override = os.environ.get("APPLIEDIN_PRECHECK_DATA_DIR")
    if override is None:
        # The default must be anchored at the *invoked shim*, not this module.
        if legacy_dir is None:
            raise PrecheckUnavailable("legacy entrypoint location is required")
        root = Path(legacy_dir)
    else:
        if not override or not Path(override).is_absolute():
            raise PrecheckUnavailable("invalid precheck data directory")
        root = Path(override)
    if not root.is_dir() or root.is_symlink():
        raise PrecheckUnavailable("unusable precheck data directory")
    # A valid root must not hide file or lock effects through symlink children.
    for name in ("precheck.lock", "precheck-events.log", "precheck-data.json",
                 "owner-handoff.md", "claims"):
        child = root / name
        if child.is_symlink():
            raise PrecheckUnavailable("unsafe precheck file path")
        if name == "claims" and child.exists() and not child.is_dir():
            raise PrecheckUnavailable("invalid claims directory")
        if name != "claims" and child.exists() and not child.is_file():
            raise PrecheckUnavailable("invalid precheck file")
    return root


def load_policy(root: Path) -> Policy:
    path = root / "precheck-data.json"
    if not path.is_file() or path.is_symlink():
        raise PrecheckUnavailable("precheck configuration unavailable")
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PrecheckUnavailable("precheck configuration unreadable") from exc
    if not isinstance(obj, dict) or set(obj) != set(PUBLIC_DEFAULTS):
        raise PrecheckUnavailable("precheck configuration schema mismatch")
    caps = obj["company_caps"]
    company_aliases, title_aliases = obj["company_aliases"], obj["title_aliases"]
    history, blocked = obj["historical_applications"], obj["blocked_job_ids"]
    if not isinstance(caps, dict) or any(
        not isinstance(k, str) or type(v) is not int or v < 0
        for k, v in caps.items()
    ):
        raise PrecheckUnavailable("invalid company caps")
    if any(
        not isinstance(m, dict) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in m.items()
        )
        for m in (company_aliases, title_aliases)
    ):
        raise PrecheckUnavailable("invalid alias mapping")
    if not isinstance(history, list) or not all(isinstance(r, dict) for r in history):
        raise PrecheckUnavailable("invalid history")
    if not isinstance(blocked, list) or not all(isinstance(x, str) for x in blocked):
        raise PrecheckUnavailable("invalid job identifiers")
    return Policy(
        company_caps={_key(k): v for k, v in caps.items()},
        company_aliases={_key(k): v for k, v in company_aliases.items()},
        title_aliases={_key(k): v for k, v in title_aliases.items()},
        historical_applications=tuple(history),
        blocked_job_ids=frozenset(blocked),
    )


def _claim_path(root: Path, pk: str) -> Path:
    # Never let a job PK select its own filesystem path.
    name = hashlib.sha256(pk.encode("utf-8")).hexdigest() + ".json"
    return root / "claims" / name


def _read_claim(root: Path, pk: str) -> dict:
    path = _claim_path(root, pk)
    if path.is_symlink():
        raise PrecheckUnavailable("unsafe claim file")
    if not path.exists():
        return {}
    if not path.is_file():
        raise PrecheckUnavailable("invalid claim file")
    try:
        claim = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError, OSError) as exc:
        raise PrecheckUnavailable("unreadable claim file") from exc
    if not isinstance(claim, dict) or claim.get("pk") != pk:
        raise PrecheckUnavailable("claim identity mismatch")
    return claim


def _submit_evidence(row: dict) -> bool:
    return row.get("status") in _APPLIED or any(
        bool(row.get(field)) for field in _SUBMIT_EVIDENCE
    )


def _when(row: dict) -> datetime | None:
    for field in ("submit_clicked_at", "applied_at", "submitted_at",
                  "application_submitted_at"):
        value = row.get(field)
        if not isinstance(value, str) or not value:
            continue
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if result.tzinfo is not None:
            return result.astimezone(UTC)
    return None


def _handoff_blocks(root: Path) -> bool:
    path = root / "owner-handoff.md"
    if not path.exists():
        return False
    if not path.is_file() or path.is_symlink():
        raise PrecheckUnavailable("invalid handoff file")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PrecheckUnavailable("unreadable handoff file") from exc
    return not lines or lines[-1] not in ("BACK TO AGENTS", "DONE")


def evaluate(
    pk: str, current: dict | None, rows: Iterable[dict], policy: Policy,
    root: Path, *, at: datetime | None = None, board: str | None = None,
) -> Decision:
    """Pure decision logic except for reading the current disposable claim/handoff.

    A read failure is not a positive eligibility witness. The caller owns the
    fcntl critical section, tracking read, and event append.
    """
    at = (at or datetime.now(UTC)).astimezone(UTC)
    if not isinstance(current, dict) or current.get("pk") != pk:
        return Decision(False, "row_missing")
    if current.get("status") == "skipped":
        return Decision(False, "skipped")
    if _submit_evidence(current):
        return Decision(False, "already_submitted")
    if current.get("status") not in ("tailored", "submitting", "needs_human"):
        return Decision(False, "status_blocked")
    if _handoff_blocks(root):
        return Decision(False, "owner_handoff")
    claim = _read_claim(root, pk)
    if claim.get("submit_clicked"):
        return Decision(False, "submit_once")
    if board is not None and board != str(
        current.get("greenhouse_board") or current.get("board") or ""
    ):
        return Decision(False, "board_pin")

    job_id = current.get("job_id")
    company = _company_key(current.get("company"), policy)
    title = _title_key(current.get("title"), policy)
    if not isinstance(job_id, str) or not job_id or not company:
        return Decision(False, "identity_missing")
    if job_id in policy.blocked_job_ids:
        return Decision(False, "blocked_job")
    others = []
    for row in (*rows, *policy.historical_applications):
        if not isinstance(row, dict) or row.get("pk") == pk:
            continue
        other = dict(row)
        other_pk = other.get("pk")
        if isinstance(other_pk, str) and other_pk:
            past_claim = _read_claim(root, other_pk)
            if past_claim.get("submit_clicked"):
                # Account for an unconfirmed click without inventing an
                # applied status or mutating the tracking row.
                other["submit_clicked"] = True
                other["submit_clicked_at"] = past_claim.get("submit_clicked_at")
        others.append(other)
    for other in others:
        if not _submit_evidence(other):
            continue
        if other.get("job_id") == job_id:
            return Decision(False, "duplicate_job_id")
        if (title and title not in _PLACEHOLDER_TITLES
                and title == _title_key(other.get("title"), policy)
                and company == _company_key(other.get("company"), policy)):
            return Decision(False, "duplicate_title_company")

    # An applied record with an unknown timestamp is counted rather than
    # interpreted as expired: missing evidence must not loosen a submit cap.
    prior = sum(
        1 for row in others
        if _submit_evidence(row) and _company_key(row.get("company"), policy) == company
        and (_when(row) is None or _when(row) >= at - CAP_WINDOW)
    )
    cap = policy.company_caps.get(company, DEFAULT_COMPANY_CAP)
    if prior >= cap:
        return Decision(False, "company_cap")
    return Decision(True, "eligible")


@contextmanager
def _serialized(root: Path):
    path = root / "precheck.lock"
    with path.open("a+b") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _append_event(root: Path, *, pk: str, operation: str,
                  decision: Decision, at: datetime) -> None:
    event = {
        "at": at.astimezone(UTC).isoformat(), "pk": pk,
        "operation": operation, "allow": decision.allow,
        "reason": decision.reason,
    }
    with (root / "precheck-events.log").open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _record_click(root: Path, pk: str, at: datetime) -> None:
    target = _claim_path(root, pk)
    target.parent.mkdir(exist_ok=True)
    # Write the fact of the click, never an unconfirmed applied status.
    data = {"pk": pk, "submit_clicked": True,
            "submit_clicked_at": at.astimezone(UTC).isoformat()}
    temporary = target.with_suffix(".tmp")
    if temporary.is_symlink():
        raise PrecheckUnavailable("unsafe temporary claim path")
    temporary.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def main(
    argv: list[str] | None = None, *, legacy_dir: Path | None = None,
    tracking: Any = None, at: datetime | None = None,
) -> int:
    """Fail-closed CLI. Exact legacy argv equivalence still needs local signoff."""
    parser = argparse.ArgumentParser(prog="precheck")
    parser.add_argument("operation", choices=("check", "record-click"))
    parser.add_argument("pk")
    args = parser.parse_args(argv)
    now = (at or datetime.now(UTC)).astimezone(UTC)
    try:
        root = _root(legacy_dir)
        policy = load_policy(root)
        if tracking is None:
            from core.stores import make_stores
            tracking = make_stores().tracking
        with _serialized(root):
            current = tracking.get(args.pk)
            decision = evaluate(
                args.pk, current, tracking.all(), policy, root,
                at=now, board=os.environ.get("BOARD_AGENT_BOARD"),
            )
            if decision.allow and args.operation == "record-click":
                _record_click(root, args.pk, now)
            _append_event(
                root, pk=args.pk, operation=args.operation,
                decision=decision, at=now,
            )
    except (PrecheckUnavailable, OSError, ValueError, RuntimeError, TypeError):
        print("STOP: precheck unavailable", file=sys.stderr)
        return 2
    if not decision.allow:
        print("STOP: " + decision.reason)
        return 2
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
