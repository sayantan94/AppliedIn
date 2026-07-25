"""Secrets Manager helper.

Holds the logins you saved for employer portals — the sessions `appliedin login`
captures, so an apply behind a sign-in wall can reuse one instead of asking you
again. Nothing here is ever created by the pipeline: it only reads credentials
you chose to store.
"""

from __future__ import annotations

import json

import boto3
from botocore.exceptions import ClientError

from .base import AbstractSecrets


class SecretsClient(AbstractSecrets):
    def __init__(self, *, region: str | None = None) -> None:
        self._sm = boto3.client("secretsmanager", region_name=region)

    def get_json(self, name: str) -> dict | None:
        try:
            resp = self._sm.get_secret_value(SecretId=name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                return None
            raise
        return json.loads(resp["SecretString"])

    def put_json(self, name: str, obj: dict) -> None:
        payload = json.dumps(obj)
        try:
            self._sm.put_secret_value(SecretId=name, SecretString=payload)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                self._sm.create_secret(Name=name, SecretString=payload)
            else:
                raise
