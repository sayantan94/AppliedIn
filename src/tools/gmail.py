"""Gmail read-only verification fetch (HLD auto-signup constraint / OQ4).

Auto-signup needs email verification links/codes; the same plumbing covers
email codes on login. Scope is strictly ``gmail.readonly``. The OAuth token
lives in Secrets Manager; the built Gmail service is injectable so tests never
touch the network (and ``googleapiclient`` is imported lazily so the package
imports without it installed).
"""

from __future__ import annotations

import base64
import re
from typing import Any

from core.storage.secrets import SecretsClient

GMAIL_TOKEN_SECRET = "appliedin/gmail-token"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

_CODE_RE = re.compile(r"\b(\d{4,8})\b")


def fetch_code(query: str, secrets: SecretsClient, *, service: Any = None) -> str | None:
    """Search Gmail for ``query`` and return the first verification code found.

    ``service`` is an injectable Gmail API service (faked in tests). Returns
    None when no matching message carries a 4-8 digit code — the caller treats
    that as a blocked signup and gates as ``no_account``.
    """
    if service is None:
        service = _build_service(secrets)
    listing = service.users().messages().list(userId="me", q=query, maxResults=5).execute()
    for meta in listing.get("messages", []):
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=meta["id"], format="full")
            .execute()
        )
        code = _extract_code(msg)
        if code is not None:
            return code
    return None


def _extract_code(message: dict) -> str | None:
    """Pull a 4-8 digit code out of the snippet or any decoded body part."""
    texts = [message.get("snippet", "")]
    stack = [message.get("payload", {})]
    while stack:
        part = stack.pop()
        data = part.get("body", {}).get("data")
        if data:
            padded = data + "=" * (-len(data) % 4)
            texts.append(base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace"))
        stack.extend(part.get("parts", []))
    for text in texts:
        match = _CODE_RE.search(text)
        if match:
            return match.group(1)
    return None


def _build_service(secrets: SecretsClient) -> Any:
    """Build the real Gmail service from the stored OAuth token (lazy imports)."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token = secrets.get_json(GMAIL_TOKEN_SECRET)
    if token is None:
        raise RuntimeError(f"gmail token secret {GMAIL_TOKEN_SECRET!r} not found")
    creds = Credentials.from_authorized_user_info(token, scopes=GMAIL_SCOPES)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)
