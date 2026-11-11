"""Workday account creation: off unless switched on, never over a saved login,
and the login is kept only once the run shows it works."""

from __future__ import annotations

import pytest

from tools import workday_accounts as wa
from tools.credentials import portal_secret

WD = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Engineer_R123"
FACTS = {"Email": "me+acme@example.org"}


class Secrets:
    def __init__(self):
        self.d = {}

    def get_json(self, name):
        return self.d.get(name)

    def put_json(self, name, obj):
        self.d[name] = obj


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(wa, "enabled", lambda: True)


def test_off_by_default_offers_nothing(monkeypatch):
    monkeypatch.setattr("core.flags.get_flag", lambda name, default="": default)
    assert wa.plan(WD, "Acme", FACTS, Secrets()) is None


def test_on_uses_profile_email_and_default_password(on):
    assert wa.plan(WD, "Acme", FACTS, Secrets()) == {
        "username": "me+acme@example.org", "password": "QazPlm123#",
        "tenant": "acme.wd5.myworkdayjobs.com", "may_create": True}


def test_not_offered_off_workday(on):
    assert wa.plan("https://jobs.ashbyhq.com/acme/1", "Acme", FACTS, Secrets()) is None


def test_saved_login_is_signed_in_with_never_recreated(on):
    s = Secrets()
    s.put_json(portal_secret("Acme"), {"username": "a@b.io", "password": "Mine123!x"})
    got = wa.plan(WD, "Acme", FACTS, s)
    assert got["username"] == "a@b.io" and got["may_create"] is False
    assert "Create Account\", use" not in wa.task_block(got)


def test_custom_password_used_and_validated(on):
    s = Secrets()
    wa.set_password(s, "Better#Pass9")
    assert wa.plan(WD, "Acme", FACTS, s)["password"] == "Better#Pass9"
    with pytest.raises(ValueError):
        wa.set_password(s, "weakpass")


@pytest.mark.parametrize("result,kept", [
    ({"status": "gate", "account": "created"}, True),
    ({"status": "failed", "account": "signed_in"}, True),
    ({"status": "applied"}, True),
    ({"status": "failed", "account": "none"}, False),
    ({"status": "failed"}, False),
])
def test_login_saved_only_when_proven(result, kept):
    s = Secrets()
    acct = {"username": "me@example.org", "password": "QazPlm123#",
            "tenant": "acme.wd5.myworkdayjobs.com", "may_create": True}
    assert wa.remember("Acme", acct, result, s) is kept
    assert bool(s.get_json(portal_secret("Acme"))) is kept


def test_password_and_logins_stay_in_their_own_instance(tmp_path):
    """Each `--port` instance has its own local_dir, so its own secrets file. A
    password set on 8788 must not change what 8787 signs up with."""
    from core.storage.local import FileSecrets

    main, other = FileSecrets(tmp_path / "a" / "secrets.json"), FileSecrets(tmp_path / "b" / "secrets.json")
    wa.set_password(other, "Other#Inst4nce")
    wa.remember("Acme", {"username": "x@y.io", "password": "Other#Inst4nce",
                         "tenant": "acme.wd5.myworkdayjobs.com"},
                {"status": "applied"}, other)
    assert wa.password(main) == wa.DEFAULT_PASSWORD
    assert main.get_json(portal_secret("Acme")) is None


def test_removing_the_custom_password_restores_the_default():
    s = Secrets()
    wa.set_password(s, "Better#Pass9")
    wa.reset_password(s)
    assert wa.password(s) == wa.DEFAULT_PASSWORD and not wa.is_custom_password(s)


def test_a_created_account_is_signed_into_next_time_even_with_the_setting_off(monkeypatch):
    """The point of tracking: the second application to a tenant signs in."""
    s = Secrets()
    monkeypatch.setattr(wa, "enabled", lambda: True)
    first = wa.plan(WD, "Acme", FACTS, s)
    wa.remember("Acme", first, {"status": "gate", "account": "created"}, s)
    monkeypatch.setattr(wa, "enabled", lambda: False)
    again = wa.plan(WD.replace("R123", "R456"), "Acme", FACTS, s)
    assert again["may_create"] is False and again["username"] == "me+acme@example.org"
    rec = wa.accounts(s)["acme.wd5.myworkdayjobs.com"]
    assert rec["created"] and rec["company"] == "Acme"


def test_login_follows_the_tenant_not_the_company(on):
    """A Splunk posting on Cisco's tenant uses the Cisco account."""
    s = Secrets()
    cisco = "https://cisco.wd5.myworkdayjobs.com/Cisco_Careers/job/X_1"
    wa.remember("Splunk", wa.plan(cisco, "Splunk", FACTS, s), {"status": "applied"}, s)
    got = wa.plan(cisco.replace("X_1", "Y_2"), "Cisco", {"Email": "other@x.io"}, s)
    assert got["username"] == "me+acme@example.org" and got["may_create"] is False


def test_the_signin_prompt_forbids_signing_up():
    block = wa.task_block({"username": "a@b.io", "password": "P", "may_create": False})
    assert "Sign In\", not \"Create Account\"" in block
