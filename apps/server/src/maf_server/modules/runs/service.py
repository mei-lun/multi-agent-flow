"""Run 和人工命令的应用接口。调度推进由 SchedulerService 完成。"""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import inspect
import json
from typing import AsyncIterator, Protocol
import uuid
from maf_contracts.common import ActorContext
from maf_domain.errors import ArgumentError, NotFoundError, ValidationError, VersionConflictError
from .repository import InMemoryRunRepository
from .schemas import *


class RunService(Protocol):
    async def start_run(self, actor: ActorContext, project_id: str, request: StartRunRequest) -> RunView:
        """创建可恢复 Run 并请求 Scheduler 启动。

        实现顺序：校验权限和项目 ACTIVE；确认 Workflow/Role/Skill/Policy 均为已发布精确
        版本；确认项目输入与仓库绑定属于项目；限制不得超过项目和系统上限；检查幂等键；
        构建完整 Run Snapshot Artifact；在事务中创建 Run/事件；提交后调用 Scheduler。
        Scheduler 启动失败不删除 Run，由恢复任务继续。成功返回 CREATED/RUNNING 投影。
        """
        ...


class RunServiceImpl:
    """Concrete Run service with immutable snapshots and durable command ordering."""

    def __init__(
        self,
        repository: InMemoryRunRepository | None = None,
        *,
        project_source: object | None = None,
        workflow_source: object | None = None,
        input_source: object | None = None,
        repository_source: object | None = None,
        reference_source: object | None = None,
        scheduler: object | None = None,
        control_signaler: object | None = None,
        control_base_commit: str = "",
    ) -> None:
        self.repository = repository or InMemoryRunRepository()
        self.project_source = project_source
        self.workflow_source = workflow_source
        self.input_source = input_source
        self.repository_source = repository_source
        self.reference_source = reference_source
        self.scheduler = scheduler
        self.control_signaler = control_signaler
        self.control_base_commit = control_base_commit

    @staticmethod
    async def _call(source: object | None, names: tuple[str, ...], *args: object) -> object | None:
        if source is None:
            return None
        for name in names:
            method = getattr(source, name, None)
            if method is not None:
                result = method(*args)
                return await result if inspect.isawaitable(result) else result
        return None

    @staticmethod
    def _actor_id(actor: ActorContext) -> str:
        value = actor.get("user_id") if isinstance(actor, dict) else None
        if not value:
            raise ArgumentError("actor user_id is required")
        return str(value)

    async def start_run(
        self, actor: ActorContext, project_id: str, request: StartRunRequest
    ) -> RunView:
        actor_id = self._actor_id(actor)
        if not request.get("idempotency_key"):
            raise ArgumentError("idempotency_key is required")
        project = await self._call(self.project_source, ("get_project_for_run", "get_project"), project_id)
        if project is None:
            raise NotFoundError(f"project not found: {project_id}")
        if not isinstance(project, dict) or project.get("status") != "ACTIVE":
            raise ValidationError("archived project cannot start a new run")
        input_version = await self._call(
            self.input_source or self.project_source,
            ("get_input_version", "get_project_input"),
            request["project_input_version_id"],
        )
        if not isinstance(input_version, dict) or input_version.get("project_id") != project_id:
            raise ValidationError("project input version does not belong to project")
        binding: object | None = None
        if request.get("repository_binding_id"):
            binding = await self._call(
                self.repository_source or self.project_source,
                ("get_binding", "get_repository_binding"),
                request["repository_binding_id"],
            )
            if not isinstance(binding, dict) or binding.get("project_id") != project_id or binding.get("status") != "READY":
                raise ValidationError("repository binding is not ready for this project")
        workflow = await self._call(
            self.workflow_source, ("get_version",), request["workflow_version_id"]
        )
        graph = await self._call(
            self.workflow_source, ("load_graph",), request["workflow_version_id"]
        )
        if not isinstance(workflow, dict) or workflow.get("status") != "PUBLISHED" or not isinstance(graph, dict):
            raise ValidationError("run requires an exact PUBLISHED workflow version")
        limits = deepcopy(request["limits"])
        try:
            amount = Decimal(str(limits["budget_amount"]))
        except (InvalidOperation, KeyError):
            raise ArgumentError("invalid budget_amount") from None
        if amount < 0 or limits.get("token_budget", 0) < 0 or limits.get("max_tasks", 0) <= 0:
            raise ArgumentError("run limits must be non-negative and max_tasks positive")
        references: dict[str, object] = {}
        resolved = await self._call(
            self.reference_source, ("resolve_snapshot", "resolve_workflow_references"), graph
        )
        if isinstance(resolved, dict):
            references = deepcopy(resolved)
        now = datetime.now(timezone.utc).isoformat()
        snapshot: RunSnapshot = {
            "project": deepcopy(project),
            "project_input": deepcopy(input_version),
            "repository_binding": deepcopy(binding) if isinstance(binding, dict) else None,
            "workflow_version": deepcopy(workflow),
            "workflow_graph": deepcopy(graph),
            "role_versions": list(references.get("role_versions", [])),
            "skill_versions": list(references.get("skill_versions", [])),
            "tool_versions": list(references.get("tool_versions", [])),
            "model_policies": list(references.get("model_policies", [])),
            "control_base_commit": str(references.get("control_base_commit", self.control_base_commit)),
            "limits": limits,
            "created_by": actor_id,
            "created_at": now,
        }
        snapshot_json = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
        snapshot_id = "snapshot:" + hashlib.sha256(snapshot_json.encode()).hexdigest()
        run_id = str(uuid.uuid4())
        run: RunView = {
            "id": run_id,
            "project_id": project_id,
            "workflow_version_id": request["workflow_version_id"],
            "snapshot_artifact_version_id": snapshot_id,
            "status": "CREATED",
            "limits": limits,
            "consumed": {"budget_amount": "0", "tokens": 0, "tasks": 0, "reworks": 0},
            "started_at": None,
            "completed_at": None,
            "failure_code": None,
            "version": 1,
        }
        request_hash = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        saved = await self.repository.create_run_with_snapshot(
            run, snapshot, idempotency_key=request["idempotency_key"], request_hash=request_hash
        )
        if self.scheduler is not None:
            await self._call(self.scheduler, ("start_run",), saved["id"])
        return saved

    async def get_run(self, actor: ActorContext, run_id: str) -> RunView:
        self._actor_id(actor)
        item = await self.repository.get_run(run_id)
        if item is None:
            raise NotFoundError(f"run not found: {run_id}")
        return item

    async def get_graph(self, actor: ActorContext, run_id: str) -> RunGraphView:
        await self.get_run(actor, run_id)
        graph = await self.repository.get_graph_projection(run_id)
        if graph is None:
            raise NotFoundError(f"run graph projection not found: {run_id}")
        return graph

    async def list_tasks(self, actor: ActorContext, run_id: str, query: TaskQuery) -> TaskPage:
        await self.get_run(actor, run_id)
        return await self.repository.list_tasks(run_id, query)

    async def stream_events(
        self, actor: ActorContext, run_id: str, last_event_id: str | None
    ) -> AsyncIterator[RunEventView]:
        await self.get_run(actor, run_id)
        for event in await self.repository.read_events_after(run_id, last_event_id, 200):
            yield event

    async def _command(
        self,
        run_id: str,
        request: RunCommand,
        *,
        allowed: set[str],
        target: str,
        scheduler_method: str,
        signal: str,
    ) -> CommandResult:
        key = request.get("idempotency_key", "")
        if not key:
            raise ArgumentError("idempotency_key is required")
        existing = await self.repository.get_command(run_id, key)
        if existing is not None:
            return existing
        run = await self.repository.get_run(run_id)
        if run is None:
            raise NotFoundError(f"run not found: {run_id}")
        if run["status"] not in allowed:
            raise VersionConflictError(f"run in {run['status']} cannot accept this command")
        result = await self.repository.apply_command(
            run_id, key, expected_version=request["expected_version"], target_status=target
        )
        control = {"command_id": result["command_id"], "signal": signal, "run_id": run_id, "new_epoch": result["run_version"]}
        await self.repository.append_control_signal(run_id, control)
        if self.control_signaler is not None:
            await self._call(self.control_signaler, ("signal_run_control", "publish_run_signal"), control)
        if self.scheduler is not None:
            if scheduler_method == "resume_run":
                await self._call(self.scheduler, (scheduler_method,), run_id, {"command_id": key, "reason": request.get("reason", "")})
            else:
                await self._call(self.scheduler, (scheduler_method,), run_id, key)
        return result

    async def pause(self, actor: ActorContext, run_id: str, request: RunCommand) -> CommandResult:
        self._actor_id(actor)
        return await self._command(run_id, request, allowed={"CREATED", "RUNNING", "WAITING_HUMAN"}, target="PAUSED", scheduler_method="pause_run", signal="PAUSE")

    async def resume(self, actor: ActorContext, run_id: str, request: ResumeRunRequest) -> CommandResult:
        self._actor_id(actor)
        return await self._command(run_id, request, allowed={"PAUSED", "WAITING_HUMAN"}, target="RUNNING", scheduler_method="resume_run", signal="RESUME")

    async def cancel(self, actor: ActorContext, run_id: str, request: RunCommand) -> CommandResult:
        self._actor_id(actor)
        return await self._command(run_id, request, allowed={"CREATED", "RUNNING", "PAUSED", "WAITING_HUMAN", "CANCELLING"}, target="CANCELLING", scheduler_method="cancel_run", signal="CANCEL")

    async def increase_budget(self, actor: ActorContext, run_id: str, request: IncreaseBudgetRequest) -> RunView:
        self._actor_id(actor)
        run = await self.get_run(actor, run_id)
        if run["version"] != request["expected_version"]:
            raise VersionConflictError("run budget version conflict")
        if run["limits"]["currency"] != request["currency"]:
            raise ArgumentError("budget currency cannot change")
        updated = deepcopy(run)
        updated["limits"]["budget_amount"] = str(
            Decimal(updated["limits"]["budget_amount"]) + Decimal(request["additional_amount"])
        )
        updated["limits"]["token_budget"] += request["additional_tokens"]
        return await self.repository.save_run(updated, run["version"])

    async def retry_task(self, actor: ActorContext, task_id: str, request: RetryTaskRequest) -> TaskView:
        self._actor_id(actor)
        task = await self.repository.get_task(task_id)
        if task is None:
            raise NotFoundError(f"task not found: {task_id}")
        if task["status"] not in {"FAILED", "LOST", "REJECTED", "BLOCKED"}:
            raise VersionConflictError("task is not retryable")
        attempt: AttemptView = {
            "id": str(uuid.uuid4()), "task_id": task_id, "attempt_no": 0,
            "status": "CREATED", "runner_id": None, "started_at": None,
            "completed_at": None, "error": None,
        }
        await self.repository.append_attempt(task_id, attempt)
        task = await self.repository.get_task(task_id)
        assert task is not None
        task["status"] = "READY"
        return await self.repository.save_task(task, request["expected_task_version"])
