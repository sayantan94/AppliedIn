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


def is_workday(url: str) -> bool:
    from .ats import detect_ats
    from .claude_chrome import direct_board_url

    return "workday" in (detect_ats(url), detect_ats(direct_board_url(url) or ""))


def plan(url: str, company: str, facts: dict, secrets: Any) -> dict | None:
    """The login this run may create, or None. A saved login always wins, and the
    account uses the same email the application goes out under."""
    from .credentials import get_login

    if not enabled() or not is_workday(url) or get_login(company, secrets):
        return None
    email = str(facts.get("Email") or facts.get("Email address") or "").strip()
    if "@" not in email:
        log.info("workday account not offered for %s: no email", company)
        return None
    return {"username": email, "password": password(secrets)}


def remember(company: str, account: dict | None, result: dict, secrets: Any) -> bool:
    """Save the login once the run shows it works. Saving before the run would turn
    a failed sign-up into a "saved login" that is never allowed to sign up again."""
    if not account:
        return False
    state = str(result.get("account") or "").lower()
    if state not in ("created", "signed_in") and result.get("status") != "applied":
        return False
    from .credentials import portal_secret

    secrets.put_json(portal_secret(company), {**account, "source": SOURCE})
    log.info("saved the Workday login for %s (%s)", company, state or "applied")
    return True


def task_block(account: dict) -> str:
    return f"""
WORKDAY ACCOUNT — you may sign in, or create the account, with exactly these
The owner has allowed this. Use these and no others:
  email:    {account['username']}
  password: {account['password']}

1. If the portal is already signed in as a DIFFERENT email, sign out first. The
   browser is shared with other instances, and that session is not this one's.
   Then on "Sign In", sign in with these first. The account may already exist.
2. If Workday says there is no account for that email, choose "Create Account".
   Use the same email, type the password into both password fields, tick the
   consent box it requires, and create it. Then carry on with the application.
3. If sign-in fails because the PASSWORD is wrong, the account exists with a
   password the owner set. Do NOT reset it and do NOT create an account under
   another address: stop with outcome "blocked" and blocked_by "login".
4. If Workday wants the email verified before it will continue, stop with outcome
   "needs_owner" and ask the owner to click the verification link.
5. If Chrome offers to save the password, accept it.

Add one more key to the final JSON: "account": "created" | "signed_in" | "none".
"""
