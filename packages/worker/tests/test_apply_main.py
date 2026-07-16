"""run_apply branch matrix with fakes — no browser, no network, no model."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from appliedin_core.config import Settings
from appliedin_core.models import ApplyMode, GateReason, JobRecord, Status
from appliedin_core.storage.answer_bank import AnswerBank
from appliedin_core.storage.artifacts import ArtifactStore
from appliedin_core.storage.tracking import TrackingStore
from appliedin_worker.apply_main import ApplyDeps, PortalConfig, run_apply
from appliedin_worker.engines import FillResult


class FakePage:
    def __init__(self):
        self.calls = []

    async def goto(self, url):
        self.calls.append(("goto", url))

    async def click(self, selector):
        self.calls.append(("click", selector))

    async def fill(self, selector, value):
        self.calls.append(("fill", selector, value))

    async def screenshot(self):
        return b"png"


class StubEngine:
    def __init__(self, result: FillResult):
        self._result = result
        self.fill_calls = 0

    async def fill(self, page, job, resolver):
        self.fill_calls += 1
        return self._result


class FakeNotifier:
    def __init__(self):
        self.gates = []
        self.receipts = []

    def send_gate(self, pk, reason, *, fieldmap_key, screenshot_keys):
        self.gates.append((pk, reason))

    def send_receipt(self, pk, confirmation_id):
        self.receipts.append((pk, confirmation_id))


class FakeSecrets:
    def __init__(self, store=None):
        self.store = dict(store or {})

    def get_json(self, name):
        return self.store.get(name)

    def put_json(self, name, obj):
        self.store[name] = obj


def _clean_result() -> FillResult:
    return FillResult(
        fields={"First Name": "Sayantan"},
        low_confidence_labels=[],
        form_snapshot={"engine": "scripted", "ats": "greenhouse", "fields": []},
    )


async def _submit_ok(page):
    return "CONF-123"


@pytest.fixture
def env(applications_table, answer_bank_table, artifacts_bucket):
    tracking = TrackingStore(applications_table)
    job = JobRecord(
        company="Acme", job_id="1", title="SWE", jd_url="https://jobs/acme/1",
        jd_text="x", location="R", ats="greenhouse",
    )
    tracking.put_new(job, status=Status.TAILORED)
    return {
        "tracking": tracking,
        "artifacts": ArtifactStore(artifacts_bucket),
        "answer_bank": AnswerBank(answer_bank_table),
        "pk": job.pk,
    }


def _deps(env, *, mode=ApplyMode.AUTO, engine=None, daily_cap=5, submitter=_submit_ok, **kw):
    notifier = FakeNotifier()
    return (
        ApplyDeps(
            tracking=env["tracking"],
            answer_bank=env["answer_bank"],
            artifacts=env["artifacts"],
            secrets=kw.pop("secrets", FakeSecrets()),
            notifier=notifier,
            settings=Settings(daily_cap=daily_cap),
            portals={"acme": kw.pop("portal", PortalConfig(mode=mode))},
            page=FakePage(),
            engine=engine or StubEngine(_clean_result()),
            submitter=submitter,
            **kw,
        ),
        notifier,
    )


async def test_low_confidence_field_gates_and_never_submits(env):
    engine = StubEngine(
        FillResult(
            fields={"First Name": "Sayantan"},
            low_confidence_labels=["Describe a time when you failed"],
            form_snapshot={"engine": "scripted", "fields": []},
        )
    )
    deps, notifier = _deps(env, engine=engine)

    status = await run_apply(env["pk"], deps=deps)

    assert status is Status.NEEDS_HUMAN
    row = env["tracking"].get(env["pk"])
    assert row["status"] == Status.NEEDS_HUMAN.value
    assert row["gate_reason"] == GateReason.LOW_CONFIDENCE.value
    # Fieldmap persisted for the approval-resume path.
    assert json.loads(env["artifacts"].get(row["fieldmap_s3_key"])) == {
        "First Name": "Sayantan"
    }
    assert notifier.gates == [(env["pk"], GateReason.LOW_CONFIDENCE)]
    assert ("click", 'button[type="submit"]') not in deps.page.calls
    # The cap slot was never consumed: today's counter still admits cap=1.
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert env["tracking"].try_increment_daily_cap(today, cap=1) is True


async def test_auto_happy_path_applies_with_confirmation_and_screenshot(env):
    deps, notifier = _deps(env)

    status = await run_apply(env["pk"], deps=deps)

    assert status is Status.APPLIED
    row = env["tracking"].get(env["pk"])
    assert row["status"] == Status.APPLIED.value
    assert row["confirmation_id"] == "CONF-123"
    assert row["submitted_at"]
    assert env["artifacts"].get(row["screenshot_s3_key"]) == b"png"
    assert notifier.receipts == [(env["pk"], "CONF-123")]
    assert ("goto", "https://jobs/acme/1") in deps.page.calls


async def test_already_submitting_gates_submit_uncertain_and_never_resubmits(env):
    env["tracking"].set_status(env["pk"], Status.SUBMITTING)
    engine = StubEngine(_clean_result())
    deps, notifier = _deps(env, engine=engine)

    status = await run_apply(env["pk"], deps=deps)

    assert status is Status.NEEDS_HUMAN
    row = env["tracking"].get(env["pk"])
    assert row["gate_reason"] == GateReason.SUBMIT_UNCERTAIN.value
    assert engine.fill_calls == 0  # never re-filled, never re-submitted
    assert deps.page.calls == []
    assert notifier.gates == [(env["pk"], GateReason.SUBMIT_UNCERTAIN)]


async def test_over_cap_parks_as_capped_without_submitting(env):
    deps, notifier = _deps(env, daily_cap=1)
    # Exhaust today's cap: another apply task already took the only slot.
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert env["tracking"].try_increment_daily_cap(today, cap=1) is True

    status = await run_apply(env["pk"], deps=deps)

    assert status is Status.CAPPED
    row = env["tracking"].get(env["pk"])
    assert row["status"] == Status.CAPPED.value  # discovery cron re-enqueues these
    assert ("click", 'button[type="submit"]') not in deps.page.calls
    assert notifier.gates == [] and notifier.receipts == []


async def test_gated_mode_gates_with_fieldmap_for_one_tap_approval(env):
    deps, notifier = _deps(env, mode=ApplyMode.GATED)

    status = await run_apply(env["pk"], deps=deps)

    assert status is Status.NEEDS_HUMAN
    row = env["tracking"].get(env["pk"])
    assert row["gate_reason"] == GateReason.GATED_MODE.value
    assert json.loads(env["artifacts"].get(row["fieldmap_s3_key"])) == {
        "First Name": "Sayantan"
    }
    assert notifier.gates == [(env["pk"], GateReason.GATED_MODE)]


async def test_submit_without_confirmation_gates_submit_uncertain(env):
    async def submit_no_confirmation(page):
        return None

    deps, _ = _deps(env, submitter=submit_no_confirmation)

    status = await run_apply(env["pk"], deps=deps)

    assert status is Status.NEEDS_HUMAN
    row = env["tracking"].get(env["pk"])
    assert row["gate_reason"] == GateReason.SUBMIT_UNCERTAIN.value


async def test_blocked_signup_gates_no_account(env):
    # Portal requires an account; no stored creds and no verification code.
    deps, notifier = _deps(
        env,
        portal=PortalConfig(mode=ApplyMode.AUTO, login_secret="appliedin/portal/acme"),
        identity={"email": "s@x.com"},
        gmail_fetch=lambda query: None,
    )

    status = await run_apply(env["pk"], deps=deps)

    assert status is Status.NEEDS_HUMAN
    row = env["tracking"].get(env["pk"])
    assert row["gate_reason"] == GateReason.NO_ACCOUNT.value
    # Crash safety held: the generated password was saved before the gate.
    assert "appliedin/portal/acme" in deps.secrets.store
    assert notifier.gates == [(env["pk"], GateReason.NO_ACCOUNT)]


async def test_unknown_pk_is_a_noop(env):
    deps, notifier = _deps(env)
    assert await run_apply("nope#0", deps=deps) is None
    assert notifier.gates == [] and notifier.receipts == []
