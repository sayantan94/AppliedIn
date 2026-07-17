"""fetch_code against a fake Gmail service — no googleapiclient required."""

from __future__ import annotations

import base64

from tools.gmail import fetch_code


class _Call:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeMessages:
    def __init__(self, listing, messages):
        self._listing = listing
        self._messages = messages
        self.queries = []

    def list(self, userId, q, maxResults):  # noqa: N803 - Gmail API surface
        self.queries.append(q)
        return _Call(self._listing)

    def get(self, userId, id, format):  # noqa: N803, A002 - Gmail API surface
        return _Call(self._messages[id])


class FakeService:
    def __init__(self, listing, messages):
        self.messages_api = FakeMessages(listing, messages)

    def users(self):
        return self

    def messages(self):
        return self.messages_api


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def test_fetch_code_finds_code_in_body():
    service = FakeService(
        listing={"messages": [{"id": "m1"}]},
        messages={
            "m1": {
                "snippet": "Welcome! Please verify your account.",
                "payload": {
                    "parts": [
                        {"body": {"data": _b64("Your verification code is 482913. Thanks!")}}
                    ]
                },
            }
        },
    )
    assert fetch_code("subject:verify", None, service=service) == "482913"


def test_fetch_code_prefers_snippet_when_present():
    service = FakeService(
        listing={"messages": [{"id": "m1"}]},
        messages={"m1": {"snippet": "code 7741", "payload": {}}},
    )
    assert fetch_code("q", None, service=service) == "7741"


def test_fetch_code_returns_none_when_no_message_matches():
    service = FakeService(listing={}, messages={})
    assert fetch_code("subject:verify", None, service=service) is None
