"""启动、恢复和控制 LangGraph Run 的接口。"""

from typing import Any, Protocol
import asyncio
import inspect
from dataclasses import replace

from maf_server.scheduler.graph_builder import compile_workflow
from maf_server.scheduler.state import RunState


class SchedulerService(Protocol):
    async def start_run(self, run_id: str) -> None:
        """为 CREATED Run 建立首个 checkpoint 并推进到第一个持久等待点。

        读取不可变 Run Snapshot，编译对应 workflow hash 的图；使用 run_id 作为 thread key；
        若 checkpoint 已存在则按恢复处理，不能重复创建 Task；推进过程中所有外部执行只转成
        Git coordination task。失败要记录可恢复调度错误，不能删除 Run。
        """
        ...

    async def resume_run(self, run_id: str, command: dict[str, Any]) -> None:
        """从最新 checkpoint 处理一个幂等唤醒命令。

        command 必须包含唯一 event/command ID；先检查 wakeup 去重表，再加载 checkpoint 和
        最新领域状态；输入结果后推进到下一持久等待点。重复命令不重复产生 Task/Job。
        """
        ...

    async def pause_run(self, run_id: str, command_id: str) -> None:
        """停止向 control 发布新任务，在安全点把 Run 收敛为 PAUSED。已分配节点通过下一次 fetch 看到取消事件。"""
        ...

    async def cancel_run(self, run_id: str, command_id: str) -> None:
        """在 control 标记未开始任务取消、请求进行中任务停止，并最终收敛为 CANCELLED。"""
        ...

    async def recover_incomplete_runs(self) -> None:
        """服务启动后扫描非终态 Run，并根据 checkpoint/等待原因恢复。

        必须分批和加互斥，跳过正常等待 Git 任务提交/人工的 Run；只修复领域状态与 checkpoint
        不一致或有未消费 wakeup 的 Run。
        """
        ...

    async def handle_task_submission(self, task_id: str, submission_event_id: str) -> None:
        """确认提交事件已进入 control 且分支/epoch 有效后，幂等唤醒对应 Run。"""
        ...

    async def handle_human_decision(self, inbox_item_id: str) -> None:
        """读取不可变 Decision 并唤醒等待该 subject/version 的 Run。"""
        ...


class SchedulerServiceImpl:
    """Small orchestration service used by bootstrap and replay tests.

    Persistence and Git implementations are injected as duck-typed services;
    this keeps scheduler decisions independent from HTTP and database wiring.
    """

    def __init__(self, *, run_repository: object | None = None, workflow_repository: object | None = None,
                 checkpointer: object | None = None, graph_factory: object | None = None,
                 wakeup_service: object | None = None, projection_service: object | None = None,
                 coordination_service: object | None = None, alert_sink: object | None = None) -> None:
        self.run_repository = run_repository
        self.workflow_repository = workflow_repository
        self.checkpointer = checkpointer
        self.graph_factory = graph_factory
        self.wakeup_service = wakeup_service
        self.projection_service = projection_service
        self.coordination_service = coordination_service
        self.alert_sink = alert_sink
        self._commands: dict[str, str] = {}
        self._graphs: dict[str, Any] = {}
        self._recovery_lock = asyncio.Lock()

    async def _call(self, obj: object, name: str, *args: Any, **kwargs: Any) -> Any:
        result = getattr(obj, name)(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    async def _get_run(self, run_id: str) -> Any:
        if self.run_repository is None:
            return None
        getter = getattr(self.run_repository, "get_run", None)
        return await self._call(self.run_repository, "get_run", run_id) if getter else None

    async def _save_status(self, run: Any, status: str) -> Any:
        if isinstance(run, dict):
            updated = dict(run)
            updated["status"] = status
            updated["version"] = int(updated.get("version", 0)) + 1
        else:
            updated = replace(run, status=status)
        if self.run_repository is not None and hasattr(self.run_repository, "save_run"):
            return await self._call(self.run_repository, "save_run", updated, run.get("version") if isinstance(run, dict) else None)
        return updated

    async def start_run(self, run_id: str) -> None:
        run = await self._get_run(run_id)
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        status = run.get("status") if isinstance(run, dict) else getattr(run, "status", None)
        if status in {"COMPLETED", "FAILED", "CANCELLED", "PAUSED", "CANCELLING"}:
            return
        graph = self._graphs.get(run_id)
        if graph is None:
            workflow = None
            if self.workflow_repository is not None and hasattr(self.workflow_repository, "load_graph"):
                version_id = run.get("workflow_version_id") if isinstance(run, dict) else getattr(run, "workflow_version_id", "")
                workflow = await self._call(self.workflow_repository, "load_graph", version_id)
                workflow = {"graph": workflow} if workflow is not None else None
            if workflow is None:
                workflow = run.get("workflow", {}) if isinstance(run, dict) else getattr(run, "workflow", {})
            factory = self.graph_factory or compile_workflow
            graph = factory(workflow, self.checkpointer)
            if inspect.isawaitable(graph):
                graph = await graph
            self._graphs[run_id] = graph
        initial = RunState(run_id=run_id, workflow_version_id=str(run.get("workflow_version_id", "") if isinstance(run, dict) else getattr(run, "workflow_version_id", "")))
        config = {"configurable": {"thread_id": run_id}}
        if hasattr(graph, "ainvoke"):
            result = graph.ainvoke(initial.to_dict(), config=config)
            if inspect.isawaitable(result):
                await result
        elif hasattr(graph, "invoke"):
            graph.invoke(initial.to_dict(), config=config)
        if status == "CREATED":
            await self._save_status(run, "RUNNING")

    async def resume_run(self, run_id: str, command: dict[str, Any]) -> None:
        event_id = command.get("event_id") or command.get("command_id")
        if not event_id:
            raise ValueError("resume command requires event_id/command_id")
        if event_id in self._commands:
            return
        run = await self._get_run(run_id)
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        status = run.get("status") if isinstance(run, dict) else getattr(run, "status", None)
        if status in {"COMPLETED", "FAILED", "CANCELLED"}:
            self._commands[str(event_id)] = status
            return
        self._commands[str(event_id)] = "accepted"
        graph = self._graphs.get(run_id)
        if graph is not None and hasattr(graph, "ainvoke"):
            result = graph.ainvoke(command, config={"configurable": {"thread_id": run_id}})
            if inspect.isawaitable(result):
                await result

    async def pause_run(self, run_id: str, command_id: str) -> None:
        if command_id in self._commands:
            return
        run = await self._get_run(run_id)
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        status = run.get("status") if isinstance(run, dict) else getattr(run, "status", None)
        if status not in {"CREATED", "RUNNING", "WAITING_HUMAN", "WAITING_GIT_TASK"}:
            self._commands[command_id] = str(status)
            return
        self._commands[command_id] = "PAUSED"
        await self._save_status(run, "PAUSED")

    async def cancel_run(self, run_id: str, command_id: str) -> None:
        if command_id in self._commands:
            return
        run = await self._get_run(run_id)
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        status = run.get("status") if isinstance(run, dict) else getattr(run, "status", None)
        self._commands[command_id] = "CANCELLED"
        if status not in {"COMPLETED", "FAILED", "CANCELLED"}:
            await self._save_status(run, "CANCELLED")

    async def recover_incomplete_runs(self) -> None:
        if self._recovery_lock.locked():
            return
        async with self._recovery_lock:
            await self._recover_incomplete_runs_locked()

    async def _recover_incomplete_runs_locked(self) -> None:
        if self.run_repository is None:
            return
        if self.coordination_service is not None:
            sync = getattr(self.coordination_service, "sync_all", None) or getattr(self.coordination_service, "fetch_all", None)
            if sync is not None:
                value = sync()
                if inspect.isawaitable(value):
                    await value
        if self.projection_service is not None:
            rebuild = getattr(self.projection_service, "rebuild_projection", None)
            if rebuild is not None:
                value = rebuild()
                if inspect.isawaitable(value):
                    await value
        method = getattr(self.run_repository, "list_incomplete_runs", None) or getattr(self.run_repository, "list_nonterminal", None)
        if method is None:
            return
        runs = await self._call(self.run_repository, method.__name__)
        for run in runs or []:
            run_id = run.get("id") if isinstance(run, dict) else getattr(run, "id")
            status = run.get("status") if isinstance(run, dict) else getattr(run, "status", None)
            waiting_for = run.get("waiting_for") if isinstance(run, dict) else getattr(run, "waiting_for", None)
            if not run_id:
                continue
            pending = False
            pending_method = getattr(self.run_repository, "has_pending_wakeup", None)
            if pending_method is not None:
                pending = bool(await self._call(self.run_repository, "has_pending_wakeup", str(run_id)))
            checkpoint = None
            if self.checkpointer is not None:
                getter = getattr(self.checkpointer, "get", None) or getattr(self.checkpointer, "load", None)
                if getter is not None:
                    checkpoint = getter(str(run_id))
                    if inspect.isawaitable(checkpoint):
                        checkpoint = await checkpoint
            normal_wait = status in {"WAITING_GIT_TASK", "WAITING_HUMAN"} or waiting_for in {"GIT_TASK", "HUMAN_GATE"}
            if normal_wait and not pending:
                # Waiting is a durable successful state. Missing checkpoint is
                # inconsistent and must be reported, never rewritten to FAILED.
                if self.checkpointer is not None and checkpoint is None:
                    await self._alert("CHECKPOINT_MISSING_FOR_WAITING_RUN", str(run_id))
                continue
            if status == "PAUSED" and not pending:
                continue
            await self.start_run(str(run_id))

    async def _alert(self, code: str, run_id: str) -> None:
        if self.alert_sink is None:
            return
        method = getattr(self.alert_sink, "emit", self.alert_sink)
        value = method({"code": code, "run_id": run_id})
        if inspect.isawaitable(value):
            await value

    async def rebuild_projection(self) -> object | None:
        """Fetch the Git fact source and deterministically rebuild its SQLite projection."""
        if self.coordination_service is not None:
            fetch = getattr(self.coordination_service, "fetch_control", None)
            snapshot = fetch() if fetch is not None else None
            if inspect.isawaitable(snapshot):
                snapshot = await snapshot
        else:
            snapshot = None
        if self.projection_service is None:
            return snapshot
        rebuild = getattr(self.projection_service, "rebuild_projection")
        value = rebuild(snapshot) if snapshot is not None else rebuild()
        return await value if inspect.isawaitable(value) else value

    async def handle_task_submission(self, task_id: str, submission_event_id: str) -> None:
        if self.wakeup_service is not None:
            run_id = task_id
            task = None
            if self.run_repository is not None and hasattr(self.run_repository, "get_task"):
                task = await self._call(self.run_repository, "get_task", task_id)
                if isinstance(task, dict):
                    run_id = str(task.get("run_id", run_id))
                elif task is not None:
                    run_id = str(getattr(task, "run_id", run_id))
            task_epoch = None
            if isinstance(task, dict):
                task_epoch = task.get("assignment_epoch")
                if task_epoch is None and isinstance(task.get("assignment"), dict):
                    task_epoch = task["assignment"].get("assignment_epoch")
            wake = getattr(self.wakeup_service, "wake")
            value = wake(run_id, submission_event_id, task_id=task_id, assignment_epoch=task_epoch)
            if inspect.isawaitable(value):
                await value

    async def handle_human_decision(self, inbox_item_id: str) -> None:
        if self.wakeup_service is not None:
            resolver = getattr(self.wakeup_service, "run_for_inbox", None)
            run_id: object = inbox_item_id
            if resolver is not None:
                run_id = resolver(inbox_item_id)
                if inspect.isawaitable(run_id):
                    run_id = await run_id
            await self._call(self.wakeup_service, "wake", str(run_id), inbox_item_id)
