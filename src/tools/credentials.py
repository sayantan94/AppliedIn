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


def get_login(company: str, secrets: Any) -> dict | None:
    """Return {'username', 'password'} for this company's portal, or None."""
    return secrets.get_json(portal_secret(company))
