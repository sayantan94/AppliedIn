"""Offline, synthetic submit-precheck contract characterization.

These assertions pin explicit decisions, CLI states and per-invocation events.
The ignored historical CLI is not part of this repository: passing these tests
alone does NOT prove pre-move equivalence, which requires an external oracle.
No browser, real Submit, live Redis or owner file is used.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import fakeredis
import pytest

from core.storage.local import RedisTracking
from tools import submit_precheck as guard


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
BOARD = "example-board"
PK = "example-co#candidate"
JOB = {
    "pk": PK, "job_id": "candidate", "company": "example-co",
    "title": "Synthetic Engineer", "status": "tailored",
    "greenhouse_board": BOARD,
}


def _events(root: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (root / "precheck-events.log").read_text(encoding="utf-8").splitlines()
    ]


def _claim(root: Path, pk: str = PK) -> dict | None:
    name = hashlib.sha256(pk.encode("utf-8")).hexdigest() + ".json"
    path = root / "claims" / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    policy = json.loads(json.dumps(guard.PUBLIC_DEFAULTS))
    (root / "precheck-data.json").write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("APPLIEDIN_PRECHECK_DATA_DIR", str(root))
    monkeypatch.setenv("BOARD_AGENT_PID", "4242")
    monkeypatch.setenv("BOARD_AGENT_BOARD", BOARD)
    client = fakeredis.FakeRedis(decode_responses=True)
    tracking = RedisTracking(client)

    def seed(*rows):
        for row in rows:
            client.set("app:" + row["pk"], json.dumps(row))

    def save():
        (root / "precheck-data.json").write_text(
            json.dumps(policy), encoding="utf-8"
        )

    def invoke(op="start", pk=PK, board=BOARD):
        return guard.main(
            [op, pk, board], legacy_dir=tmp_path / "not-an-operator",
            tracking=tracking, at=NOW,
        )

    seed(JOB)
    return SimpleNamespace(
        root=root, policy=policy, tracking=tracking, seed=seed,
        save=save, invoke=invoke, tmp=tmp_path,
    )


def test_five_cli_operations_and_distinct_claim_states(world, capsys):
    w = world
    assert w.invoke("start") == 0
    claim = _claim(w.root)
    assert claim == {
        "pk": PK, "board": BOARD, "agent_pid": "4242", "state": "filling",
        "started_at": NOW.isoformat(), "submit_clicked": False,
    }
    assert w.invoke("submit") == 0
    assert _claim(w.root) == {
        **claim, "state": "submit-clicked", "submit_clicked": True,
        "submit_clicked_at": NOW.isoformat(),
    }
    assert w.invoke("done") == 0
    assert _claim(w.root)["state"] == "recorded"
    assert _claim(w.root)["recorded_at"] == NOW.isoformat()
    assert w.tracking.get(PK)["status"] == "tailored"
    assert capsys.readouterr().out == "OK\nOK\nOK\n"
    assert [(event["operation"], event["prior_claim_state"]) for event in
            _events(w.root)] == [
        ("start", None), ("submit", "filling"), ("done", "submit-clicked"),
    ]


def test_exact_per_invocation_event_shape_and_order(world, capsys):
    w = world
    assert w.invoke("start") == 0
    output = capsys.readouterr()
    assert (output.out, output.err) == ("OK\n", "")
    assert _events(w.root) == [{
        "at": NOW.isoformat(), "pk": PK, "operation": "start",
        "argv": ["start", PK, BOARD], "return_code": 0, "output": "OK\n",
        "prior_claim_state": None, "agent_pid": "4242",
        "allow": True, "reason": "eligible",
    }]
    assert w.invoke("start") == 1
    output = capsys.readouterr()
    assert output.out == "STOP: claim_exists\n"
    assert _events(w.root)[1] == {
        "at": NOW.isoformat(), "pk": PK, "operation": "start",
        "argv": ["start", PK, BOARD], "return_code": 1,
        "output": "STOP: claim_exists\n", "prior_claim_state": "filling",
        "agent_pid": "4242", "allow": False, "reason": "claim_exists",
    }


def test_release_removes_only_matching_filling_claim(world, monkeypatch):
    w = world
    assert w.invoke("release") == 1
    assert w.invoke("start") == 0
    assert w.invoke("release", board="another-board") == 1
    assert _claim(w.root)["state"] == "filling"
    assert w.invoke("release") == 0
    assert _claim(w.root) is None
    assert w.invoke("start") == 0
    assert _claim(w.root)["state"] == "filling"


def test_release_does_not_remove_submit_clicked_or_recorded(world):
    w = world
    assert w.invoke("start") == 0
    assert w.invoke("submit") == 0
    assert w.invoke("release") == 1
    assert _claim(w.root)["state"] == "submit-clicked"
    assert w.invoke("done") == 0
    assert w.invoke("release") == 1
    assert w.invoke("done") == 1
    assert _claim(w.root)["state"] == "recorded"


def test_reopen_only_from_clicked_by_claiming_agent(world, monkeypatch):
    w = world
    assert w.invoke("reopen") == 1
    assert w.invoke("start") == 0
    assert w.invoke("reopen") == 1
    assert w.invoke("submit") == 0
    monkeypatch.setenv("BOARD_AGENT_PID", "9999")
    assert w.invoke("reopen") == 1
    monkeypatch.setenv("BOARD_AGENT_PID", "4242")
    assert w.invoke("reopen", board="another-board") == 1
    assert _claim(w.root)["state"] == "submit-clicked"
    assert w.invoke("reopen") == 0
    assert _claim(w.root)["state"] == "filling"
    assert not _claim(w.root)["submit_clicked"]
    assert "submit_clicked_at" not in _claim(w.root)
    assert w.invoke("submit") == 0
    assert _claim(w.root)["state"] == "submit-clicked"


def test_board_and_pid_identity_pins_all_transitions(world, monkeypatch, capsys):
    w = world
    assert w.invoke("start", board="wrong-board") == 1
    assert capsys.readouterr().out == "STOP: board_pin\n"
    assert w.invoke("start") == 0
    monkeypatch.setenv("BOARD_AGENT_PID", "another-pid")
    assert w.invoke("submit") == 1
    assert w.invoke("release") == 1
    assert _claim(w.root)["agent_pid"] == "4242"
    monkeypatch.setenv("BOARD_AGENT_PID", "4242")
    assert w.invoke("submit") == 0


def test_submit_rechecks_status_and_eligibility_immediately(world, capsys):
    w = world
    assert w.invoke("start") == 0
    w.seed({**JOB, "status": "skipped"})
    assert w.invoke("submit") == 1
    assert capsys.readouterr().out.endswith("STOP: skipped\n")
    assert _claim(w.root)["state"] == "filling"
    w.seed(JOB)
    w.seed({
        "pk": "other-co#candidate", "job_id": "candidate",
        "company": "other-co", "title": "Different role",
        "status": "applied", "applied_at": NOW.isoformat(),
    })
    assert w.invoke("submit") == 1
    assert _events(w.root)[-1]["reason"] == "duplicate_job_id"
    assert _claim(w.root)["state"] == "filling"


def test_submitted_click_counts_but_does_not_mark_applied(world):
    w = world
    assert w.invoke("start") == 0
    assert w.invoke("submit") == 0
    assert w.tracking.get(PK)["status"] == "tailored"
    assert w.invoke("start") == 1
    w.seed({
        "pk": "example-co#next", "job_id": "next", "company": "example-co",
        "title": JOB["title"], "status": "tailored", "greenhouse_board": BOARD,
    })
    assert w.invoke("start", pk="example-co#next") == 1
    assert _events(w.root)[-1]["reason"] == "duplicate_title_company"


def test_duplicate_job_id_despite_different_title_and_company(world):
    w = world
    w.seed({
        "pk": "another-co#old", "job_id": "candidate",
        "company": "another-co", "title": "Different role",
        "status": "applied", "applied_at": NOW.isoformat(),
    })
    assert w.invoke() == 1
    assert _events(w.root)[-1]["reason"] == "duplicate_job_id"


def test_normalized_title_company_and_distinct_companies(world):
    w = world
    w.seed({
        "pk": "example-co#old", "job_id": "old",
        "company": "Example Co", "title": "  SYNTHETIC--Engineer!  ",
        "status": "applied_manual", "applied_at": NOW.isoformat(),
    })
    assert w.invoke() == 1
    assert _events(w.root)[-1]["reason"] == "duplicate_title_company"
    w.seed({
        "pk": "example-co#old", "job_id": "old",
        "company": "another-co", "title": "Synthetic Engineer",
        "status": "applied", "applied_at": NOW.isoformat(),
    })
    assert w.invoke() == 0


def test_placeholder_title_not_a_wildcard(world):
    w = world
    w.seed({**JOB, "title": "Unknown"})
    w.seed({
        "pk": "example-co#old", "job_id": "old", "company": "example-co",
        "title": "UNKNOWN", "status": "applied", "applied_at": NOW.isoformat(),
    })
    assert w.invoke() == 0


def test_never_and_parked_row_identifiers(world):
    w = world
    w.policy["NEVER"] = [PK]
    w.save()
    assert w.invoke() == 1
    assert _events(w.root)[-1]["reason"] == "blocked_row"
    w.policy["NEVER"] = []
    w.policy["PARKED"] = ["example-co#"]
    w.save()
    assert w.invoke() == 1
    assert _events(w.root)[-1]["reason"] == "parked"


def test_explicit_distinct_pair_exception_exempts_duplicate_not_cap(world):
    w = world
    other = {
        "pk": "example-co#old", "job_id": "candidate",
        "company": "example-co", "title": JOB["title"],
        "status": "applied", "applied_at": NOW.isoformat(),
    }
    w.seed(other)
    assert w.invoke() == 1
    w.policy["DISTINCT_PAIRS"] = [[PK, other["pk"]]]
    w.save()
    assert w.invoke() == 0
    assert _claim(w.root)["state"] == "filling"
    assert w.invoke("release") == 0
    w.policy["COUNT_CAPS"] = {"example-co": 1}
    w.save()
    assert w.invoke() == 1
    assert _events(w.root)[-1]["reason"] == "company_cap"


def test_company_date_cap(world):
    w = world
    w.policy["CAPS"] = {"example-co": "2026-01-01"}
    w.save()
    assert w.invoke() == 1
    assert _events(w.root)[-1]["reason"] == "company_date_cap"
    w.policy["CAPS"] = {"example-co": "2025-12-31"}
    w.save()
    assert w.invoke() == 0


def test_count_cap_fifth_sixth_and_180_day_expiry(world):
    w = world
    priors = [
        {
            "pk": f"example-co#prior-{i}", "job_id": f"prior-{i}",
            "company": "example-co", "title": f"Synthetic role {i}",
            "status": "applied",
            "applied_at": (NOW - timedelta(days=30)).isoformat(),
        }
        for i in range(5)
    ]
    expired = {
        **priors[4], "applied_at": (NOW - timedelta(days=180, seconds=1)).isoformat(),
    }
    w.seed(*priors[:4], expired)
    assert w.invoke() == 0  # Four qualifying priors and the fifth candidate.
    assert w.invoke("release") == 0
    w.seed(priors[4])
    assert w.invoke() == 1  # Five qualifying priors and the sixth candidate.
    assert _events(w.root)[-1]["reason"] == "company_cap"


@pytest.mark.parametrize(
    ("handoff", "allowed"),
    [
        ("", True),
        ("Active request", True),
        ("other-co#unrelated active\n", True),
        (f"{PK} active\n", False),
        (f"{PK} BACK TO AGENTS\nstill active\n", True),
        (f"{PK} BACK TO AGENTS\n", True),
        (f"{PK} DONE\n", True),
        (f"{PK} DONE\n{PK} active\n", False),
    ],
)
def test_handoff_holds_only_the_named_row(world, handoff, allowed):
    w = world
    (w.root / "owner-handoff.md").write_text(handoff, encoding="utf-8")
    assert (w.invoke() == 0) is allowed


def test_skipped_and_unconfirmed_submit_row_guard(world):
    w = world
    w.seed({**JOB, "status": "skipped"})
    assert w.invoke() == 1
    assert _events(w.root)[-1]["reason"] == "skipped"
    w.seed({**JOB, "status": "needs_human", "submit_clicked_at": NOW.isoformat()})
    assert w.invoke() == 1
    assert _events(w.root)[-1]["reason"] == "already_submitted"


def test_submission_without_claim_fails_even_when_row_eligible(world):
    w = world
    assert w.invoke("submit") == 1
    assert _claim(w.root) is None
    assert _events(w.root)[-1]["reason"] == "claim_mismatch"


@pytest.mark.parametrize("args", [[], ["start"], ["bogus", PK, BOARD],
                                      ["start", PK, BOARD, "extra"]])
def test_invalid_arguments_record_original_argv_and_return_code(world, args, capsys):
    w = world
    rc = guard.main(args, legacy_dir=w.tmp, tracking=w.tracking, at=NOW)
    assert rc == 2
    output = capsys.readouterr()
    assert output.out.startswith("usage: precheck ")
    assert output.err == ""
    event = _events(w.root)[-1]
    assert event["argv"] == args
    assert event["return_code"] == 2
    assert event["output"] == output.out
    assert event["reason"] == "invalid_arguments"
    assert event["prior_claim_state"] is None


def test_help_is_logged_and_does_not_create_claim(world, capsys):
    w = world
    assert guard.main(["--help"], legacy_dir=w.tmp, tracking=w.tracking, at=NOW) == 2
    assert "usage: precheck" in capsys.readouterr().out
    assert _claim(w.root) is None
    assert _events(w.root)[-1]["argv"] == ["--help"]


def test_empty_or_invalid_json_refuses_closed(world, capsys):
    w = world
    (w.root / "precheck-data.json").unlink()
    assert w.invoke() == 2
    assert "OK" not in capsys.readouterr().out
    assert _claim(w.root) is None
    w.policy["COUNT_CAPS"] = {"example-co": -1}
    w.save()
    assert w.invoke() == 2
    assert _events(w.root)[-1]["reason"] == "precheck_unavailable"


@pytest.mark.parametrize("value", ["", "relative", "/not-a-real-precheck-root"])
def test_invalid_root_has_no_lock_or_event_effect(world, monkeypatch, capsys, value):
    w = world
    monkeypatch.setenv("APPLIEDIN_PRECHECK_DATA_DIR", value)
    assert w.invoke() != 0
    assert "OK" not in capsys.readouterr().out
    assert not (w.root / "precheck.lock").exists()
    assert not (w.root / "precheck-events.log").exists()
    assert _claim(w.root) is None


def test_symlinked_lock_and_log_cannot_escape(world, tmp_path):
    w = world
    outside = tmp_path / "outside"
    outside.write_text("sentinel", encoding="utf-8")
    (w.root / "precheck.lock").symlink_to(outside)
    assert w.invoke() == 2
    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert not (w.root / "precheck-events.log").exists()


def test_unset_override_anchors_at_disposable_installed_legacy_dir(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.delenv("APPLIEDIN_PRECHECK_DATA_DIR", raising=False)
    operator = tmp_path / "disposable" / ".local" / "operator"
    operator.mkdir(parents=True)
    (operator / "precheck-data.json").write_text(
        json.dumps(guard.PUBLIC_DEFAULTS), encoding="utf-8",
    )
    monkeypatch.setenv("BOARD_AGENT_PID", "4242")
    monkeypatch.setenv("BOARD_AGENT_BOARD", BOARD)
    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    tracking.r.set("app:" + PK, json.dumps(JOB))
    assert guard.main(
        ["start", PK, BOARD], legacy_dir=operator, tracking=tracking, at=NOW,
    ) == 0
    assert capsys.readouterr().out == "OK\n"
    assert (operator / "precheck.lock").is_file()
    assert _claim(operator)["state"] == "filling"
    assert not (tmp_path / "precheck.lock").exists()


def test_sequential_and_concurrent_start_are_serialized_in_one_process(world):
    w = world
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: w.invoke("start"), range(2)))
    assert sorted(results) == [0, 1]
    assert _claim(w.root)["state"] == "filling"
    events = _events(w.root)
    assert len(events) == 2
    assert sorted(e["reason"] for e in events) == ["claim_exists", "eligible"]
    assert (w.root / "precheck.lock").is_file()


def test_concurrent_submit_records_one_click_transition(world):
    w = world
    assert w.invoke("start") == 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: w.invoke("submit"), range(2)))
    assert sorted(results) == [0, 1]
    assert _claim(w.root)["state"] == "submit-clicked"
    events = _events(w.root)
    assert len(events) == 3
    assert sorted(e["reason"] for e in events[1:]) == [
        "claim_mismatch", "eligible",
    ]


def test_installed_equivalent_shim_from_foreign_cwd_no_checkout_injection(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    installed_tree = tmp_path / "disposable-tree"
    installed = installed_tree / ".local" / "operator" / "precheck.py"
    installed.parent.mkdir(parents=True)
    copied_module = installed_tree / "src" / "tools" / "submit_precheck.py"
    copied_module.parent.mkdir(parents=True)
    shutil.copyfile(repo / "scripts" / "submit_precheck_shim.py", installed)
    shutil.copyfile(repo / "src" / "tools" / "submit_precheck.py", copied_module)
    root = tmp_path / "synthetic-data"
    root.mkdir()
    (root / "precheck-data.json").write_text(
        json.dumps(guard.PUBLIC_DEFAULTS), encoding="utf-8",
    )
    foreign = tmp_path / "foreign-cwd"
    foreign.mkdir()
    child = r"""
import runpy
import sys
from pathlib import Path
repo, script, expected = [Path(arg).resolve() for arg in sys.argv[1:]]
# A package's editable .pth is not proof that the installed shim resolves the
# local copy. Keep the existing offline sitecustomize startup guard active.
sys.path[:] = [
    entry for entry in sys.path
    if Path(entry or ".").resolve() not in (repo, repo / "src")
]
assert str(repo) not in sys.path
assert str(repo / "src") not in sys.path
sys.argv = [str(script), "--help"]
try:
    runpy.run_path(str(script), run_name="__main__")
except SystemExit as exc:
    assert exc.code == 2
else:
    raise AssertionError("shim did not terminate")
assert Path(sys.modules["tools.submit_precheck"].__file__).resolve() == expected
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    env["APPLIEDIN_PRECHECK_DATA_DIR"] = str(root)
    run = subprocess.run(
        [sys.executable, "-c", child, str(repo), str(installed), str(copied_module)],
        cwd=foreign, env=env, text=True, capture_output=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert "usage: precheck" in run.stdout
    assert len(_events(root)) == 1
    assert _events(root)[0]["argv"] == ["--help"]
