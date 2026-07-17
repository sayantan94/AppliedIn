"""Dispatcher Lambda — apply-queue SQS -> one Fargate task per job.

Task-per-job launch IS the IP rotation strategy: every ``run_task`` goes to a
public subnet with ``assignPublicIp=ENABLED`` so each application gets a fresh
public IP from AWS's pool (no NAT gateway, no shared egress IP). A
max-concurrent lid protects capacity; over the lid we RAISE so SQS redelivers
the message on a later attempt instead of dropping the job.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import boto3
from core.config import get_settings
from core.logging import get_logger

log = get_logger(__name__)

TASK_MODE = "apply"


class CapacityLimit(Exception):
    """Raised when RUNNING tasks >= the lid; SQS will redeliver the message."""


class RunTaskFailure(Exception):
    """Raised when ECS reports failures for run_task; SQS will redeliver."""


@dataclass(frozen=True)
class DispatcherConfig:
    cluster: str
    task_definition: str
    subnets: list[str] = field(default_factory=list)
    security_group: str = ""
    container: str = "worker"
    max_concurrent: int = 3

    @classmethod
    def from_env(cls) -> DispatcherConfig:
        return cls(
            cluster=os.environ.get("APPLIEDIN_ECS_CLUSTER", "appliedin"),
            task_definition=os.environ.get("APPLIEDIN_TASK_DEFINITION", "appliedin-worker"),
            subnets=[
                s.strip()
                for s in os.environ.get("APPLIEDIN_SUBNETS", "").split(",")
                if s.strip()
            ],
            security_group=os.environ.get("APPLIEDIN_SECURITY_GROUP", ""),
            container=os.environ.get("APPLIEDIN_WORKER_CONTAINER", "worker"),
            max_concurrent=int(os.environ.get("APPLIEDIN_MAX_CONCURRENT", "3")),
        )


def count_running(ecs, cluster: str) -> int:
    """Count RUNNING tasks in the cluster (follows pagination)."""
    kwargs: dict = {"cluster": cluster, "desiredStatus": "RUNNING"}
    total = 0
    while True:
        resp = ecs.list_tasks(**kwargs)
        total += len(resp.get("taskArns", []))
        token = resp.get("nextToken")
        if not token:
            return total
        kwargs["nextToken"] = token


def dispatch_one(pk: str, *, ecs, config: DispatcherConfig) -> str:
    """Lid check + run_task for one job; returns the launched task ARN."""
    running = count_running(ecs, config.cluster)
    if running >= config.max_concurrent:
        raise CapacityLimit(
            f"{running} tasks RUNNING >= lid {config.max_concurrent}; "
            f"leaving {pk!r} for redelivery"
        )

    resp = ecs.run_task(
        cluster=config.cluster,
        taskDefinition=config.task_definition,
        launchType="FARGATE",
        count=1,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": config.subnets,
                "securityGroups": [config.security_group],
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": config.container,
                    "environment": [
                        {"name": "APPLIEDIN_JOB_PK", "value": pk},
                        {"name": "APPLIEDIN_TASK_MODE", "value": TASK_MODE},
                    ],
                }
            ]
        },
    )

    failures = resp.get("failures") or []
    if failures or not resp.get("tasks"):
        raise RunTaskFailure(f"run_task failed for {pk!r}: {failures}")

    task_arn = resp["tasks"][0].get("taskArn", "")
    log.info("launched apply task for %s: %s", pk, task_arn)
    return task_arn


def handler(event, context, *, ecs=None, config=None):  # noqa: ANN001 - Lambda signature
    """Per SQS record: launch one Fargate apply task for the job's pk.

    Raises on capacity-lid or RunTask failure so SQS redelivers the message.
    """
    if ecs is None:
        ecs = boto3.client("ecs", region_name=get_settings().aws_region)
    if config is None:
        config = DispatcherConfig.from_env()

    launched = []
    for record in event.get("Records", []):
        pk = json.loads(record["body"])["pk"]
        launched.append(dispatch_one(pk, ecs=ecs, config=config))

    log.info("dispatched %d apply task(s)", len(launched))
    return {"launched": launched}
