"""Auto-signup: crash-safe ordering + one-account-per-portal, with fakes
recording a single global call order."""

from __future__ import annotations

import pytest
from appliedin_worker.signup import (
    SignupError,
    SignupVerificationError,
    ensure_account,
    generate_password,
)


class OrderedFakeSecrets:
    """Secrets fake writing into a shared event log to prove ordering."""

    def __init__(self, events, existing=None):
        self.events = events
        self.store = dict(existing or {})

    def get_json(self, name):
        return self.store.get(name)

    def put_json(self, name, obj):
        self.store[name] = obj
        self.events.append(("secret_saved", name))


class OrderedFakePage:
    def __init__(self, events):
        self.events = events

    async def fill(self, selector, value):
        self.events.append(("fill", selector, value))

    async def click(self, selector):
        self.events.append(("click", selector))


async def test_password_persisted_before_any_page_interaction():
    events = []
    secrets = OrderedFakeSecrets(events)
    page = OrderedFakePage(events)

    creds = await ensure_account(
        "appliedin/portal/acme", {"email": "s@x.com"}, secrets, page=page
    )

    kinds = [e[0] for e in events]
    assert kinds[0] == "secret_saved", "password must land in Secrets Manager before submit"
    assert "click" in kinds and kinds.index("secret_saved") < kinds.index("click")
    assert secrets.store["appliedin/portal/acme"]["password"] == creds["password"]
    assert creds["email"] == "s@x.com"


async def test_existing_secret_returns_creds_and_never_signs_up():
    events = []
    existing = {"appliedin/portal/acme": {"email": "s@x.com", "password": "old"}}
    secrets = OrderedFakeSecrets(events, existing=existing)
    page = OrderedFakePage(events)

    creds = await ensure_account(
        "appliedin/portal/acme", {"email": "s@x.com"}, secrets, page=page
    )

    assert creds == {"email": "s@x.com", "password": "old"}
    assert events == []  # no put_json, no page interaction — NEVER re-signup


async def test_verification_code_is_fetched_and_entered():
    events = []
    secrets = OrderedFakeSecrets(events)
    page = OrderedFakePage(events)

    await ensure_account(
        "appliedin/portal/acme",
        {"email": "s@x.com"},
        secrets,
        page=page,
        gmail_fetch=lambda query: "482913",
    )

    assert ("fill", 'input[name="code"]', "482913") in events


async def test_missing_verification_code_raises_after_creds_are_safe():
    events = []
    secrets = OrderedFakeSecrets(events)
    page = OrderedFakePage(events)

    with pytest.raises(SignupVerificationError):
        await ensure_account(
            "appliedin/portal/acme",
            {"email": "s@x.com"},
            secrets,
            page=page,
            gmail_fetch=lambda query: None,
        )

    # Even on failure the account is not orphaned: creds were saved first.
    assert "appliedin/portal/acme" in secrets.store


async def test_no_page_and_no_creds_is_a_signup_error():
    secrets = OrderedFakeSecrets([])
    with pytest.raises(SignupError):
        await ensure_account("appliedin/portal/acme", {"email": "s@x.com"}, secrets)


def test_generated_password_is_strong():
    pw = generate_password()
    assert len(pw) == 20
    assert any(c.islower() for c in pw)
    assert any(c.isupper() for c in pw)
    assert any(c.isdigit() for c in pw)
    assert any(c in "!@#$%^&*-_" for c in pw)
