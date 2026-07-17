"""MetaClient: Graph API payload shape + the 3-button hard cap. No network."""

from __future__ import annotations

import json

import httpx
import pytest
from whatsapp.client import MetaClient

WA = "15550001111"


def _client_with_capture():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUT"}]})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return MetaClient("tok-123", "555000", http=http), calls


def _body(request: httpx.Request) -> dict:
    return json.loads(request.read())


def test_send_text_payload_shape():
    client, calls = _client_with_capture()
    client.send_text(WA, "hello")

    assert len(calls) == 1
    req = calls[0]
    assert req.url.path.endswith("/555000/messages")  # phone number id in path
    assert req.headers["Authorization"] == "Bearer tok-123"
    body = _body(req)
    assert body["messaging_product"] == "whatsapp"
    assert body["to"] == WA
    assert body["type"] == "text"
    assert body["text"]["body"] == "hello"


def test_send_buttons_payload_and_ids():
    client, calls = _client_with_capture()
    client.send_buttons(WA, "pick one", ["Approve", "Company only", "Skip"])

    body = _body(calls[0])
    assert body["type"] == "interactive"
    assert body["interactive"]["type"] == "button"
    assert body["interactive"]["body"]["text"] == "pick one"
    buttons = body["interactive"]["action"]["buttons"]
    assert [b["reply"]["title"] for b in buttons] == ["Approve", "Company only", "Skip"]
    assert [b["reply"]["id"] for b in buttons] == ["approve", "company_only", "skip"]
    assert all(b["type"] == "reply" for b in buttons)


def test_more_than_three_buttons_raises_and_sends_nothing():
    client, calls = _client_with_capture()
    with pytest.raises(ValueError):
        client.send_buttons(WA, "too many", ["A", "B", "C", "D"])
    assert calls == []  # rejected before any HTTP


def test_send_template_payload_shape():
    client, calls = _client_with_capture()
    client.send_template(WA, "receipt_v1", ["Acme", "SWE"])

    body = _body(calls[0])
    assert body["type"] == "template"
    assert body["template"]["name"] == "receipt_v1"
    params = body["template"]["components"][0]["parameters"]
    assert [p["text"] for p in params] == ["Acme", "SWE"]


def test_send_document_payload_shape():
    client, calls = _client_with_capture()
    client.send_document(WA, "https://s3/presigned.pdf", "resume sent to Acme")

    body = _body(calls[0])
    assert body["type"] == "document"
    assert body["document"]["link"] == "https://s3/presigned.pdf"
    assert body["document"]["caption"] == "resume sent to Acme"
