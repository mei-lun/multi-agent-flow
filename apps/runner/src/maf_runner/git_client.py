"""节点通过 Git pull/push 参与协调的接口。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final, Protocol

import structlog
import yaml

from maf_contracts.coordination import (
    CoordinationEvent,
    CoordinationEventModel,
    CoordinationTask,
    build_event_file_path,
    build_node_branch_name,
)
from maf_domain.errors import ArgumentError

from maf_runner.workspace.git import RunnerGitCli


class GitCoordinationClient(Protocol):
    async def fetch_control(self) -> dict[str, Any]:
        """fetch 远端 `maf/control`，验证 fast-forward、协议版本和 Schema 后返回快照。

        不使用工作区未提交内容覆盖 control；签名/Schema/仓库身份异常时停止认领新任务。

        返回字典包含 fetch 状态字段（``fetch_ok``、``control_commit``、``control_branch``、
        ``remote``、``fetch_error``）以及快照字段（``project_id``、``commit_timestamp``、
        ``project_yaml``、``status_md``、``tasks_paths``、``nodes_paths``、``events_paths``、
        ``tasks``、``nodes``、``generated_at``）。fetch 失败或快照读取失败时仅返回
        fetch 状态字段，调用方可据此判断是否继续认领新任务。

        TASK-067 增强：``tasks`` 与 ``nodes`` 列表已从 ``.maf/tasks/*.yaml`` 和
        ``.maf/nodes/*.yaml`` 解析填充，供同步主循环检测中央写入的分配信息
        （assignment）。节点**不在本地决定任务分配**，只从 control 读取。
        """
        ...

    async def append_event(self, event: CoordinationEvent) -> dict:
        """向本节点 `maf/node/<node-id>` 分支追加一个事件文件并 push，返回事件 commit。

        只能 fast-forward 自己的节点分支；push 冲突时 fetch/rebase 并用同一 event_id 重试；
        不能直接修改 `maf/control`。
        """
        ...

    async def verify_binding(self, *, timeout_seconds: int = 60) -> dict[str, Any]:
        """验证 Git 远端绑定（remote URL 可达且 control 分支可 fetch）。

        返回脱敏验证结果字典，含 ``ok``、``remote``、``control_branch``、``error``
        字段；凭据不进入返回值或日志。用于启动自检与同步主循环前的健康检查。
        """
        ...

    async def wait_for_assignment(self, task_id: str, event_id: str, timeout_seconds: int) -> CoordinationTask | None:
        """周期性 fetch control，直到 claim 被接受/拒绝或超时；仅 owner/epoch 匹配时返回任务。"""
        ...

    async def push_task_branch(self, task: CoordinationTask, workspace_path: str) -> str:
        """把本地提交 fast-forward push 到任务规定分支并返回远端 head。

        branch 名、base、node_id 和 epoch 必须与 control assignment 一致；禁止 push main/control。
        """
        ...

    async def current_task(self, task_id: str) -> CoordinationTask:
        """重新 fetch 后返回权威任务，用于检查取消、超时、返工和 epoch 变化。"""
        ...


# --------------------------------------------------------------------------- #
# TASK-014: Git 凭据与远端验证
# --------------------------------------------------------------------------- #


#: 节点禁止 push 的分支（``refs/heads/`` 前缀剥离后比较）。
#: 对应《GitHub 分布式协作协议》§2：``main`` 由最终评审流程写入，
#: ``maf/control`` 由中央调度器单写；``master`` 为兼容旧仓库命名。
FORBIDDEN_PUSH_BRANCHES: Final[frozenset[str]] = frozenset(
    {"main", "master", "maf/control"}
)

#: ``refs/heads/`` 前缀，用于归一化分支名比较。
_REFS_HEADS_PREFIX: Final[str] = "refs/heads/"

#: 事件 worktree 子目录名（位于 repository_path 下，保持在 allowed_roots 内）。
_EVENT_WORKTREE_DIR: Final[str] = ".maf-wt-events"


class RunnerGitClient:
    """Runner 端 Git 客户端，封装 :class:`RunnerGitCli`。

    提供 ``fetch_control`` 与 ``push_task_branch`` 等操作，附加：

    - **凭据注入**：经 :class:`RunnerGitCli.extra_env` 注入子进程（HTTPS token 经
      ``MAF_GIT_CREDENTIAL_TOKEN``），凭据绝不进入命令行参数或日志（由
      :class:`SubprocessGitCli` 保证）。
    - **分支保护**：``push_task_branch`` 和 ``append_event`` 在调用 ``git push`` 前
      校验目标分支，拒绝 ``main``、``master``、``maf/control``（协议 §2 节点写权限
      边界）。这是节点侧的硬性约束，与远端分支保护互为补充。
    - **脱敏健康报告**：返回的字典只含 ``fetch_ok``、``control_commit``、
      ``push_ok``、``branch``、``remote``、错误信息（已由
      :class:`SubprocessGitCli._redact_text` 脱敏）；明文凭据不出现在返回值中。

    设计决策：

    - 不使用 ``git remote`` 子命令（不在白名单）；remote 名/URL 由
      :class:`NodeSettings.control_remote_url` 配置，直接传给 ``fetch``/``push``。
    - ``fetch_control`` 只 fetch ``maf/control`` 分支，不切换工作区，不修改
      本地分支；调用方负责后续 ``rev-parse`` / Schema 校验。
    - ``append_event`` 使用临时 ``git worktree`` 写入 ``maf/node/<node-id>``
      分支，避免干扰主工作区；每个事件一个独立 JSON 文件（append-only）。
    """

    def __init__(
        self,
        *,
        git_cli: RunnerGitCli,
        repository_path: str,
        control_remote: str = "origin",
        control_branch: str = "maf/control",
        node_id: str = "",
        logger: Any = None,
    ) -> None:
        if not repository_path:
            raise ArgumentError("repository_path must not be empty")
        if not control_remote:
            raise ArgumentError("control_remote must not be empty")
        if not control_branch:
            raise ArgumentError("control_branch must not be empty")
        self._cli: RunnerGitCli = git_cli
        self._repository_path: str = repository_path
        self._control_remote: str = control_remote
        self._control_branch: str = control_branch
        self._node_id: str = node_id
        self._log: Any = logger or structlog.get_logger("maf.runner.git_client")

    # ------------------------------------------------------------------ #
    # 分支保护
    # ------------------------------------------------------------------ #

    def is_forbidden_push_target(self, branch: str) -> bool:
        """判断分支是否为禁止 push 的受保护分支。

        比较 ``refs/heads/`` 前缀剥离后的分支名与
        :data:`FORBIDDEN_PUSH_BRANCHES`。
        """
        clean = self._normalize_branch(branch)
        return clean in FORBIDDEN_PUSH_BRANCHES

    @staticmethod
    def _normalize_branch(branch: str) -> str:
        """剥离 ``refs/heads/`` 前缀与首尾空白。"""
        clean = branch.strip()
        if clean.startswith(_REFS_HEADS_PREFIX):
            clean = clean[len(_REFS_HEADS_PREFIX):]
        return clean

    # ------------------------------------------------------------------ #
    # Git 操作
    # ------------------------------------------------------------------ #

    async def fetch_control(self, *, timeout_seconds: int = 60) -> dict[str, Any]:
        """fetch ``maf/control`` 分支并读取快照，返回脱敏 fetch 结果与快照字段。

        TASK-016 扩展：fetch 成功后从远端跟踪 ref（``refs/remotes/<remote>/<branch>``）
        只读读取 ``.maf/`` 协议目录内容，构造与 :class:`LocalGitCoordinationService.fetch_control`
        一致语义的快照字段。读取使用 ``git show`` / ``git ls-tree`` / ``git log``，
        不修改工作区、不切换分支、不 push。

        返回字典字段：

        - **fetch 状态**（始终存在）：
            - ``fetch_ok``：fetch 是否成功。
            - ``control_commit``：fetch 成功后解析的远端 control 分支 head commit；
              失败时为 ``None``。调用方据此判断"无变化"避免重复处理。
            - ``control_branch``、``remote``：调用参数（不含明文凭据）。
            - ``fetch_error``：失败时的 stderr（已由 SubprocessGitCli 脱敏）。
        - **快照字段**（fetch 成功且快照读取成功时存在）：
            - ``project_id``、``commit_timestamp``、``project_yaml``、``status_md``、
              ``tasks_paths``、``nodes_paths``、``events_paths``、``tasks``、``nodes``、
              ``generated_at``：与 :class:`CoordinationSnapshot` 字段一致。
        - ``snapshot_error``：fetch 成功但快照读取失败时的错误信息（已脱敏）。
        """
        rc, _out, err = await self._cli.run(
            self._repository_path,
            ["fetch", "--", self._control_remote, self._control_branch],
            timeout_seconds,
        )
        control_commit: str | None = None
        if rc == 0:
            rc_rev, sha, _err_rev = await self._cli.run(
                self._repository_path,
                [
                    "rev-parse",
                    f"refs/remotes/{self._control_remote}/{self._control_branch}",
                ],
                timeout_seconds,
            )
            if rc_rev == 0:
                control_commit = sha.strip() or None
        result: dict[str, Any] = {
            "fetch_ok": rc == 0,
            "control_commit": control_commit,
            "control_branch": self._control_branch,
            "remote": self._control_remote,
            "fetch_error": err if rc != 0 else None,
        }

        # TASK-016: fetch 成功后读取 control 快照（只读）。
        # 远端跟踪 ref 格式：refs/remotes/<remote>/<control_branch>
        snapshot_error: str | None = None
        if rc == 0 and control_commit:
            try:
                snapshot = await self._read_control_snapshot(
                    control_commit=control_commit,
                    timeout_seconds=timeout_seconds,
                )
                result.update(snapshot)
            except Exception as exc:
                # 快照读取失败不阻塞 fetch 状态返回；调用方据 fetch_ok 与
                # snapshot_error 决定是否继续认领新任务。
                snapshot_error = str(exc)
                result["snapshot_error"] = snapshot_error
                self._log.warning(
                    "runner_fetch_control_snapshot_failed",
                    control_commit=control_commit,
                    error=snapshot_error,
                    node_id=self._node_id,
                )

        self._log.info(
            "runner_fetch_control",
            fetch_ok=result["fetch_ok"],
            has_control_commit=control_commit is not None,
            has_snapshot=snapshot_error is None and rc == 0 and control_commit is not None,
            node_id=self._node_id,
        )
        return result

    async def _read_control_snapshot(
        self,
        *,
        control_commit: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """从远端跟踪 ref 只读读取 control 快照（TASK-016）。

        使用 ``git show`` / ``git ls-tree`` / ``git log`` 读取
        ``refs/remotes/<remote>/<control_branch>`` 上的 ``.maf/`` 协议目录内容，
        构造与 :class:`LocalGitCoordinationService.fetch_control` 一致语义的快照字段。

        所有 git 命令均为只读，不修改工作区、不切换分支、不 push。
        读取失败时抛异常，由 :meth:`fetch_control` 捕获并记录为 ``snapshot_error``。
        """
        remote_ref = f"refs/remotes/{self._control_remote}/{self._control_branch}"
        maf_dir = ".maf"

        # 1. 读取 commit 时间戳（ISO 8601）。
        rc, out, err = await self._cli.run(
            self._repository_path,
            ["log", "-1", "--format=%cI", remote_ref],
            timeout_seconds,
        )
        if rc != 0:
            raise RuntimeError(
                f"git log -1 --format=%cI failed: {err.strip()}"
            )
        commit_timestamp = out.strip()

        # 2. 读取 .maf/project.yaml。
        rc, out, err = await self._cli.run(
            self._repository_path,
            ["show", f"{remote_ref}:{maf_dir}/project.yaml"],
            timeout_seconds,
        )
        if rc != 0:
            raise RuntimeError(
                f"git show {remote_ref}:{maf_dir}/project.yaml failed: {err.strip()}"
            )
        try:
            project_yaml = yaml.safe_load(out) or {}
        except yaml.YAMLError as exc:
            raise RuntimeError(f".maf/project.yaml is not valid YAML: {exc}") from exc
        if not isinstance(project_yaml, dict):
            raise RuntimeError(".maf/project.yaml is not an object")

        project_id = project_yaml.get("project_id", "")

        # 3. 读取 .maf/status.md。
        rc, out, err = await self._cli.run(
            self._repository_path,
            ["show", f"{remote_ref}:{maf_dir}/status.md"],
            timeout_seconds,
        )
        if rc != 0:
            raise RuntimeError(
                f"git show {remote_ref}:{maf_dir}/status.md failed: {err.strip()}"
            )
        status_md = out

        # 4. 列出 tasks/nodes/events 目录。
        tasks_paths = await self._list_remote_dir(
            remote_ref, f"{maf_dir}/tasks/", timeout_seconds
        )
        nodes_paths = await self._list_remote_dir(
            remote_ref, f"{maf_dir}/nodes/", timeout_seconds
        )
        events_paths = await self._list_remote_dir(
            remote_ref, f"{maf_dir}/events/", timeout_seconds
        )

        # 5. TASK-067: 解析 tasks/nodes YAML 文件，供同步主循环检测分配信息。
        #    只读读取，不修改工作区；解析失败的单个文件跳过（不阻塞整体快照）。
        tasks = await self._read_yaml_files(
            remote_ref, tasks_paths, timeout_seconds
        )
        nodes = await self._read_yaml_files(
            remote_ref, nodes_paths, timeout_seconds
        )

        from datetime import datetime, timezone

        return {
            "project_id": project_id,
            "control_commit": control_commit,
            "commit_timestamp": commit_timestamp,
            "project_yaml": project_yaml,
            "status_md": status_md,
            "tasks_paths": tasks_paths,
            "nodes_paths": nodes_paths,
            "events_paths": events_paths,
            "tasks": tasks,
            "nodes": nodes,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def _read_yaml_files(
        self,
        remote_ref: str,
        paths: list[str],
        timeout_seconds: int,
    ) -> list[dict[str, Any]]:
        """从远端 ref 读取并解析多个 YAML 文件（TASK-067）。

        跳过 ``.gitkeep`` 占位文件与解析失败的单个文件；返回成功解析的字典列表。
        用于读取 ``.maf/tasks/*.yaml`` 和 ``.maf/nodes/*.yaml``，供同步主循环
        检测中央写入的任务分配信息。
        """
        results: list[dict[str, Any]] = []
        for path in paths:
            if path.endswith(".gitkeep"):
                continue
            rc, out, _err = await self._cli.run(
                self._repository_path,
                ["show", f"{remote_ref}:{path}"],
                timeout_seconds,
            )
            if rc != 0:
                continue
            try:
                data = yaml.safe_load(out)
            except yaml.YAMLError:
                continue
            if isinstance(data, dict):
                results.append(data)
        return results

    async def _list_remote_dir(
        self,
        remote_ref: str,
        prefix: str,
        timeout_seconds: int,
    ) -> list[str]:
        """``git ls-tree -r --name-only <remote_ref> -- <prefix>``，列出远端 ref 上某目录所有文件。"""
        rc, out, _err = await self._cli.run(
            self._repository_path,
            ["ls-tree", "-r", "--name-only", remote_ref, "--", prefix],
            timeout_seconds,
        )
        if rc != 0:
            return []
        paths = [line.strip() for line in out.splitlines() if line.strip()]
        return sorted(paths)

    async def push_task_branch(
        self,
        *,
        branch: str,
        timeout_seconds: int = 60,
    ) -> dict:
        """push 任务分支到远端，返回脱敏 push 结果。

        对应协议 §2：节点只写自己的事件分支（``maf/node/<node-id>``）和被分配
        任务的分支（``maf/task/<task-id>/...``）。push 到 ``main``、``master``
        或 ``maf/control`` 一律拒绝，抛 :class:`ArgumentError`。

        返回字典字段：

        - ``push_ok``：push 是否成功。
        - ``branch``：归一化后的目标分支名（``refs/heads/`` 前缀已剥离）。
        - ``remote``、``push_error``：调用参数与错误信息（已脱敏）。
        """
        if self.is_forbidden_push_target(branch):
            raise ArgumentError(
                f"nodes must not push to protected branch {branch!r}",
                context={
                    "branch": branch,
                    "node_id": self._node_id,
                    "forbidden": sorted(FORBIDDEN_PUSH_BRANCHES),
                },
            )
        normalized = self._normalize_branch(branch)
        rc, _out, err = await self._cli.run(
            self._repository_path,
            [
                "push",
                "--",
                self._control_remote,
                f"HEAD:refs/heads/{normalized}",
            ],
            timeout_seconds,
        )
        result: dict[str, Any] = {
            "push_ok": rc == 0,
            "branch": normalized,
            "remote": self._control_remote,
            "push_error": err if rc != 0 else None,
        }
        self._log.info(
            "runner_push_task_branch",
            push_ok=result["push_ok"],
            branch=normalized,
            node_id=self._node_id,
        )
        return result

    # ------------------------------------------------------------------ #
    # TASK-018: 节点事件分支写入
    # ------------------------------------------------------------------ #

    async def append_event(
        self,
        event: CoordinationEvent,
        *,
        timeout_seconds: int = 60,
        max_retries: int = 2,
    ) -> dict:
        """向 ``maf/node/<node-id>`` 分支追加一个事件文件并 fast-forward push。

        对应协议 §2/§6/§11：节点只写自己的事件分支，不能写 ``maf/control``。
        每个事件以独立 JSON 文件（``.maf/events/<event-id>.json``）追加，
        保证 append-only：新事件不覆盖已有事件文件。

        参数：
            event: ``CoordinationEvent`` dict（由 ``RunnerRegistry`` 等构造）。
            timeout_seconds: 每次 git 子命令的超时。
            max_retries: push 冲突时的最大重试次数（fetch + rebase 后用同
                event_id 重试，对应协议 §11）。

        返回字典字段：

        - ``push_ok``：push 是否成功。
        - ``commit``：push 成功后的 HEAD commit SHA；失败时为 ``None``。
        - ``branch``：归一化后的节点分支名。
        - ``event_id``、``event_path``：事件 ID 和事件文件相对路径。
        - ``remote``、``push_error``：调用参数与错误信息（已脱敏）。
        - ``attempts``：实际尝试次数（含首次）。

        异常：
            ArgumentError: 事件格式非法、node_id 不匹配客户端配置、或目标
            分支受保护（``maf/control``/``main``/``master``）。
        """
        # 1. 校验事件格式（与 event-v1.schema.json 对齐）。
        model = CoordinationEventModel.model_validate(dict(event))

        # 2. 节点只能写自己的事件分支（协议 §2/§4）。
        if not self._node_id:
            raise ArgumentError(
                "append_event requires node_id to be configured on RunnerGitClient",
                context={"event_node_id": model.node_id},
            )
        if model.node_id != self._node_id:
            raise ArgumentError(
                "node can only append events to its own branch",
                context={
                    "client_node_id": self._node_id,
                    "event_node_id": model.node_id,
                },
            )

        # 3. 确定目标分支并执行分支保护检查。
        branch = build_node_branch_name(model.node_id)
        if self.is_forbidden_push_target(branch):
            raise ArgumentError(
                f"append_event target branch {branch!r} is protected",
                context={
                    "branch": branch,
                    "node_id": self._node_id,
                    "forbidden": sorted(FORBIDDEN_PUSH_BRANCHES),
                },
            )

        event_path = build_event_file_path(model.event_id)
        event_json = json.dumps(
            model.model_dump(mode="json"), indent=2, ensure_ascii=False, sort_keys=True
        )
        worktree_path = str(Path(self._repository_path) / _EVENT_WORKTREE_DIR)

        last_error: str | None = None
        for attempt in range(1, max_retries + 2):
            result = await self._append_event_attempt(
                branch=branch,
                event_path=event_path,
                event_json=event_json,
                event_id=model.event_id,
                worktree_path=worktree_path,
                timeout_seconds=timeout_seconds,
            )
            if result["push_ok"]:
                result["attempts"] = attempt
                self._log.info(
                    "runner_append_event_ok",
                    event_id=model.event_id,
                    event_type=model.event_type,
                    branch=branch,
                    commit=result.get("commit"),
                    attempts=attempt,
                    node_id=self._node_id,
                )
                return result
            last_error = result.get("push_error")
            # push 冲突：worktree 已清理，下一轮重新 fetch + 创建 worktree。
            self._log.warning(
                "runner_append_event_retry",
                event_id=model.event_id,
                attempt=attempt,
                push_error=last_error,
                node_id=self._node_id,
            )

        return {
            "push_ok": False,
            "commit": None,
            "branch": branch,
            "event_id": model.event_id,
            "event_path": event_path,
            "remote": self._control_remote,
            "push_error": last_error,
            "attempts": max_retries + 1,
        }

    async def _append_event_attempt(
        self,
        *,
        branch: str,
        event_path: str,
        event_json: str,
        event_id: str,
        worktree_path: str,
        timeout_seconds: int,
    ) -> dict:
        """单次 append+push 尝试：准备 worktree → 写文件 → commit → push。"""
        remote = self._control_remote
        remote_ref = f"refs/remotes/{remote}/{branch}"

        # 清理上一轮残留的 worktree（忽略错误——可能不存在）。
        await self._cli.run(
            self._repository_path,
            ["worktree", "remove", "--force", worktree_path],
            timeout_seconds,
        )

        # fetch 远端节点分支（首次可能不存在，忽略失败）。
        await self._cli.run(
            self._repository_path,
            ["fetch", "--", remote, branch],
            timeout_seconds,
        )

        # 检查远端跟踪分支是否存在。
        rc_check, _out_check, _err_check = await self._cli.run(
            self._repository_path,
            ["show-ref", "--verify", "--quiet", remote_ref],
            timeout_seconds,
        )

        if rc_check == 0:
            # 分支已存在：创建 worktree 并在其上创建本地分支跟踪远端。
            rc_wt, _out_wt, err_wt = await self._cli.run(
                self._repository_path,
                ["worktree", "add", "-B", branch, worktree_path, remote_ref],
                timeout_seconds,
            )
        else:
            # 分支不存在：创建 orphan worktree（首次注册事件）。
            # 注意：``--orphan`` 是一个标志（不接受 ``=value`` 语法，也不直接
            # 接分支名），需要配合 ``-b <branch>`` 指定新分支名。兼容
            # Git 2.42+ 的 ``worktree add`` 语法（在 Windows Git 2.45 上验证）。
            rc_wt, _out_wt, err_wt = await self._cli.run(
                self._repository_path,
                ["worktree", "add", "--orphan", "-b", branch, worktree_path],
                timeout_seconds,
            )

        if rc_wt != 0:
            return await self._attempt_failure(
                branch=branch,
                event_id=event_id,
                event_path=event_path,
                remote=remote,
                error=f"worktree add failed: {err_wt}",
                worktree_path=worktree_path,
                timeout_seconds=timeout_seconds,
            )

        # 写入事件文件。
        full_event_path = Path(worktree_path) / event_path
        full_event_path.parent.mkdir(parents=True, exist_ok=True)
        full_event_path.write_text(event_json + "\n", encoding="utf-8")

        # git add -- <event_path>（仅添加事件文件，不触碰其他文件）。
        rc_add, _out_add, err_add = await self._cli.run(
            worktree_path,
            ["add", "--", event_path],
            timeout_seconds,
        )
        if rc_add != 0:
            return await self._attempt_failure(
                branch=branch,
                event_id=event_id,
                event_path=event_path,
                remote=remote,
                error=f"git add failed: {err_add}",
                worktree_path=worktree_path,
                timeout_seconds=timeout_seconds,
            )

        # git commit。
        rc_commit, _out_commit, err_commit = await self._cli.run(
            worktree_path,
            ["commit", "-m", f"maf: append event {event_id}", "--no-edit"],
            timeout_seconds,
        )
        if rc_commit != 0:
            return await self._attempt_failure(
                branch=branch,
                event_id=event_id,
                event_path=event_path,
                remote=remote,
                error=f"git commit failed: {err_commit}",
                worktree_path=worktree_path,
                timeout_seconds=timeout_seconds,
            )

        # git push HEAD:refs/heads/<branch>（fast-forward）。
        rc_push, _out_push, err_push = await self._cli.run(
            worktree_path,
            ["push", "--", remote, f"HEAD:refs/heads/{branch}"],
            timeout_seconds,
        )

        if rc_push != 0:
            return await self._attempt_failure(
                branch=branch,
                event_id=event_id,
                event_path=event_path,
                remote=remote,
                error=f"git push failed: {err_push}",
                worktree_path=worktree_path,
                timeout_seconds=timeout_seconds,
            )

        # 获取 HEAD commit SHA。
        rc_sha, sha, _err_sha = await self._cli.run(
            worktree_path,
            ["rev-parse", "HEAD"],
            timeout_seconds,
        )
        commit = sha.strip() if rc_sha == 0 else None

        # 清理 worktree（成功路径）。
        await self._cli.run(
            self._repository_path,
            ["worktree", "remove", "--force", worktree_path],
            timeout_seconds,
        )

        return {
            "push_ok": True,
            "commit": commit,
            "branch": branch,
            "event_id": event_id,
            "event_path": event_path,
            "remote": remote,
            "push_error": None,
        }

    async def _attempt_failure(
        self,
        *,
        branch: str,
        event_id: str,
        event_path: str,
        remote: str,
        error: str,
        worktree_path: str,
        timeout_seconds: int,
    ) -> dict:
        """清理 worktree 并返回失败结果。"""
        await self._cli.run(
            self._repository_path,
            ["worktree", "remove", "--force", worktree_path],
            timeout_seconds,
        )
        return {
            "push_ok": False,
            "commit": None,
            "branch": branch,
            "event_id": event_id,
            "event_path": event_path,
            "remote": remote,
            "push_error": error,
        }


__all__ = [
    "FORBIDDEN_PUSH_BRANCHES",
    "GitCoordinationClient",
    "RunnerGitClient",
]
