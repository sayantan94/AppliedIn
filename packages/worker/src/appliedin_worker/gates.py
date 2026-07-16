"""Gate persistence + notification (HLD GATE path).

When automation must stop (CAPTCHA, no account, unknown field, low confidence,
gated mode, submit uncertainty), the full context is persisted to S3 first —
field-map JSON, form-structure snapshot, screenshots — so the approval-resume
flow can re-fill without a new LLM mapping. Only then is the row flipped to
``needs_human`` and the human notified. The notifier is injected: the real one
lives in the whatsapp package; tests pass a fake.
"""

from __future__ import annotations

import json
from typing import Protocol

from appliedin_core.logging import get_logger
from appliedin_core.models import GateReason, Status
from appliedin_core.storage.artifacts import ArtifactStore
from appliedin_core.storage.tracking import TrackingStore

log = get_logger(__name__)


class Notifier(Protocol):
    """Outbound human notifications (WhatsApp in production, faked in tests)."""

    def send_gate(
        self,
        pk: str,
        reason: GateReason,
        *,
        fieldmap_key: str,
        screenshot_keys: list[str],
    ) -> None: ...

    def send_receipt(self, pk: str, confirmation_id: str | None) -> None: ...


def raise_gate(
    pk: str,
    reason: GateReason,
    fieldmap: dict,
    snapshot: dict,
    screenshots: list[bytes],
    *,
    tracking: TrackingStore,
    artifacts: ArtifactStore,
    notifier: Notifier,
) -> None:
    """Persist gate context to S3, set ``needs_human`` + gate_reason, notify.

    Persist-before-status ordering matters: once the row says needs_human the
    human may act immediately, so the artifacts they act on must already exist.
    """
    fieldmap_key = artifacts.put(
        "fieldmaps",
        f"{pk}/fieldmap.json",
        json.dumps(fieldmap).encode("utf-8"),
        "application/json",
    )
    snapshot_key = artifacts.put(
        "fieldmaps",
        f"{pk}/snapshot.json",
        json.dumps(snapshot).encode("utf-8"),
        "application/json",
    )
    screenshot_keys = [
        artifacts.put("screenshots", f"{pk}/gate-{i}.png", data, "image/png")
        for i, data in enumerate(screenshots)
    ]
    tracking.set_status(
        pk,
        Status.NEEDS_HUMAN,
        gate_reason=reason.value,
        fieldmap_s3_key=fieldmap_key,
        snapshot_s3_key=snapshot_key,
        screenshot_s3_key=screenshot_keys[0] if screenshot_keys else "",
    )
    notifier.send_gate(pk, reason, fieldmap_key=fieldmap_key, screenshot_keys=screenshot_keys)
    log.info("gated %s reason=%s", pk, reason.value)
