"""Shared fixtures: moto-backed DynamoDB tables matching the CDK schema."""

from __future__ import annotations

import boto3
import pytest
from appliedin_core.storage.tracking import JD_HASH_INDEX, STATUS_INDEX
from moto import mock_aws


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    """Give boto3/moto a region + dummy creds so clients construct in tests.

    Mirrors the real Lambda/Fargate runtime, which sets AWS_REGION for us.
    """
    for key, value in {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
    }.items():
        monkeypatch.setenv(key, value)
    yield


@pytest.fixture
def aws():
    with mock_aws():
        yield


def make_applications_table(name: str = "applications") -> None:
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "jd_hash", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": JD_HASH_INDEX,
                "KeySchema": [{"AttributeName": "jd_hash", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": STATUS_INDEX,
                "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )


def make_answer_bank_table(name: str = "answer_bank") -> None:
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
    )
