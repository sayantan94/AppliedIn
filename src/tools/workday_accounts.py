"""Workday accounts created on the owner's behalf, when they have opted in.

Every Workday tenant is its own account, so each new employer used to stop at
"a login is needed". With the setting on, the apply session signs in or signs up
with the profile's email and the owner's chosen password, and the login is saved
for the next application to that tenant. Off by default.
"""

from __future__ import annotations

import re
from typing import Any

from core.logging import get_logger

log = get_logger(__name__)

DEFAULT_PASSWORD = "QazPlm123#"
_FLAG = "workday_accounts"
_PASSWORD_SECRET = "workday/default_password"
_LEDGER = "workday/accounts"
SOURCE = "workday-auto"


def enabled() -> bool:
    from core import flags

    return flags.get_flag(_FLAG, "no") == "yes"


def set_enabled(on: bool) -> None:
    from core import flags

    flags.set_flag(_FLAG, "yes" if on else "no")


def password(secrets: Any) -> str:
    try:
        stored = (secrets.get_json(_PASSWORD_SECRET) or {}).get("password", "")
    except Exception:  # noqa: BLE001
        log.debug("could not read the Workday password", exc_info=True)
        stored = ""
    return stored or DEFAULT_PASSWORD


def is_custom_password(secrets: Any) -> bool:
    return password(secrets) != DEFAULT_PASSWORD


def password_problem(pw: str) -> str:
    """What Workday's default password policy would reject, or ''."""
    if len(pw) < 8:
        return "at least 8 characters"
    for rx, need in ((r"[A-Z]", "an uppercase letter"), (r"[a-z]", "a lowercase letter"),
                     (r"\d", "a number"), (r"[^A-Za-z0-9]", "a special character")):
        if not re.search(rx, pw):
            return need
    return ""


def set_password(secrets: Any, pw: str) -> None:
    pw = (pw or "").strip()
    if problem := password_problem(pw):
        raise ValueError(f"Workday needs {problem} in the password")
    secrets.put_json(_PASSWORD_SECRET, {"password": pw})


def reset_password(secrets: Any) -> None:
    secrets.put_json(_PASSWORD_SECRET, {})


def tenant(url: str) -> str:
    """The Workday host an account belongs to, or '' off Workday. Accounts are
    per tenant, not per watchlist company: a Splunk posting signs in on Cisco's."""
    from urllib.parse import urlparse

    from .ats import detect_ats
    from .claude_chrome import direct_board_url

    for u in (url, direct_board_url(url) or ""):
        if detect_ats(u) == "workday":
            return (urlparse(u).hostname or "").lower()
    return ""


def accounts(secrets: Any) -> dict:
    """Every Workday login this instance has made or used, by tenant host."""
    try:
        return secrets.get_json(_LEDGER) or {}
    except Exception:  # noqa: BLE001
        log.debug("could not read the Workday ledger", exc_info=True)
        return {}


def plan(url: str, company: str, facts: dict, secrets: Any) -> dict | None:
    """The login for this run, or None. A known login is signed in with and never
    replaced; a new account is made only with the setting on, under the email
    the application goes out with."""
    from .credentials import get_login

    host = tenant(url)
    if not host:
        return None
    known = accounts(secrets).get(host) or get_login(company, secrets)
    if known and known.get("username") and known.get("password"):
        return {"username": known["username"], "password": known["password"],
                "tenant": host, "may_create": False}
    if not enabled():
        return None
    email = str(facts.get("Email") or facts.get("Email address") or "").strip()
    if "@" not in email:
        log.info("workday account not offered for %s: no email", company)
        return None
    return {"username": email, "password": password(secrets), "tenant": host,
            "may_create": True}


def remember(company: str, account: dict | None, result: dict, secrets: Any) -> bool:
    """Record the login once the run shows it works. Saving before the run would
    turn a failed sign-up into a known login that is never allowed to sign up."""
    if not account:
        return False
    state = str(result.get("account") or "").lower()
    if state not in ("created", "signed_in") and result.get("status") != "applied":
        return False
    if state == "none":
        # Applied, but as a guest or through another sign-in: no account exists
        # under this login. Recording one makes plan() answer "sign in, never
        # create" for this tenant from then on, and every later job there stops
        # at a login that was never made.
        return False
    from datetime import UTC, datetime

    from .credentials import portal_secret

    now = datetime.now(UTC).isoformat(timespec="seconds")
    login = {"username": account["username"], "password": account["password"]}
    # Read directly, not through accounts(): that swallows a read error into {},
    # and writing {} plus this one entry back would erase every other tenant.
    ledger = secrets.get_json(_LEDGER) or {}
    prev = ledger.get(account["tenant"]) or {}
    ledger[account["tenant"]] = {
        **login, "company": company,
        "created": prev.get("created") or (now if state == "created" else ""),
        "last_used": now}
    secrets.put_json(_LEDGER, ledger)
    secrets.put_json(portal_secret(company), {**login, "source": SOURCE})
    log.info("recorded the Workday login for %s on %s (%s)",
             company, account["tenant"], state or "applied")
    return True


def task_block(account: dict) -> str:
    login = f"""  email:    {account['username']}
  password: {account['password']}"""
    common = """If the portal is already signed in as a DIFFERENT email, sign out first. The
browser is shared with other instances, and that session is not this one's.
If Workday wants the email verified before it will continue, stop with outcome
"needs_owner" and ask the owner to click the verification link.
If sign-in fails because the PASSWORD is wrong, do NOT reset it and do NOT
create an account under another address: stop with outcome "blocked" and
blocked_by "login".
If Chrome offers to save the password, accept it.

Add one more key to the final JSON: "account": "created" | "signed_in" | "none".
"""
    if not account.get("may_create"):
        return f"""
WORKDAY ACCOUNT — an account already exists here. Sign in with exactly these:
{login}

Choose "Sign In", not "Create Account": this account was made on an earlier
application, and a second sign-up under the same email fails.
{common}"""
    return f"""
WORKDAY ACCOUNT — sign in, or create the account, with exactly these
The owner has allowed this. Use these and no others:
{login}

Choose "Sign In" first; the account may already exist. If Workday says there is
no account for that email, choose "Create Account", use the same email, type the
password into both password fields, tick the consent box it requires, and create
it. Then carry on with the application.
{common}"""
