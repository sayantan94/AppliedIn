"""Crash-safe auto-signup (HLD Constraints).

Ordering is the whole point: the generated password is written to Secrets
Manager BEFORE the signup form is touched, so a crash can never orphan an
account the system doesn't know about. One account per portal, enforced by
checking Secrets Manager first — if creds already exist they are returned
as-is and NO signup is attempted. "Existing creds but login fails" is the
CALLER's problem and is NEVER treated as re-signup (it gates as needs_human).

Minimal async Page surface driven here (the caller positions the page at the
portal's signup form before calling; tests pass a fake recording call order):
    await page.fill(selector, value)
    await page.click(selector)
"""

from __future__ import annotations

import secrets as pysecrets
import string
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from core.logging import get_logger
from core.storage.secrets import SecretsClient

log = get_logger(__name__)

_LOWER = string.ascii_lowercase
_UPPER = string.ascii_uppercase
_DIGITS = string.digits
_SYMBOLS = "!@#$%^&*-_"
_ALPHABET = _LOWER + _UPPER + _DIGITS + _SYMBOLS

EMAIL_SELECTOR = 'input[type="email"]'
PASSWORD_SELECTOR = 'input[type="password"]'
SUBMIT_SELECTOR = 'button[type="submit"]'
CODE_SELECTOR = 'input[name="code"]'


class SignupError(RuntimeError):
    """Signup could not complete; the caller gates as ``no_account``."""


class SignupVerificationError(SignupError):
    """No verification code arrived; the account may exist but is unverified.

    Creds are already in Secrets Manager (written before submit), so the
    "Account created — retry" button is safe.
    """


def generate_password(length: int = 20) -> str:
    """A strong random password guaranteed to contain all four classes."""
    rng = pysecrets.SystemRandom()
    while True:
        pw = "".join(rng.choice(_ALPHABET) for _ in range(length))
        if (
            any(c in _LOWER for c in pw)
            and any(c in _UPPER for c in pw)
            and any(c in _DIGITS for c in pw)
            and any(c in _SYMBOLS for c in pw)
        ):
            return pw


async def ensure_account(
    portal_secret_name: str,
    identity: dict,
    secrets: SecretsClient,
    *,
    page: Any = None,
    gmail_fetch: Callable[[str], str | None] | None = None,
) -> dict:
    """Return portal creds, auto-signing-up if — and only if — none exist.

    ``identity`` holds the signup identity from the answer-bank global tier
    (at minimum ``email``). ``gmail_fetch`` is the injected verification-code
    fetcher (see :mod:`worker.gmail`).
    """
    existing = secrets.get_json(portal_secret_name)
    if existing is not None:
        # One account per portal. If these creds later fail login, the caller
        # gates as needs_human — NEVER re-signup.
        return existing

    if page is None:
        raise SignupError(f"no stored creds for {portal_secret_name!r} and no page to sign up")
    email = identity.get("email")
    if not email:
        raise SignupError("signup identity is missing 'email'")

    creds = {
        "email": email,
        "password": generate_password(),
        "created_at": datetime.now(UTC).isoformat(),
    }
    # CRASH SAFETY: persist BEFORE any page interaction. From here on, a crash
    # leaves at worst an account whose password we already know.
    secrets.put_json(portal_secret_name, creds)

    await page.fill(EMAIL_SELECTOR, creds["email"])
    await page.fill(PASSWORD_SELECTOR, creds["password"])
    await page.click(SUBMIT_SELECTOR)

    if gmail_fetch is not None:
        code = gmail_fetch(f"to:{email} newer_than:1d (verify OR verification OR code)")
        if code is None:
            raise SignupVerificationError(
                f"no verification code received for {portal_secret_name!r}"
            )
        await page.fill(CODE_SELECTOR, code)
        await page.click(SUBMIT_SELECTOR)

    log.info("account created for %s", portal_secret_name)
    return creds
