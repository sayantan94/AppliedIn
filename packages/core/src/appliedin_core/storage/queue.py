"""SQS enqueue helper — tailor-queue and apply-queue.

Message bodies are JSON dicts; the two-queue split buys per-job retry
semantics and keeps discovery a fast, dumb poller (HLD architecture note).
"""

from __future__ import annotations

import json

import boto3


class Queue:
    def __init__(self, *, region: str | None = None) -> None:
        self._sqs = boto3.client("sqs", region_name=region)

    def enqueue(self, queue_url: str, body: dict) -> str:
        resp = self._sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(body))
        return resp["MessageId"]
