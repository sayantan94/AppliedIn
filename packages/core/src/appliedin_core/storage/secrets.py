"""Secrets Manager helper.

Holds portal credentials (including auto-signup passwords, written BEFORE a
signup form is submitted so a crash can never orphan an account), the WhatsApp
token + app secret, and the Gmail OAuth refresh token.
"""

from __future__ import annotations

import json

import boto3
from botocore.exceptions import ClientError


class SecretsClient:
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
