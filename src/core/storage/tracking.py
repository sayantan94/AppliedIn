"""The ``applications`` DynamoDB table — the pipeline's memory.

Dedup and the daily cap are enforced by conditional writes, never
read-then-write, so overlapping poll cycles and concurrent apply tasks
cannot race (HLD premise 5 + guardrail 1).
"""

from __future__ import annotations

from typing import Any

import boto3
from botocore.exceptions import ClientError

from ..models import JobRecord, Status
from .base import AbstractTracking

JD_HASH_INDEX = "jd_hash-index"
STATUS_INDEX = "status-index"
_CAP_PK = "meta#dailycap"


class TrackingStore(AbstractTracking):
    def __init__(self, table_name: str, *, region: str | None = None) -> None:
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def put_new(self, job: JobRecord, *, status: Status = Status.FOUND) -> bool:
        """Conditionally create a new row. Returns False if the pk already exists.

        The ``attribute_not_exists(pk)`` condition is the dedup guarantee: a
        second poll cycle that rediscovers the same posting is a no-op.
        """
        item: dict[str, Any] = {
            "pk": job.pk,
            "jd_hash": job.jd_hash,
            "status": status.value,
            "company": job.company,
            "job_id": job.job_id,
            "title": job.title,
            "jd_url": job.jd_url,
            "location": job.location,
            "ats": job.ats,
            "attempts": 0,
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(pk)",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def get(self, pk: str) -> dict | None:
        return self._table.get_item(Key={"pk": pk}).get("Item")

    def set_status(self, pk: str, status: Status, **attrs: Any) -> None:
        names = {"#s": "status"}
        values = {":s": status.value}
        sets = ["#s = :s"]
        for i, (key, value) in enumerate(attrs.items()):
            names[f"#a{i}"] = key
            values[f":a{i}"] = value
            sets.append(f"#a{i} = :a{i}")
        self._table.update_item(
            Key={"pk": pk},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def find_by_jd_hash(self, jd_hash: str) -> str | None:
        """Return the pk of an existing row with this jd_hash, or None."""
        resp = self._table.query(
            IndexName=JD_HASH_INDEX,
            KeyConditionExpression=boto3.dynamodb.conditions.Key("jd_hash").eq(jd_hash),
            Limit=1,
        )
        items = resp.get("Items", [])
        return items[0]["pk"] if items else None

    def query_status(self, status: Status) -> list[dict]:
        resp = self._table.query(
            IndexName=STATUS_INDEX,
            KeyConditionExpression=boto3.dynamodb.conditions.Key("status").eq(status.value),
        )
        return resp.get("Items", [])

    def all(self) -> list[dict]:
        rows, kwargs = [], {}
        while True:
            resp = self._table.scan(**kwargs)
            rows += [r for r in resp.get("Items", []) if not str(r.get("pk", "")).startswith("meta#")]
            if "LastEvaluatedKey" not in resp:
                return rows
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def try_increment_daily_cap(self, date_str: str, cap: int) -> bool:
        """Atomically reserve one submit slot for ``date_str``.

        Returns True if the increment stayed under ``cap`` (slot reserved),
        False if the cap is already reached. Called immediately before setting
        ``submitting`` so concurrent apply tasks cannot overshoot.
        """
        try:
            self._table.update_item(
                Key={"pk": f"{_CAP_PK}#{date_str}"},
                UpdateExpression="SET #c = if_not_exists(#c, :zero) + :one",
                ConditionExpression="attribute_not_exists(#c) OR #c < :cap",
                ExpressionAttributeNames={"#c": "count"},
                ExpressionAttributeValues={":zero": 0, ":one": 1, ":cap": cap},
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
