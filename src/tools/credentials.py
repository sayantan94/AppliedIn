"""Per-portal login credentials — you create the account once, we remember it.

When a portal needs an account, the applier gates ("create it and save the
login"). You create it, save the credential here (CLI or dashboard), and every
future application to that portal logs in with it. Stored in the mode's secret
store: a git-ignored JSON file locally, AWS Secrets Manager in the cloud.
"""

from __future__ import annotations

from typing import Any


def portal_secret(company: str) -> str:
    return f"portal/{company.strip().lower()}"


def save_login(company: str, username: str, password: str, secrets: Any) -> None:
    secrets.put_json(portal_secret(company), {"username": username, "password": password})


# Un-filled placeholder markers in secrets.json — treated as "no credential" so the
# applier hands off to the human to sign in rather than typing placeholder text.
_PLACEHOLDER = ("replace_with", "replace-with", "placeholder", "your_apple", "your-apple",
                "your_email", "your-email", "your_password", "your-password", "changeme",
                "xxxx", "<", "example.com")


def _unset(v: str | None) -> bool:
    v = (v or "").strip().lower()
    return not v or any(mark in v for mark in _PLACEHOLDER)


def get_login(company: str, secrets: Any) -> dict | None:
    """Return {'username', 'password'} for this company's portal, or None. An entry
    still holding placeholder text (not yet filled in) counts as None — the applier
    then hands off to the human to sign in in the browser window instead of trying
    to type a placeholder. (Portals with 2FA — Apple/Google/Microsoft — should be
    signed into once in the persistent window rather than stored here at all.)"""
    creds = secrets.get_json(portal_secret(company))
    if not creds or _unset(creds.get("username")) or _unset(creds.get("password")):
        return None
    return creds
