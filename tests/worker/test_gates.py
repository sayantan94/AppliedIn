"""raise_gate persists context to S3 FIRST, then flips status, then notifies."""

from __future__ import annotations

import json

from core.models import GateReason, JobRecord, Status
from core.storage.artifacts import ArtifactStore
from core.storage.tracking import TrackingStore
from worker.gates import raise_gate


class FakeNotifier:
    def __init__(self):
        self.gates = []
        self.receipts = []

    def send_gate(self, pk, reason, *, fieldmap_key, screenshot_keys):
        self.gates.append((pk, reason, fieldmap_key, screenshot_keys))

    def send_receipt(self, pk, confirmation_id):
        self.receipts.append((pk, confirmation_id))


def _seed_job(tracking: TrackingStore) -> str:
    job = JobRecord(
        company="Acme", job_id="1", title="SWE", jd_url="u",
        jd_text="x", location="R", ats="greenhouse",
    )
    tracking.put_new(job)
    return job.pk


def test_raise_gate_persists_sets_status_and_notifies(applications_table, artifacts_bucket):
    tracking = TrackingStore(applications_table)
    artifacts = ArtifactStore(artifacts_bucket)
    notifier = FakeNotifier()
    pk = _seed_job(tracking)
    fieldmap = {"First Name": "Sayantan"}
    snapshot = {"engine": "scripted", "fields": [{"label": "First Name"}]}

    raise_gate(
        pk,
        GateReason.LOW_CONFIDENCE,
        fieldmap,
        snapshot,
        [b"png-one", b"png-two"],
        tracking=tracking,
        artifacts=artifacts,
        notifier=notifier,
    )

    row = tracking.get(pk)
    assert row["status"] == Status.NEEDS_HUMAN.value
    assert row["gate_reason"] == GateReason.LOW_CONFIDENCE.value

    assert json.loads(artifacts.get(row["fieldmap_s3_key"])) == fieldmap
    assert json.loads(artifacts.get(row["snapshot_s3_key"])) == snapshot
    assert artifacts.get(row["screenshot_s3_key"]) == b"png-one"

    (gpk, reason, fieldmap_key, shot_keys) = notifier.gates[0]
    assert gpk == pk and reason is GateReason.LOW_CONFIDENCE
    assert fieldmap_key == row["fieldmap_s3_key"]
    assert len(shot_keys) == 2 and artifacts.get(shot_keys[1]) == b"png-two"


def test_raise_gate_without_screenshots(applications_table, artifacts_bucket):
    tracking = TrackingStore(applications_table)
    notifier = FakeNotifier()
    pk = _seed_job(tracking)

    raise_gate(
        pk,
        GateReason.CAPTCHA,
        {},
        {},
        [],
        tracking=tracking,
        artifacts=ArtifactStore(artifacts_bucket),
        notifier=notifier,
    )

    row = tracking.get(pk)
    assert row["gate_reason"] == GateReason.CAPTCHA.value
    assert row["screenshot_s3_key"] == ""
    assert notifier.gates[0][3] == []
