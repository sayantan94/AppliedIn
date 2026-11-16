"""Source-owned pre-Submit guard and historical claim transitions.

No application Submit click occurs here. Operator-specific policy values are
loaded from the ignored precheck-data.json file, never public source.
"""

from __future__ import annotations

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


# Values are never embedded in public code. Keep the historical five categories.
PUBLIC_DEFAULTS: dict[str, object] = {
    "CAPS": {},
    "COUNT_CAPS": {},
    "NEVER": [],
    "PARKED": [],
    "DISTINCT_PAIRS": [],
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
    CAPS: dict[str, datetime]
    COUNT_CAPS: dict[str, int]
    NEVER: frozenset[str]
    PARKED: tuple[str, ...]
    DISTINCT_PAIRS: frozenset[frozenset[str]]


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
    return _key(value)


def _title_key(value: object, policy: Policy) -> str:
    return _key(value)


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
    if not root.is_dir() or any(p.is_symlink() for p in (root, *root.parents)):
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
    caps, count_caps = obj["CAPS"], obj["COUNT_CAPS"]
    never, parked, pairs = obj["NEVER"], obj["PARKED"], obj["DISTINCT_PAIRS"]
    if not isinstance(caps, dict) or any(
        not isinstance(k, str) or not isinstance(v, str)
        for k, v in caps.items()
    ):
        raise PrecheckUnavailable("invalid date caps")
    dates = {}
    try:
        for company, value in caps.items():
            date = datetime.fromisoformat(value)
            if date.tzinfo is not None or date.time().isoformat() != "00:00:00":
                raise ValueError("date cap must be a calendar date")
            dates[_key(company)] = date.replace(tzinfo=UTC)
    except ValueError as exc:
        raise PrecheckUnavailable("invalid date cap") from exc
    if not isinstance(count_caps, dict) or any(
        not isinstance(k, str) or type(v) is not int or v < 0
        for k, v in count_caps.items()
    ):
        raise PrecheckUnavailable("invalid count caps")
    if not isinstance(never, list) or not all(isinstance(v, str) and v for v in never):
        raise PrecheckUnavailable("invalid never identifiers")
    if not isinstance(parked, list) or not all(isinstance(v, str) and v for v in parked):
        raise PrecheckUnavailable("invalid parked identifiers")
    if not isinstance(pairs, list) or any(
        not isinstance(pair, list) or len(pair) != 2
        or not all(isinstance(v, str) and v for v in pair)
        or pair[0] == pair[1]
        for pair in pairs
    ):
        raise PrecheckUnavailable("invalid distinct pair exceptions")
    return Policy(
        CAPS=dates,
        COUNT_CAPS={_key(k): v for k, v in count_caps.items()},
        NEVER=frozenset(never),
        PARKED=tuple(parked),
        DISTINCT_PAIRS=frozenset(frozenset(pair) for pair in pairs),
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


def _handoff_blocks(root: Path, pk: str) -> bool:
    # The legacy guard only holds a row that a handoff line actually names; the
    # last such line must release it with BACK TO AGENTS or DONE. A whole-file
    # last line would let an unrelated owner row block or release every row.
    path = root / "owner-handoff.md"
    if not path.exists():
        return False
    if not path.is_file() or path.is_symlink():
        raise PrecheckUnavailable("invalid handoff file")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PrecheckUnavailable("unreadable handoff file") from exc
    named = re.compile(re.escape(pk) + r"(?![0-9a-z])")
    lines = [line for line in text.splitlines() if named.search(line)]
    if not lines:
        return False
    return not re.search(r"\bBACK TO AGENTS\b|\bDONE\b", lines[-1].upper())


def evaluate(
    pk: str, current: dict | None, rows: Iterable[dict], policy: Policy,
    root: Path, *, at: datetime | None = None, board: str | None = None,
) -> Decision:
    """Read-only guard. Called under the existing per-root fcntl lock."""
    at = (at or datetime.now(UTC)).astimezone(UTC)
    if not isinstance(current, dict) or current.get("pk") != pk:
        return Decision(False, "row_missing")
    if current.get("status") == "skipped":
        return Decision(False, "skipped")
    if _submit_evidence(current):
        return Decision(False, "already_submitted")
    if current.get("status") not in ("tailored", "submitting", "needs_human"):
        return Decision(False, "status_blocked")
    if _handoff_blocks(root, pk):
        return Decision(False, "owner_handoff")
    claim = _read_claim(root, pk)
    if claim.get("submit_clicked") or claim.get("state") in ("submit-clicked", "recorded"):
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
    if pk in policy.NEVER:
        return Decision(False, "blocked_row")
    if any(pk == token or pk.startswith(token) for token in policy.PARKED):
        return Decision(False, "parked")
    if company in policy.CAPS and at.date() <= policy.CAPS[company].date():
        return Decision(False, "company_date_cap")
    others = []
    for row in rows:
        if not isinstance(row, dict) or row.get("pk") == pk:
            continue
        other = dict(row)
        other_pk = other.get("pk")
        if isinstance(other_pk, str) and other_pk:
            past_claim = _read_claim(root, other_pk)
            if past_claim.get("submit_clicked") or past_claim.get("state") in (
                "submit-clicked", "recorded"
            ):
                other["submit_clicked"] = True
                other["submit_clicked_at"] = past_claim.get("submit_clicked_at")
        others.append(other)
    for other in others:
        if not _submit_evidence(other):
            continue
        pair = other.get("pk")
        if isinstance(pair, str) and frozenset((pk, pair)) in policy.DISTINCT_PAIRS:
            continue
        if other.get("job_id") == job_id:
            return Decision(False, "duplicate_job_id")
        if (title and title not in _PLACEHOLDER_TITLES
                and title == _title_key(other.get("title"), policy)
                and company == _company_key(other.get("company"), policy)):
            return Decision(False, "duplicate_title_company")

    prior = sum(
        1 for row in others
        if _submit_evidence(row) and _company_key(row.get("company"), policy) == company
        and (_when(row) is None or _when(row) >= at - CAP_WINDOW)
    )
    cap = policy.COUNT_CAPS.get(company, DEFAULT_COMPANY_CAP)
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


def _append_event(
    root: Path, *, pk: str | None, operation: str | None, at: datetime,
    argv: list[str], return_code: int, output: str, reason: str,
    prior_claim_state: str | None, agent_pid: str,
) -> None:
    event = {
        "at": at.astimezone(UTC).isoformat(),
        "pk": pk, "operation": operation, "argv": argv,
        "return_code": return_code, "output": output,
        "prior_claim_state": prior_claim_state, "agent_pid": agent_pid,
        "allow": return_code == 0 and operation in _OPERATIONS,
        "reason": reason,
    }
    target = root / "precheck-events.log"
    if target.is_symlink():
        raise PrecheckUnavailable("unsafe event log")
    with target.open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _write_claim(root: Path, pk: str, claim: dict) -> None:
    target = _claim_path(root, pk)
    target.parent.mkdir(exist_ok=True)
    if target.is_symlink():
        raise PrecheckUnavailable("unsafe claim file")
    temporary = target.with_suffix(".tmp")
    if temporary.is_symlink():
        raise PrecheckUnavailable("unsafe temporary claim path")
    temporary.write_text(
        json.dumps(claim, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)


def _agent_pid() -> str:
    # A stable board-agent PID may be supplied across separate CLI invocations.
    return os.environ.get("BOARD_AGENT_PID") or str(os.getppid())


def _matches_claim(claim: dict, pk: str, board: str, pid: str, state: str) -> bool:
    return bool(
        claim.get("pk") == pk and claim.get("board") == board
        and claim.get("state") == state
        and str(claim.get("agent_pid")) == pid
    )


_OPERATIONS = ("start", "submit", "done", "release", "reopen")
_USAGE = "usage: precheck {start,submit,done,release,reopen} pk board\n"


def main(
    argv: list[str] | None = None, *, legacy_dir: Path | None = None,
    tracking: Any = None, at: datetime | None = None,
) -> int:
    """Never click Submit; record only a permitted claim transition."""
    raw = list(sys.argv[1:] if argv is None else argv)
    now = (at or datetime.now(UTC)).astimezone(UTC)
    op = raw[0] if raw else None
    pk = raw[1] if len(raw) > 1 else None
    board = raw[2] if len(raw) > 2 else None
    pid = _agent_pid()
    valid = len(raw) == 3 and op in _OPERATIONS and bool(pk) and bool(board)
    try:
        root = _root(legacy_dir)
        with _serialized(root):
            prior = _read_claim(root, pk) if pk else {}
            prior_state = prior.get("state")
            if not valid:
                reason, output, code = "invalid_arguments", _USAGE, 2
            elif os.environ.get("BOARD_AGENT_BOARD") not in (None, board):
                reason, output, code = "board_pin", "STOP: board_pin\n", 1
            else:
                try:
                    if op in ("start", "submit"):
                        policy = load_policy(root)
                        if tracking is None:
                            from core.stores import make_stores
                            tracking = make_stores().tracking
                    if op == "start":
                        if prior:
                            decision = Decision(False, "claim_exists")
                        else:
                            current = tracking.get(pk)
                            decision = evaluate(
                                pk, current, tracking.all(), policy, root,
                                at=now, board=board,
                            )
                        if decision.allow:
                            _write_claim(root, pk, {
                                "pk": pk, "board": board, "agent_pid": pid,
                                "state": "filling", "started_at": now.isoformat(),
                                "submit_clicked": False,
                            })
                    elif op == "submit":
                        if not _matches_claim(prior, pk, board, pid, "filling"):
                            decision = Decision(False, "claim_mismatch")
                        else:
                            # The eligibility check happens after claim validation,
                            # immediately before recording the external click intent.
                            current = tracking.get(pk)
                            decision = evaluate(
                                pk, current, tracking.all(), policy, root,
                                at=now, board=board,
                            )
                            if decision.allow:
                                next_claim = {
                                    **prior, "state": "submit-clicked",
                                    "submit_clicked": True,
                                    "submit_clicked_at": now.isoformat(),
                                }
                                _write_claim(root, pk, next_claim)
                    elif op == "done":
                        decision = Decision(
                            _matches_claim(prior, pk, board, pid, "submit-clicked"),
                            "recorded" if _matches_claim(
                                prior, pk, board, pid, "submit-clicked"
                            ) else "claim_mismatch",
                        )
                        if decision.allow:
                            _write_claim(root, pk, {
                                **prior, "state": "recorded",
                                "recorded_at": now.isoformat(),
                            })
                    elif op == "release":
                        decision = Decision(
                            _matches_claim(prior, pk, board, pid, "filling"),
                            "released" if _matches_claim(
                                prior, pk, board, pid, "filling"
                            ) else "claim_mismatch",
                        )
                        if decision.allow:
                            _claim_path(root, pk).unlink()
                    else:
                        # Only the claiming agent/board can reopen; no newly
                        # invented approval flag or tracking status is inferred.
                        decision = Decision(
                            _matches_claim(prior, pk, board, pid, "submit-clicked"),
                            "reopened" if _matches_claim(
                                prior, pk, board, pid, "submit-clicked"
                            ) else "claim_mismatch",
                        )
                        if decision.allow:
                            next_claim = {**prior, "state": "filling",
                                          "submit_clicked": False}
                            next_claim.pop("submit_clicked_at", None)
                            _write_claim(root, pk, next_claim)
                    reason = decision.reason
                    # The legacy CLI returns 1 for a guard STOP; 2 stays the
                    # usage/precheck-unavailable convention.
                    code = 0 if decision.allow else 1
                    output = "OK\n" if decision.allow else "STOP: " + reason + "\n"
                except (PrecheckUnavailable, OSError, ValueError, RuntimeError, TypeError):
                    reason, output, code = (
                        "precheck_unavailable", "STOP: precheck unavailable\n", 2,
                    )
            _append_event(
                root, pk=pk, operation=op, at=now, argv=raw,
                return_code=code, output=output, reason=reason,
                prior_claim_state=prior_state, agent_pid=pid,
            )
    except (PrecheckUnavailable, OSError, ValueError, RuntimeError, TypeError):
        print("STOP: precheck unavailable", file=sys.stderr)
        return 2
    print(output, end="", file=sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
