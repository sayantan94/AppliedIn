"""Meta WhatsApp Business Cloud API client (Graph API, direct — no middleman).

The HTTP client is injectable so tests never touch the network; when omitted,
an ``httpx.Client`` is created lazily on first send.
"""

from __future__ import annotations

from typing import Any

from .templates import MAX_BUTTONS, button_id

GRAPH_BASE = "https://graph.facebook.com/v20.0"


class MetaClient:
    def __init__(
        self,
        token: str,
        phone_number_id: str,
        *,
        http: Any = None,
        base_url: str = GRAPH_BASE,
    ) -> None:
        self._token = token
        self._phone_number_id = phone_number_id
        self._http = http
        self._base_url = base_url.rstrip("/")

    def _client(self) -> Any:
        if self._http is None:
            import httpx  # lazy: keep import cost off the webhook cold path

            self._http = httpx.Client(timeout=10)
        return self._http

    def _send(self, payload: dict) -> dict:
        url = f"{self._base_url}/{self._phone_number_id}/messages"
        resp = self._client().post(
            url,
            json={"messaging_product": "whatsapp", **payload},
            headers={"Authorization": f"Bearer {self._token}"},
        )
        resp.raise_for_status()
        return resp.json()

    def send_text(self, wa_id: str, text: str) -> dict:
        return self._send({"to": wa_id, "type": "text", "text": {"body": text}})

    def send_buttons(self, wa_id: str, text: str, buttons: list[str]) -> dict:
        """Interactive reply-button message. WhatsApp hard-caps at 3 buttons."""
        if len(buttons) > MAX_BUTTONS:
            raise ValueError(f"WhatsApp allows at most {MAX_BUTTONS} buttons, got {len(buttons)}")
        return self._send(
            {
                "to": wa_id,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": text},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": button_id(b), "title": b}}
                            for b in buttons
                        ]
                    },
                },
            }
        )

    def send_template(self, wa_id: str, name: str, params: list[str]) -> dict:
        """Pre-approved template message — required outside the 24h reply window."""
        return self._send(
            {
                "to": wa_id,
                "type": "template",
                "template": {
                    "name": name,
                    "language": {"code": "en"},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [{"type": "text", "text": p} for p in params],
                        }
                    ],
                },
            }
        )

    def send_document(self, wa_id: str, link: str, caption: str = "") -> dict:
        """Attach a stored artifact (e.g. a presigned resume PDF / screenshot)."""
        return self._send(
            {
                "to": wa_id,
                "type": "document",
                "document": {"link": link, "caption": caption},
            }
        )
