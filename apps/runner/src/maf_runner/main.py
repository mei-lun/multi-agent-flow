"""Runner process entry point and startup self-check.

TASK-066 实现：节点启动时加载配置、执行环境自检、构建 ``NodeManifest`` 与
注册事件。本模块**不包含 Claim 循环**——仅完成启动到注册事件构造的流程。

启动顺序：

1. 加载 :class:`NodeSettings`（从 ``MAF_*`` 环境变量）。
2. 装配日志（``setup_logging``）。
3. 加载或创建持久 ``node_id``（``load_or_create_node_id``）。
4. 执行启动自检（:class:`StartupChecker`）：Docker、Git、工作目录、仓库绑定、
   安全基线。
5. 自检失败 → 打印汇总报告并以非零状态码退出，**不构造注册事件**。
6. 自检通过 → 构建 ``NodeManifest`` 与 ``NODE_REGISTERED`` 事件，输出 JSON。
7. 不 push 事件（push 属于 Claim 循环，不在本任务范围）。

设计决策：

- **禁止 Server HTTP**：仅通过 Git 协调分支通信，不调用中央服务器 HTTP API。
- **可测试性**：``run_startup`` 接受可注入的 ``settings``/``checker``/``probe``，
  测试无需依赖真实 Docker/Git 环境。
- **失败不申请任务**：自检失败时立即退出，不进入 Claim 循环。
- **node_id 稳定**：优先使用 ``NodeSettings.node_id``，未设置时从
  ``<workspace_root>/.maf/node-id`` 持久化文件读取或生成。
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from maf_runner.config import NodeSettings
from maf_runner.git_client import GitCoordinationClient
from maf_runner.logging import get_logger, setup_logging
from maf_runner.registry import (
    GitIdentityProvider,
    RunnerRegistry,
    load_or_create_node_id,
)
from maf_runner.security.startup_check import (
    StartupCheckResult,
    StartupChecker,
)


# --------------------------------------------------------------------------- #
# 启动结果
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class StartupOutcome:
    """启动流程结果。

    ``ok`` 为 ``True`` 时 ``event`` 携带注册事件，``manifest`` 携带节点清单；
    ``ok`` 为 ``False`` 时 ``check_result`` 携带失败详情，``event``/``manifest``
    为 ``None``。
    """

    ok: bool
    node_id: str = ""
    manifest: dict[str, Any] | None = None
    event: dict[str, Any] | None = None
    check_result: StartupCheckResult | None = None
    control_commit: str = ""

    def to_report(self) -> str:
        """返回人类可读的启动报告。"""
        lines: list[str] = []
        if self.ok:
            lines.append(f"startup OK: node_id={self.node_id}")
            if self.manifest:
                lines.append(
                    f"manifest: status={self.manifest.get('status')}, "
                    f"capabilities={self.manifest.get('capabilities')}"
                )
            if self.event:
                lines.append(
                    f"event: type={self.event.get('event_type')}, "
                    f"event_id={self.event.get('event_id')}"
                )
        else:
            lines.append(f"startup FAILED: node_id={self.node_id}")
            if self.check_result:
                lines.append(self.check_result.summary())
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 本地 Git 身份读取（默认实现）
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class LocalGitIdentityProvider:
    """从本地 Git 配置读取提交身份的默认实现。

    使用 ``git config user.name`` / ``git config user.email`` 读取；
    失败时返回空字典，由 :class:`RunnerRegistry` 回退到默认值。
    不抛异常。
    """

    git_binary: str = "git"
    workspace_root: Path | None = None

    def read_identity(self) -> dict[str, str]:
        import subprocess

        name = ""
        email = ""
        cwd = str(self.workspace_root) if self.workspace_root else None
        try:
            r_name = subprocess.run(
                [self.git_binary, "config", "user.name"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                cwd=cwd,
            )
            if r_name.returncode == 0:
                name = r_name.stdout.strip()
            r_email = subprocess.run(
                [self.git_binary, "config", "user.email"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                cwd=cwd,
            )
            if r_email.returncode == 0:
                email = r_email.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        return {"name": name, "email": email}


# --------------------------------------------------------------------------- #
# 启动流程
# --------------------------------------------------------------------------- #


def run_startup(
    settings: NodeSettings,
    *,
    checker: StartupChecker | None = None,
    git_identity_provider: GitIdentityProvider | None = None,
    control_commit: str = "0" * 40,
    output_json: bool = True,
) -> StartupOutcome:
    """执行启动自检并构建注册事件。

    参数：
        settings: 已加载的 :class:`NodeSettings`。
        checker: 可选的 :class:`StartupChecker`（测试注入）；默认构造新实例。
        git_identity_provider: 可选的 Git 身份读取器；默认使用
            :class:`LocalGitIdentityProvider`。
        control_commit: 当前 ``maf/control`` HEAD commit，用作 fencing 水位。
            默认 ``"0"*40``（占位，实际由调用方在 Claim 循环中替换为真实值）。
        output_json: 是否将注册事件 JSON 输出到 stdout。

    返回：
        :class:`StartupOutcome`。``ok=False`` 时不包含 manifest/event。
    """
    log = get_logger("maf_runner.main")

    # 1. node_id 已在 NodeSettings 中（由调用方通过 load_or_create_node_id
    #    预先填充）。此处仅记录。
    node_id = settings.node_id
    log.info("runner_startup_begin", node_id=node_id)

    # 2. 执行启动自检。
    if checker is None:
        checker = StartupChecker()
    check_result = checker.run(
        docker_binary=settings.docker_binary,
        git_binary=settings.git_binary,
        docker_socket=settings.docker_socket,
        workspace_root=settings.workspace_root,
        control_remote_url=settings.control_remote_url,
    )

    if not check_result.ok:
        report = check_result.summary()
        log.error("runner_startup_self_check_failed", node_id=node_id, report=report)
        if output_json:
            print(report, file=sys.stderr)
        return StartupOutcome(
            ok=False,
            node_id=node_id,
            check_result=check_result,
        )

    log.info("runner_startup_self_check_passed", node_id=node_id)

    # 3. 构建 Git 身份读取器（若未注入）。
    if git_identity_provider is None:
        git_identity_provider = LocalGitIdentityProvider(
            git_binary=settings.git_binary,
            workspace_root=settings.workspace_root,
        )

    # 4. 构建 NodeManifest 与注册事件。
    registry = RunnerRegistry(
        settings=settings,
        git_identity_provider=git_identity_provider,
    )
    manifest = registry.build_manifest()
    event = registry.build_registration_event(manifest, control_commit)

    log.info(
        "runner_registration_event_built",
        node_id=node_id,
        event_type=event["event_type"],
        event_id=event["event_id"],
    )

    if output_json:
        print(json.dumps(event, indent=2, ensure_ascii=False, sort_keys=True))

    return StartupOutcome(
        ok=True,
        node_id=node_id,
        manifest=dict(manifest),
        event=dict(event),
        check_result=check_result,
        control_commit=control_commit,
    )


# --------------------------------------------------------------------------- #
# TASK-067: Git 同步主循环
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SyncLoopResult:
    """同步主循环运行结果。

    统计字段供调用方与测试断言循环行为：迭代次数、fetch 失败次数、
    处理的分配数量、推送的事件数量、是否因 ``request_stop`` 退出。
    """

    iterations: int = 0
    last_commit: str = ""
    fetch_failures: int = 0
    assignments_processed: int = 0
    events_pushed: int = 0
    stopped: bool = False


class SyncLoop:
    """Git 同步主循环（TASK-067）。

    周期 fetch control → 比较 commit hash 去重 → 处理分配 → 推送节点事件。

    设计决策：

    - **去重**：双层去重——``control_commit`` 判断是否有变化（相同 commit 跳过），
      ``assignment_id`` 增量去重（新 commit 中已处理过的分配不重复处理）。
    - **错误重试**：fetch 失败时等待 ``poll_interval`` 后重试，不崩溃。
    - **优雅停止**：``request_stop()`` 设置标志，当前迭代完成后退出。
    - **离线分配禁止**：只从 control 读取 ``assignment``，不在本地决定任务分配。
      ``_extract_assignments`` 只返回 ``task.assignment.node_id == self._node_id``
      的任务——这些是中央调度器写入 control 的分配信息，不是节点本地决定。
    - **可测试性**：``sleeper``/``assignment_handler`` 可注入，测试不依赖真实
      ``asyncio.sleep`` 阻塞。
    """

    def __init__(
        self,
        *,
        registry: RunnerRegistry,
        git_client: GitCoordinationClient,
        node_id: str,
        poll_interval: float = 30.0,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        assignment_handler: (
            Callable[[dict[str, Any], dict[str, Any]], None] | None
        ) = None,
        logger: Any = None,
    ) -> None:
        self._registry: RunnerRegistry = registry
        self._git_client: GitCoordinationClient = git_client
        self._node_id: str = node_id
        self._poll_interval: float = poll_interval
        self._sleeper: Callable[[float], Awaitable[None]] = (
            sleeper or asyncio.sleep
        )
        self._assignment_handler: (
            Callable[[dict[str, Any], dict[str, Any]], None] | None
        ) = assignment_handler
        self._log: Any = logger or get_logger("maf_runner.sync_loop")
        self._stop_requested: bool = False
        self._last_commit: str = ""
        # 已处理的分配标识集合（assignment_id，缺省回退 task_id）。
        # 用于增量处理：即使 control commit 变化，已感知过的分配也不重复处理。
        self._processed_assignment_ids: set[str] = set()

    def request_stop(self) -> None:
        """请求优雅停止：当前迭代完成后退出，不中断正在执行的工作。"""
        self._stop_requested = True

    async def run_sync_loop(
        self,
        *,
        max_iterations: int | None = None,
    ) -> SyncLoopResult:
        """运行同步主循环。

        参数：
            max_iterations: 最大迭代次数；``None`` 表示无限循环直到
                ``request_stop`` 被调用。

        返回：
            :class:`SyncLoopResult`：包含迭代次数、最后 commit、失败次数等统计。

        循环流程（每次迭代）：

        1. 检查 ``_stop_requested`` → 若已请求停止则退出。
        2. 检查 ``max_iterations`` → 若已达上限则退出。
        3. ``fetch_control()`` → fetch 失败则记录并等待重试（不崩溃）。
        4. 比较 ``control_commit`` 与 ``_last_commit`` → 相同则跳过（去重）。
        5. 提取分配给本节点的任务（从 control 读取 ``assignment``），并按
           ``assignment_id`` 增量去重（已处理过的分配不重复处理）。
        6. 对每个新分配调用 ``assignment_handler``（若提供）。
        7. 若有新分配，构建并推送 ``NODE_UPDATED`` 事件。
        8. 更新 ``_last_commit``。
        9. 等待 ``poll_interval``（若未请求停止且未达上限）。
        """
        result = SyncLoopResult()
        while not self._stop_requested:
            if max_iterations is not None and result.iterations >= max_iterations:
                break
            result.iterations += 1
            await self._run_iteration(result)
            # 迭代完成后检查停止条件，避免在最后一次迭代后还 sleep。
            if self._stop_requested:
                break
            if max_iterations is not None and result.iterations >= max_iterations:
                break
            await self._sleeper(self._poll_interval)

        result.last_commit = self._last_commit
        result.stopped = self._stop_requested
        return result

    async def _run_iteration(self, result: SyncLoopResult) -> None:
        """执行单次迭代：fetch → 去重 → 处理分配 → 推送事件。"""
        try:
            snapshot = await self._git_client.fetch_control()
        except Exception as exc:  # noqa: BLE001 - fetch 异常不应崩溃主循环
            result.fetch_failures += 1
            self._log.warning(
                "sync_loop_fetch_exception",
                error=str(exc),
                iteration=result.iterations,
                node_id=self._node_id,
            )
            return

        if not snapshot.get("fetch_ok"):
            # fetch 失败：记录并等待重试，不生成离线分配。
            result.fetch_failures += 1
            self._log.warning(
                "sync_loop_fetch_failed",
                fetch_error=snapshot.get("fetch_error"),
                iteration=result.iterations,
                node_id=self._node_id,
            )
            return

        commit = snapshot.get("control_commit") or ""

        # 去重：相同 commit 不重复处理。
        if commit and commit == self._last_commit:
            return

        # 有变化：提取分配给本节点的任务（从 control 读取，非本地决定）。
        assignments = self._extract_assignments(snapshot)
        # 增量处理：只处理未感知过的分配（按 assignment_id 去重，缺省回退
        # task_id）。即使 control commit 变化，已处理过的分配也不重复处理，
        # 避免对同一分配重复推送事件。
        new_assignments: list[dict[str, Any]] = []
        for task in assignments:
            assignment = task.get("assignment") or {}
            assign_key = (
                assignment.get("assignment_id") or task.get("task_id") or ""
            )
            if assign_key and assign_key in self._processed_assignment_ids:
                continue
            new_assignments.append(task)
            if assign_key:
                self._processed_assignment_ids.add(assign_key)

        for task in new_assignments:
            if self._assignment_handler is not None:
                try:
                    self._assignment_handler(task, snapshot)
                except Exception as exc:  # noqa: BLE001 - handler 异常不阻塞循环
                    self._log.warning(
                        "sync_loop_assignment_handler_error",
                        error=str(exc),
                        task_id=task.get("task_id"),
                        node_id=self._node_id,
                    )
            result.assignments_processed += 1

        # 推送节点事件（NODE_UPDATED），通知中央节点已感知分配。
        if new_assignments:
            await self._push_sync_event(snapshot, result)

        self._last_commit = commit

    def _extract_assignments(
        self, snapshot: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """从快照中提取分配给本节点的任务。

        **离线分配禁止**：只返回 control 中由中央写入的、
        ``assignment.node_id`` 匹配本节点的任务。节点不在本地决定任务分配。
        """
        tasks = snapshot.get("tasks") or []
        assigned: list[dict[str, Any]] = []
        for task in tasks:
            assignment = task.get("assignment")
            if assignment is None:
                continue
            if assignment.get("node_id") == self._node_id:
                assigned.append(task)
        return assigned

    async def _push_sync_event(
        self, snapshot: dict[str, Any], result: SyncLoopResult
    ) -> None:
        """构建并推送 ``NODE_UPDATED`` 事件，通知中央节点已感知分配。"""
        try:
            manifest = self._registry.build_manifest()
            event = self._registry.build_registration_event(
                manifest, snapshot.get("control_commit") or "0" * 40
            )
            push_result = await self._git_client.append_event(event)
            if push_result.get("push_ok"):
                result.events_pushed += 1
            else:
                self._log.warning(
                    "sync_loop_event_push_failed",
                    push_error=push_result.get("push_error"),
                    node_id=self._node_id,
                )
        except Exception as exc:  # noqa: BLE001 - 事件推送失败不阻塞主循环
            self._log.warning(
                "sync_loop_event_push_exception",
                error=str(exc),
                node_id=self._node_id,
            )


def main() -> int:
    """CLI 入口：加载配置、自检、构建注册事件。

    返回进程退出码：``0`` 表示成功，非零表示自检失败。

    对应《GitHub 分布式协作协议》：节点通过 Git 协调分支通信，**不调用
    Server HTTP**。本函数不 push 事件（push 属于 Claim 循环）。
    """
    # 1. 加载配置（从 MAF_* 环境变量）。
    try:
        settings = NodeSettings()
    except Exception as exc:
        print(f"failed to load NodeSettings: {exc}", file=sys.stderr)
        return 2

    # 2. 装配日志。
    setup_logging(settings)

    # 3. 确保持久 node_id（若 NodeSettings.node_id 来自环境变量，已就绪；
    #    否则从 workspace_root/.maf/node-id 加载或生成）。
    if not settings.node_id:
        node_id = load_or_create_node_id(settings.workspace_root)
        # NodeSettings.node_id 是必填字段，此处不应到达；防御性处理。
        settings = settings.model_copy(update={"node_id": node_id})

    # 4. 执行启动流程。
    outcome = run_startup(settings, control_commit="0" * 40)

    if not outcome.ok:
        # 自检失败：打印报告，以非零状态码退出，不申请任务。
        print(outcome.to_report(), file=sys.stderr)
        return 1

    # 5. 成功：注册事件已输出到 stdout。
    print(outcome.to_report(), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
