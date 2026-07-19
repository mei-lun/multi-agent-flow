"""校验 Git 权威分配并协调完整本地任务生命周期的接口。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from maf_contracts.coordination import CoordinationTask
from maf_contracts.job import AttemptResult
from maf_runner.execute_attempt import execute_attempt


def _assignment(task: CoordinationTask) -> dict[str, Any]:
    value = task.get("assignment")
    if not isinstance(value, dict):
        raise ValueError("task has no authoritative assignment")
    return value


def _same_assignment(task: CoordinationTask, node_id: str, assignment_id: str, epoch: int) -> bool:
    current = task.get("assignment") or {}
    return (
        current.get("node_id") == node_id
        and current.get("assignment_id") == assignment_id
        and int(current.get("assignment_epoch", 0)) == epoch
    )


def _failure(task: CoordinationTask, code: str, message: str, *, status: str = "FAILED") -> AttemptResult:
    assignment = task.get("assignment") or {}
    return AttemptResult(
        task_id=str(task.get("task_id", "")),
        assignment_id=str(assignment.get("assignment_id", "")),
        assignment_epoch=int(assignment.get("assignment_epoch", 0)),
        status=status,
        output_paths=[], execution_summary=message, self_check=[], known_risks=[],
        remaining_items=[], model_usage={}, tool_usage={}, workspace_result=None,
        error={"code": code, "message": message, "retryable": code in {"STALE_ASSIGNMENT", "PUSH_FAILED"}},
    )


class JobExecutor:
    """执行一个 assignment epoch，并在最后一次权威检查后写入一次提交事件。

    所有协作者均可注入，默认实现仍可被 CLI/daemon 配置；关键是 epoch 检查在容器启动前
    和任务分支 push 前各执行一次，旧 epoch 绝不能产生成功提交事件。
    """

    def __init__(self, *, node_id: str, git_client=None, workspace_manager=None,
                 docker_manager=None, network_applier=None, attempt_executor=None,
                 progress_reporter=None) -> None:
        self.node_id = node_id
        self.git = git_client
        self.workspace = workspace_manager
        self.docker = docker_manager
        self.network = network_applier
        self.attempt = attempt_executor
        self.progress = progress_reporter

    async def _current(self, task: CoordinationTask) -> CoordinationTask:
        if self.git is None or not hasattr(self.git, "current_task"):
            return task
        return await self.git.current_task(str(task["task_id"]))

    async def _prepare_workspace(self, task: CoordinationTask, envelope: dict[str, Any]) -> str:
        spec = envelope.get("workspace") or {}
        if self.workspace is None:
            return str(spec.get("repository_path") or ".")
        kind = str(spec.get("kind", "GENERIC")).upper()
        if kind == "GIT":
            return await self.workspace.prepare(
                str(task["task_id"]), str(spec.get("repository_path", "")),
                str(spec.get("base_commit", "")), str(spec.get("expected_tree_hash", "")),
                list(spec.get("writable_subpaths", [])),
            )
        return await self.workspace.prepare(
            str(task["task_id"]), str(spec.get("input_bundle_ref", "")),
            list(spec.get("writable_subpaths", [])),
        )

    async def execute(self, task: CoordinationTask) -> AttemptResult:
        try:
            assignment = _assignment(task)
            epoch = int(assignment["assignment_epoch"])
            assignment_id = str(assignment["assignment_id"])
            if assignment.get("node_id") != self.node_id:
                return _failure(task, "NOT_OWNER", "assignment belongs to another node")
            current = await self._current(task)
            if not _same_assignment(current, self.node_id, assignment_id, epoch):
                return _failure(task, "STALE_ASSIGNMENT", "assignment changed before execution", status="CANCELLED")
            envelope = (task.get("requirements") or {}).get("envelope")
            if not isinstance(envelope, dict):
                return _failure(task, "INVALID_ENVELOPE", "task requirements do not contain a dispatch envelope")
            workspace_path = await self._prepare_workspace(task, envelope)
            network_handle = None
            container = None
            try:
                if self.network is not None:
                    network_handle = await self.network.prepare(envelope.get("network_policy_ref") or {})
                if self.docker is not None:
                    profile = envelope.get("resource_profile") or {}
                    container = await self.docker.create(
                        str(task["task_id"]), str(envelope.get("docker_image_digest", "")),
                        profile, workspace_path, network_handle or {"network_mode": "none"},
                    )
                    await self.docker.start(container)
                if self.attempt is not None:
                    result = await self.attempt.execute(envelope, workspace_path)
                else:
                    result = await execute_attempt(envelope, workspace_path)
                if self.workspace is not None and hasattr(self.workspace, "collect"):
                    collected = await self.workspace.collect(workspace_path)
                    result["workspace_result"] = collected
                if result.get("status") != "SUBMITTED":
                    return result
                latest = await self._current(task)
                if not _same_assignment(latest, self.node_id, assignment_id, epoch):
                    return _failure(task, "STALE_ASSIGNMENT", "assignment changed before submission", status="CANCELLED")
                if self.git is not None and hasattr(self.git, "push_task_branch"):
                    await self.git.push_task_branch(latest, workspace_path)
                wr = result.get("workspace_result") or {}
                event_key = f"{task['task_id']}:{assignment_id}:{epoch}:{wr.get('head_commit', '')}"
                event_id = "evt-submission-" + hashlib.sha256(event_key.encode()).hexdigest()[:32]
                event = {
                    "schema_version": 1, "event_id": event_id, "event_type": "SUBMISSION_CREATED",
                    "node_id": self.node_id, "task_id": task["task_id"],
                    "assignment_id": assignment_id, "assignment_epoch": epoch,
                    "based_on_control_commit": str(assignment.get("based_on_control_commit", "")),
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "payload": {**wr, "output_paths": result.get("output_paths", []),
                                "execution_summary": result.get("execution_summary", "")},
                }
                if self.git is not None and hasattr(self.git, "append_event"):
                    await self.git.append_event(event)
                return result
            finally:
                if self.progress is not None and hasattr(self.progress, "flush"):
                    await self.progress.flush()
                if container is not None and self.docker is not None:
                    try:
                        await self.docker.stop(container, 10)
                    finally:
                        await self.docker.remove(container)
                if network_handle is not None and self.network is not None:
                    await self.network.cleanup(network_handle)
                if self.workspace is not None and hasattr(self.workspace, "cleanup"):
                    await self.workspace.cleanup(workspace_path)
        except Exception as exc:
            return _failure(task, "JOB_FAILED", str(exc))


_default_executor: JobExecutor | None = None


def configure_job_executor(executor: JobExecutor) -> None:
    global _default_executor
    _default_executor = executor


async def execute_job(task: CoordinationTask) -> AttemptResult:
    executor = _default_executor
    if executor is None:
        return _failure(task, "NOT_CONFIGURED", "job executor is not configured")
    return await executor.execute(task)


__all__ = ["JobExecutor", "configure_job_executor", "execute_job"]
