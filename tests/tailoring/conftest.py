from __future__ import annotations

import boto3
import pytest
from core.storage.tracking import JD_HASH_INDEX, STATUS_INDEX
from moto import mock_aws


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


@pytest.fixture
def aws():
    with mock_aws():
        yield


@pytest.fixture
def applications_table(aws):
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="applications",
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
    return "applications"


@pytest.fixture
def artifacts_bucket(aws):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="artifacts")
    return "artifacts"
