"""API Gateway (HTTP API) webhook — verify, gate, enqueue, ACK fast.

Security model (HLD): a forged POST is a forged approval, so
  1. every POST body must carry a valid ``X-Hub-Signature-256`` app-secret
     HMAC (constant-time compare), and
  2. only updates whose sender wa_id is Sayantan's are processed — anything
     else is acknowledged with 200 (so Meta stops retrying) but never enqueued.

The handler does no real work: valid updates go to the internal processing
queue and the processor Lambda picks them up asynchronously.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any

from core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class WebhookConfig:
    verify_token: str
    app_secret: str
    owner_wa_id: str  # Sayantan's wa_id — the only accepted sender
    process_queue_url: str


def config_from_env() -> WebhookConfig:
    return WebhookConfig(
        verify_token=os.environ.get("APPLIEDIN_WA_VERIFY_TOKEN", ""),
        app_secret=os.environ.get("APPLIEDIN_WA_APP_SECRET", ""),
        owner_wa_id=os.environ.get("APPLIEDIN_WA_OWNER_WA_ID", ""),
        process_queue_url=os.environ.get("APPLIEDIN_WA_PROCESS_QUEUE_URL", ""),
    )


def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """Check ``X-Hub-Signature-256`` == ``sha256=`` + HMAC-SHA256(app_secret, body).

    Constant-time compare so the signature cannot be brute-forced byte by byte.
    """
    if not header or not app_secret:
        return False
    expected = "sha256=" + hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _sender_wa_ids(update: dict) -> list[str]:
    """All sender wa_ids in the update (delivery/status events have none)."""
    senders: list[str] = []
    for entry in update.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                senders.append(msg.get("from", ""))
    return senders


def _response(status: int, body: str) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "text/plain"}, "body": body}


def _raw_body(event: dict) -> bytes:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return body.encode()


def handler(event: dict, context: Any = None, *, config: WebhookConfig | None = None,
            queue: Any = None) -> dict:  # noqa: ANN401 - Lambda signature
    """API Gateway HTTP API (payload v2) entrypoint."""
    config = config or config_from_env()
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "POST"
    )

    if method == "GET":
        params = event.get("queryStringParameters") or {}
        if (
            params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token") == config.verify_token
        ):
            return _response(200, params.get("hub.challenge", ""))
        return _response(403, "verification failed")

    raw = _raw_body(event)
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not verify_signature(config.app_secret, raw, headers.get("x-hub-signature-256")):
        log.warning("webhook: bad or missing signature — rejected")
        return _response(403, "bad signature")

    try:
        update = json.loads(raw)
    except ValueError:
        return _response(400, "bad json")

    senders = _sender_wa_ids(update)
    if not senders:
        return _response(200, "ignored")  # status/delivery events — nothing to do
    if any(s != config.owner_wa_id for s in senders):
        # Signed but not from Sayantan: ACK so Meta stops retrying, never process.
        log.warning("webhook: ignoring update from foreign wa_id")
        return _response(200, "ignored")

    if queue is None:
        from core.storage.queue import Queue  # lazy: not needed on GET

        queue = Queue()
    queue.enqueue(config.process_queue_url, update)
    return _response(200, "ok")
