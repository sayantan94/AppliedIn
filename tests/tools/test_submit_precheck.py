"""Synthetic offline characterization of the source-owned submit precheck.

The ignored historical CLI is not available to public GitHub workers. These
are source contract tests, not a claim that the old private CLI's argv or exact
events have been compared. The operator must verify that before adoption.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import fakeredis
import pytest

from core.storage.local import RedisTracking
from tools import submit_precheck as precheck


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
JOB = {
    "pk": "example-co#candidate", "company": "example-co",
    "job_id": "candidate", "title": "Synthetic Engineer",
    "status": "tailored",
}


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "precheck"
    root.mkdir()
    policy = {
        "company_caps": {}, "company_aliases": {},
        "title_aliases": {}, "historical_applications": [],
        "blocked_job_ids": [],
    }
    (root / "precheck-data.json").write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("APPLIEDIN_PRECHECK_DATA_DIR", str(root))
    client = fakeredis.FakeRedis(decode_responses=True)
    tracking = RedisTracking(client)

    def seed(*rows):
        for row in rows:
            client.set("app:" + row["pk"], json.dumps(row))

    def save():
        (root / "precheck-data.json").write_text(json.dumps(policy), encoding="utf-8")

    def invoke(operation="check", pk=JOB["pk"]):
        return precheck.main([operation, pk], legacy_dir=tmp_path / "no-real-operator",
                             tracking=tracking, at=NOW)

    seed(JOB)
    return root, policy, tracking, seed, save, invoke


def test_eligible_and_event_stability(world, capsys):
    root, _, _, _, _, invoke = world
    assert invoke() == 0
    assert capsys.readouterr().out == "OK\n"
    records = [json.loads(x) for x in (root / "precheck-events.log").read_text().splitlines()]
    assert records == [{
        "at": NOW.isoformat(), "pk": JOB["pk"], "operation": "check",
        "allow": True, "reason": "eligible",
    }]
    assert (root / "precheck.lock").is_file()


def test_duplicate_id_cannot_be_hidden_by_a_different_title_or_company(world, capsys):
    root, _, _, seed, _, invoke = world
    seed({
        "pk": "another-co#candidate", "company": "another-co",
        "job_id": "candidate", "title": "Different synthetic title",
        "status": "applied", "applied_at": NOW.isoformat(),
    })
    assert invoke() == 2
    assert capsys.readouterr().out == "STOP: duplicate_job_id\n"
    event = json.loads((root / "precheck-events.log").read_text().splitlines()[-1])
    assert event["reason"] == "duplicate_job_id"


def test_normalized_title_company_and_placeholder_are_distinct(world, capsys):
    _, _, _, seed, _, invoke = world
    seed({
        "pk": "example-co#old", "company": "Example Co",
        "job_id": "old", "title": "  SYNTHETIC--Engineer!  ",
        "status": "applied_manual", "applied_at": NOW.isoformat(),
    })
    assert invoke() == 2
    assert "duplicate_title_company" in capsys.readouterr().out


def test_title_aliases_and_distinct_company(world):
    _, policy, tracking, seed, save, invoke = world
    policy["company_aliases"] = {"example company": "example-co"}
    policy["title_aliases"] = {"synthetic eng": "synthetic engineer"}
    save()
    seed({
        "pk": "another-co#old", "company": "another-co",
        "job_id": "old", "title": "Synthetic Engineer",
        "status": "applied",
    })
    assert invoke() == 0
    row = {**JOB, "title": "Synthetic Eng", "company": "Example Company"}
    tracking.r.set("app:" + JOB["pk"], json.dumps(row))
    seed({
        "pk": "example-co#other", "company": "example-co",
        "job_id": "other", "title": "Synthetic Engineer",
        "status": "applied",
    })
    assert invoke() == 2


def test_placeholder_title_is_not_a_wildcard_duplicate(world):
    _, _, tracking, seed, _, invoke = world
    tracking.r.set("app:" + JOB["pk"], json.dumps({**JOB, "title": "Unknown"}))
    seed({
        "pk": "example-co#prior", "company": "example-co",
        "job_id": "prior", "title": "UNKNOWN",
        "status": "applied", "applied_at": NOW.isoformat(),
    })
    assert invoke() == 0


def test_cap_at_fifth_sixth_and_expired_boundary(world, capsys):
    _, _, _, seed, _, invoke = world
    priors = [
        {
            "pk": "example-co#prior-" + str(i),
            "company": "example-co", "job_id": "prior-" + str(i),
            "title": "Synthetic title " + str(i), "status": "applied",
            "applied_at": (NOW - timedelta(days=30)).isoformat(),
        }
        for i in range(5)
    ]
    expired = {
        **priors[4],
        "applied_at": (NOW - timedelta(days=180, seconds=1)).isoformat(),
    }
    seed(*priors[:4], expired)
    assert invoke() == 0  # Four qualifying priors, prospective fifth.
    capsys.readouterr()
    seed(priors[4])
    assert invoke() == 2  # Five qualifying priors, prospective sixth.
    assert capsys.readouterr().out == "STOP: company_cap\n"


@pytest.mark.parametrize(
    ("handoff", "allowed"),
    [
        ("", False),
        ("An active request", False),
        ("An active request\nBACK TO AGENTS\nstill active", False),
        ("An active request\nBACK TO AGENTS\n", True),
        ("An active request\nDONE\n", True),
    ],
)
def test_last_line_handoff_only(world, handoff, allowed):
    root, _, _, _, _, invoke = world
    (root / "owner-handoff.md").write_text(handoff, encoding="utf-8")
    assert (invoke() == 0) is allowed


def test_skipped_and_unconfirmed_click_never_write_applied(world):
    root, _, tracking, _, _, invoke = world
    tracking.r.set("app:" + JOB["pk"], json.dumps({**JOB, "status": "skipped"}))
    assert invoke() == 2
    tracking.r.set("app:" + JOB["pk"], json.dumps(JOB))
    assert invoke("record-click") == 0
    assert invoke() == 2
    assert tracking.get(JOB["pk"])["status"] == "tailored"
    claims = list((root / "claims").glob("*.json"))
    assert len(claims) == 1
    claim = json.loads(claims[0].read_text())
    assert claim["submit_clicked"] is True
    assert claim["pk"] == JOB["pk"]
    assert len((root / "precheck-events.log").read_text().splitlines()) == 3


def test_submit_clicked_prior_counts_without_confirmed_status(world):
    _, _, _, seed, _, invoke = world
    seed({
        "pk": "example-co#old", "company": "example-co",
        "job_id": "candidate", "title": "Different synthetic role",
        "status": "needs_human", "submit_clicked_at": NOW.isoformat(),
    })
    assert invoke() == 2


def test_unconfirmed_click_counts_for_another_job_without_applied_status(world):
    _, _, tracking, seed, _, invoke = world
    assert invoke("record-click") == 0
    assert tracking.get(JOB["pk"])["status"] == "tailored"
    seed({
        "pk": "example-co#next", "company": "example-co",
        "job_id": "next", "title": JOB["title"], "status": "tailored",
    })
    assert invoke("check", pk="example-co#next") == 2


def test_board_pin_exact_match(world, monkeypatch):
    _, _, tracking, _, _, invoke = world
    tracking.r.set("app:" + JOB["pk"], json.dumps({
        **JOB, "greenhouse_board": "example-board",
    }))
    monkeypatch.setenv("BOARD_AGENT_BOARD", "different-board")
    assert invoke() == 2
    monkeypatch.setenv("BOARD_AGENT_BOARD", "example-board")
    assert invoke() == 0


@pytest.mark.parametrize("override", ["", "relative/path", "/not-a-real-precheck-root"])
def test_bad_override_refuses_before_any_guard_file_or_lock(world, monkeypatch, capsys,
                                                              override):
    root, _, tracking, _, _, _ = world
    monkeypatch.setenv("APPLIEDIN_PRECHECK_DATA_DIR", override)
    result = precheck.main(
        ["check", JOB["pk"]], legacy_dir=root, tracking=tracking, at=NOW,
    )
    assert result != 0
    captured = capsys.readouterr()
    assert "OK" not in captured.out
    assert not (root / "precheck-events.log").exists()
    assert not (root / "precheck.lock").exists()
    assert not (root / "claims").exists()


def test_symlinked_lock_cannot_escape_valid_data_root(world, tmp_path, capsys):
    root, _, _, _, _, invoke = world
    outside = tmp_path / "outside"
    outside.write_text("sentinel", encoding="utf-8")
    (root / "precheck.lock").symlink_to(outside)
    assert invoke() == 2
    assert capsys.readouterr().out == ""
    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert not (root / "precheck-events.log").exists()


def test_missing_or_invalid_json_fails_closed_with_no_lock(world, capsys):
    root, policy, tracking, _, save, invoke = world
    (root / "precheck-data.json").unlink()
    assert invoke() == 2
    assert "OK" not in capsys.readouterr().out
    assert not (root / "precheck.lock").exists()
    policy["company_caps"] = {"example-co": -1}
    save()
    assert invoke() == 2
    assert not (root / "precheck.lock").exists()


def test_unset_override_uses_disposable_legacy_shim_anchor(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("APPLIEDIN_PRECHECK_DATA_DIR", raising=False)
    operator = tmp_path / "disposable" / ".local" / "operator"
    operator.mkdir(parents=True)
    (operator / "precheck-data.json").write_text(
        json.dumps(precheck.PUBLIC_DEFAULTS), encoding="utf-8",
    )
    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    tracking.r.set("app:" + JOB["pk"], json.dumps(JOB))
    assert precheck.main(["check", JOB["pk"]], legacy_dir=operator,
                         tracking=tracking, at=NOW) == 0
    assert capsys.readouterr().out == "OK\n"
    assert (operator / "precheck.lock").exists()
    assert not (tmp_path / "precheck.lock").exists()


def test_two_serialized_calls_record_two_events_not_one_winner(world):
    root, _, _, _, _, invoke = world
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: invoke(), range(2)))
    assert results == [0, 0]
    events = [json.loads(s) for s in
              (root / "precheck-events.log").read_text().splitlines()]
    assert len(events) == 2
    assert all(e["reason"] == "eligible" and e["allow"] for e in events)


def test_shim_can_bootstrap_from_foreign_cwd_without_checkout_pythonpath(tmp_path):
    root = Path(__file__).resolve().parents[2]
    shim = root / "scripts" / "submit_precheck_shim.py"
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    child = r"""
import runpy
import sys
from pathlib import Path
repo, shim = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
# An editable-install .pth can independently put src into sys.path even
# after PYTHONPATH is cleared; remove it explicitly without disabling the
# offline sitecustomize network audit hook.
sys.path[:] = [
    entry for entry in sys.path
    if Path(entry or ".").resolve() not in (repo, repo / "src")
]
assert str(repo) not in sys.path
assert str(repo / "src") not in sys.path
sys.argv = [str(shim), "--help"]
try:
    runpy.run_path(str(shim), run_name="__main__")
except SystemExit as exc:
    assert exc.code == 0
else:
    raise AssertionError("shim help did not terminate as expected")
assert Path(sys.modules["tools.submit_precheck"].__file__).resolve() == (
    repo / "src" / "tools" / "submit_precheck.py"
)
"""
    env = dict(os.environ)
    # tests/offline/sitecustomize.py necessarily reinjects its own startup
    # hook and blocks networking. It must NOT reinject the repo's import roots.
    env["PYTHONPATH"] = ""
    run = subprocess.run(
        [sys.executable, "-c", child, str(root), str(shim)],
        cwd=foreign, env=env, text=True, capture_output=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert "usage: precheck" in run.stdout.lower()
