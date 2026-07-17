"""Dispatcher unit tests with a fake ECS client (moto's ECS run_task is patchy).

The fake records run_task calls and returns a controllable RUNNING count so the
tests exercise the lid check and the exact RunTask request shape.
"""

from __future__ import annotations

import json

import pytest
from dispatcher import handler as h


class FakeEcs:
    def __init__(self, running: int = 0, failures: list | None = None):
        self.running = running
        self.failures = failures or []
        self.run_task_calls: list[dict] = []
        self.list_tasks_calls: list[dict] = []

    def list_tasks(self, **kwargs):
        self.list_tasks_calls.append(kwargs)
        return {"taskArns": [f"arn:aws:ecs:task/{i}" for i in range(self.running)]}

    def run_task(self, **kwargs):
        self.run_task_calls.append(kwargs)
        if self.failures:
            return {"tasks": [], "failures": self.failures}
        return {"tasks": [{"taskArn": "arn:aws:ecs:task/new"}], "failures": []}


def _config(**overrides) -> h.DispatcherConfig:
    defaults = dict(
        cluster="appliedin-cluster",
        task_definition="appliedin-worker:7",
        subnets=["subnet-aaa", "subnet-bbb"],
        security_group="sg-123",
        container="worker",
        max_concurrent=3,
    )
    defaults.update(overrides)
    return h.DispatcherConfig(**defaults)


def test_dispatch_one_runs_task_with_public_ip_and_env_override():
    ecs = FakeEcs(running=0)

    arn = h.dispatch_one("acme#123", ecs=ecs, config=_config())

    assert arn == "arn:aws:ecs:task/new"
    assert len(ecs.run_task_calls) == 1
    call = ecs.run_task_calls[0]
    assert call["cluster"] == "appliedin-cluster"
    assert call["taskDefinition"] == "appliedin-worker:7"
    assert call["launchType"] == "FARGATE"
    assert call["count"] == 1

    vpc = call["networkConfiguration"]["awsvpcConfiguration"]
    assert vpc["assignPublicIp"] == "ENABLED"
    assert vpc["subnets"] == ["subnet-aaa", "subnet-bbb"]
    assert vpc["securityGroups"] == ["sg-123"]

    (override,) = call["overrides"]["containerOverrides"]
    assert override["name"] == "worker"
    env = {e["name"]: e["value"] for e in override["environment"]}
    assert env["APPLIEDIN_JOB_PK"] == "acme#123"
    assert env["APPLIEDIN_TASK_MODE"] == "apply"


def test_dispatch_one_raises_at_lid_without_launching():
    ecs = FakeEcs(running=3)

    with pytest.raises(h.CapacityLimit):
        h.dispatch_one("acme#123", ecs=ecs, config=_config(max_concurrent=3))

    assert ecs.run_task_calls == []


def test_dispatch_one_raises_over_lid():
    ecs = FakeEcs(running=5)

    with pytest.raises(h.CapacityLimit):
        h.dispatch_one("acme#123", ecs=ecs, config=_config(max_concurrent=3))

    assert ecs.run_task_calls == []


def test_dispatch_one_raises_on_run_task_failures():
    ecs = FakeEcs(running=0, failures=[{"reason": "RESOURCE:MEMORY"}])

    with pytest.raises(h.RunTaskFailure):
        h.dispatch_one("acme#123", ecs=ecs, config=_config())


def test_count_running_filters_on_running_status():
    ecs = FakeEcs(running=2)

    assert h.count_running(ecs, "appliedin-cluster") == 2
    assert ecs.list_tasks_calls == [
        {"cluster": "appliedin-cluster", "desiredStatus": "RUNNING"}
    ]


def test_count_running_follows_pagination():
    class PagedEcs:
        def __init__(self):
            self.calls = []

        def list_tasks(self, **kwargs):
            self.calls.append(kwargs)
            if "nextToken" not in kwargs:
                return {"taskArns": ["a", "b"], "nextToken": "t1"}
            return {"taskArns": ["c"]}

    ecs = PagedEcs()
    assert h.count_running(ecs, "c1") == 3
    assert ecs.calls[1]["nextToken"] == "t1"


def test_handler_dispatches_each_record():
    ecs = FakeEcs(running=0)
    event = {
        "Records": [
            {"body": json.dumps({"pk": "acme#1"})},
            {"body": json.dumps({"pk": "globex#2"})},
        ]
    }

    result = h.handler(event, None, ecs=ecs, config=_config())

    assert len(ecs.run_task_calls) == 2
    assert len(result["launched"]) == 2
    pks = [
        {e["name"]: e["value"] for e in c["overrides"]["containerOverrides"][0]["environment"]}[
            "APPLIEDIN_JOB_PK"
        ]
        for c in ecs.run_task_calls
    ]
    assert pks == ["acme#1", "globex#2"]


def test_handler_propagates_lid_so_sqs_redelivers():
    ecs = FakeEcs(running=3)
    event = {"Records": [{"body": json.dumps({"pk": "acme#1"})}]}

    with pytest.raises(h.CapacityLimit):
        h.handler(event, None, ecs=ecs, config=_config(max_concurrent=3))

    assert ecs.run_task_calls == []


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("APPLIEDIN_ECS_CLUSTER", "c1")
    monkeypatch.setenv("APPLIEDIN_TASK_DEFINITION", "td:9")
    monkeypatch.setenv("APPLIEDIN_SUBNETS", "subnet-1, subnet-2,")
    monkeypatch.setenv("APPLIEDIN_SECURITY_GROUP", "sg-9")
    monkeypatch.setenv("APPLIEDIN_WORKER_CONTAINER", "apply-worker")
    monkeypatch.setenv("APPLIEDIN_MAX_CONCURRENT", "5")

    cfg = h.DispatcherConfig.from_env()

    assert cfg.cluster == "c1"
    assert cfg.task_definition == "td:9"
    assert cfg.subnets == ["subnet-1", "subnet-2"]
    assert cfg.security_group == "sg-9"
    assert cfg.container == "apply-worker"
    assert cfg.max_concurrent == 5


def test_config_from_env_defaults(monkeypatch):
    for var in (
        "APPLIEDIN_ECS_CLUSTER",
        "APPLIEDIN_TASK_DEFINITION",
        "APPLIEDIN_SUBNETS",
        "APPLIEDIN_SECURITY_GROUP",
        "APPLIEDIN_WORKER_CONTAINER",
        "APPLIEDIN_MAX_CONCURRENT",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = h.DispatcherConfig.from_env()

    assert cfg.subnets == []
    assert cfg.max_concurrent == 3
