from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    for key in (
        "AWS_DEFAULT_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.setenv(key, "us-east-1" if key == "AWS_DEFAULT_REGION" else "testing")
    yield
