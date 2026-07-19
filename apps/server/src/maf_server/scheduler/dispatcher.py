"""把 Workflow 节点转换为 Git 协调任务的接口。"""

from dataclasses import dataclass
from dataclasses import field
import hashlib
import inspect
from typing import Any


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    run_id: str
    node_run_id: str
    role_snapshot_id: str
    repository_binding_id: str
    required_capabilities: tuple[str, ...]
    base_commit: str
    dependencies: tuple[str, ...] = ()
    input_artifact_version_ids: tuple[str, ...] = ()
    output_contracts: tuple[dict[str, Any], ...] = ()
    assignment_epoch: int = 1


class TaskDispatcher:
    """Idempotently publish functional tasks to the Git control branch.

    The dispatcher deliberately accepts a small duck-typed coordination
    service so it remains usable with the production Git service and focused
    fakes.  Task IDs are derived from the immutable run/node-run identity.
    """

    def __init__(
        self,
        coordination_service: object | None = None,
        *,
        project_id: str = "",
        expected_control_commit: str = "",
        task_store: object | None = None,
        checkpoint_store: object | None = None,
    ) -> None:
        self._coordination_service = coordination_service
        self._project_id = project_id
        self._expected_control_commit = expected_control_commit
        self._task_store = task_store
        self._checkpoint_store = checkpoint_store
        self._published: dict[str, str] = {}

    async def dispatch(self, request: DispatchRequest) -> str:
        """创建或返回一个 Git coordination task_id。

        从 Run Snapshot 构造功能级任务、输入引用、输出契约、能力和依赖；以
        run_id+node_run_id 为幂等键；通过 GitCoordinationService 写 `maf/control`。该方法不
        选择节点、不调用节点，也不创建 HTTP Job。
        """
        if not request.run_id or not request.node_run_id:
            raise ValueError("run_id and node_run_id are required")
        idempotency_key = f"{request.run_id}:{request.node_run_id}"
        if idempotency_key in self._published:
            return self._published[idempotency_key]
        lookup = getattr(self._task_store, "get_by_idempotency_key", None)
        if lookup is not None:
            existing = lookup(idempotency_key)
            if inspect.isawaitable(existing):
                existing = await existing
            if existing:
                task_id = existing if isinstance(existing, str) else existing.get("task_id", existing.get("id"))
                if task_id:
                    self._published[idempotency_key] = str(task_id)
                    return str(task_id)

        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
        # task-v1 uses an uppercase prefix and numeric-looking suffixes; keep
        # the deterministic digest while producing a protocol-safe identifier.
        task_id = f"TASK-{digest.upper()}-1"
        task: dict[str, Any] = {
            "schema_version": 1,
            "task_id": task_id,
            "parent_task_id": None,
            "title": f"Workflow node {request.node_run_id}",
            "description": f"Run {request.run_id} node execution",
            "status": "READY",
            "priority": 100,
            "requirements": {
                "role_snapshot_id": request.role_snapshot_id,
                "repository_binding_id": request.repository_binding_id,
                "capabilities": list(request.required_capabilities),
                "base_commit": request.base_commit,
                "run_id": request.run_id,
                "node_run_id": request.node_run_id,
                "idempotency_key": idempotency_key,
                "input_artifact_version_ids": list(request.input_artifact_version_ids),
                "output_contracts": list(request.output_contracts),
                "assignment_epoch": request.assignment_epoch,
            },
            "dependencies": list(request.dependencies),
            "assignment": None,
            "progress": {
                "percent": 0,
                "completed_items": [],
                "remaining_items": [],
                "problems": [],
                "current_head_commit": None,
                "test_summary": None,
                "last_reported_at": None,
            },
            "delivery": {
                "branch": None,
                "base_commit": request.base_commit or None,
                "head_commit": None,
                "pull_request_url": None,
                "changed_paths": [],
                "test_report_path": None,
                "known_issues": [],
            },
            "version": 1,
        }
        publish = getattr(self._coordination_service, "publish_tasks", None)
        if publish is not None:
            result = publish(self._project_id, [task], self._expected_control_commit)
            if inspect.isawaitable(result):
                await result
        elif self._coordination_service is not None:
            create = getattr(self._coordination_service, "create_task", None)
            if create is None:
                raise TypeError("coordination service must expose publish_tasks or create_task")
            result = create(task)
            if inspect.isawaitable(result):
                await result
        save = getattr(self._task_store, "save", None)
        if save is not None:
            result = save(idempotency_key, task)
            if inspect.isawaitable(result):
                await result
        self._published[idempotency_key] = task_id
        checkpoint = getattr(self._checkpoint_store, "mark_waiting_git_task", None)
        if checkpoint is not None:
            result = checkpoint(request.run_id, request.node_run_id, task_id)
            if inspect.isawaitable(result):
                await result
        return task_id
