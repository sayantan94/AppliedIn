"""Webhook: GET challenge, HMAC signature gate, owner-only wa_id gate, fast enqueue."""

from __future__ import annotations

import hashlib
import hmac
import json

from whatsapp.webhook import WebhookConfig, handler, verify_signature

APP_SECRET = "s3cret"
OWNER = "15550001111"

CONFIG = WebhookConfig(
    verify_token="verify-me",
    app_secret=APP_SECRET,
    owner_wa_id=OWNER,
    process_queue_url="https://sqs/process",
)


class FakeQueue:
    def __init__(self):
        self.messages = []

    def enqueue(self, url, body):
        self.messages.append((url, body))
        return "mid"


def _update(wa_id=OWNER, text="hello"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": "wamid.IN",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _post_event(update: dict, *, signature: str | None = None) -> dict:
    body = json.dumps(update)
    sig = signature if signature is not None else _sign(body.encode())
    return {
        "requestContext": {"http": {"method": "POST"}},
        "headers": {"X-Hub-Signature-256": sig},
        "body": body,
    }


def test_get_challenge_echoed_on_token_match():
    event = {
        "requestContext": {"http": {"method": "GET"}},
        "queryStringParameters": {
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-me",
            "hub.challenge": "12345",
        },
    }
    resp = handler(event, config=CONFIG, queue=FakeQueue())
    assert resp["statusCode"] == 200
    assert resp["body"] == "12345"


def test_get_wrong_token_rejected():
    event = {
        "requestContext": {"http": {"method": "GET"}},
        "queryStringParameters": {
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "12345",
        },
    }
    resp = handler(event, config=CONFIG, queue=FakeQueue())
    assert resp["statusCode"] == 403


def test_bad_signature_rejected_and_not_enqueued():
    queue = FakeQueue()
    event = _post_event(_update(), signature="sha256=" + "0" * 64)
    resp = handler(event, config=CONFIG, queue=queue)
    assert resp["statusCode"] == 403
    assert queue.messages == []


def test_missing_signature_rejected():
    queue = FakeQueue()
    event = _post_event(_update())
    del event["headers"]["X-Hub-Signature-256"]
    resp = handler(event, config=CONFIG, queue=queue)
    assert resp["statusCode"] == 403
    assert queue.messages == []


def test_foreign_wa_id_acked_but_ignored():
    queue = FakeQueue()
    resp = handler(_post_event(_update(wa_id="19998887777")), config=CONFIG, queue=queue)
    assert resp["statusCode"] == 200  # ACK so Meta stops retrying
    assert queue.messages == []  # ...but a forged sender never reaches the router


def test_valid_update_enqueued_with_200():
    queue = FakeQueue()
    update = _update(text="/status")
    resp = handler(_post_event(update), config=CONFIG, queue=queue)
    assert resp["statusCode"] == 200
    assert queue.messages == [("https://sqs/process", update)]


def test_status_only_update_ignored():
    queue = FakeQueue()
    update = {"entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}]}
    resp = handler(_post_event(update), config=CONFIG, queue=queue)
    assert resp["statusCode"] == 200
    assert queue.messages == []


def test_verify_signature_is_exact():
    body = b'{"a":1}'
    assert verify_signature(APP_SECRET, body, _sign(body)) is True
    assert verify_signature(APP_SECRET, body, _sign(b'{"a":2}')) is False
    assert verify_signature(APP_SECRET, body, None) is False
    assert verify_signature(APP_SECRET, body, "sha1=abc") is False
