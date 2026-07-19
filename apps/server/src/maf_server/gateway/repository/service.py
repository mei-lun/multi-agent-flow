"""供运行、评审和最终合并使用的统一 Repository Gateway。"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

import structlog

from maf_contracts.repository import *  # noqa: F401,F403
from maf_contracts.coordination import CoordinationEventModel, CoordinationTask
from maf_domain.errors import ArgumentError, NotFoundError, ValidationError

from maf_server.gateway.repository.git_cli import ServerGitCli
from maf_server.gateway.secrets.service import SecretService


# --------------------------------------------------------------------------- #
# TASK-035: VerifyResult
# --------------------------------------------------------------------------- #


@dataclass
class VerifyResult:
    """``RepositoryAdapter.verify`` 的返回类型（TASK-035）。

    ``verified`` 为 True 表示仓库可访问且分支存在；``repository_info`` 含
    default_branch、branches、can_read、can_write 等脱敏元信息；
    ``error`` 在验证失败时含诊断信息（不含明文凭据）。
    """

    verified: bool
    repository_info: dict | None = None
    error: str | None = None


class RepositoryAdapter(Protocol):
    async def verify(
        self,
        repository_url: str,
        credentials: dict,
        *,
        expected_branch: str | None = None,
    ) -> VerifyResult:
        """无破坏验证仓库可访问性、分支存在性和所需权限（TASK-035）。

        :param repository_url: 仓库 URL（不含凭据）。
        :param credentials: 凭据字典，格式 ``{"type": "HTTPS_TOKEN"|"SSH_KEY"|"NONE",
            "token": "...", "ssh_key_path": "..."}``；明文仅短暂存在，不进日志。
        :param expected_branch: 期望存在的分支；``None`` 表示不检查特定分支。
        :returns: :class:`VerifyResult`，含脱敏仓库信息和诊断错误。
        """
        ...

    async def list_branches(
        self, repository_url: str, credentials: dict
    ) -> list[str]:
        """列出仓库远端分支名（不含 ``refs/heads/`` 前缀）。"""
        ...

    async def get_default_branch(
        self, repository_url: str, credentials: dict
    ) -> str:
        """返回仓库默认分支名（如 ``main`` 或 ``master``）。"""
        ...

    async def resolve_base(self, binding: dict, branch: str) -> CommitRef:  # type: ignore[name-defined] # noqa: F821
        """把 branch 解析为不可变 commit/tree；分支不存在明确失败。"""
        ...
    async def export_base_bundle(self, binding: dict, commit: str) -> str:  # type: ignore[name-defined] # noqa: F821
        """导出固定 commit 的只读 bundle/source archive，返回 Artifact Version ID。"""
        ...
    async def materialize_change(self, command: RepositoryCommand) -> BranchRef:  # type: ignore[name-defined] # noqa: F821
        """在受控工作区把 Patch 应用到固定 base，验证 tree 后创建/更新 run 分支。"""
        ...
    async def open_review(self, command: RepositoryCommand) -> ReviewRef:  # type: ignore[name-defined] # noqa: F821
        """创建 GitHub PR 或本地等价 Review；幂等键防止重复 PR。"""
        ...
    async def get_review(self, ref: ReviewRef) -> RepositoryReviewState:  # type: ignore[name-defined] # noqa: F821
        """读取实时 head/checks/approval/mergeable，不使用过期缓存作最终合并判断。"""
        ...
    async def merge(self, command: RepositoryCommand) -> MergeResult:  # type: ignore[name-defined] # noqa: F821
        """只有 expected head 精确匹配时执行配置的 merge method。"""
        ...


class RepositoryGateway(Protocol):
    async def verify_binding(self, binding_id: str) -> dict:
        """解析绑定与 Secret，选择 GitHub/Local Adapter 并返回健康结果。"""
        ...
    async def prepare_workspace(self, command: RepositoryCommand) -> str:  # type: ignore[name-defined] # noqa: F821
        """固定 base commit 后导出 Runner 可读 Artifact，不向 Runner 下发长期凭据。"""
        ...
    async def materialize_change(self, command: RepositoryCommand) -> BranchRef:  # type: ignore[name-defined] # noqa: F821
        """校验 Patch 来源、base 和 tree，在 Server 受控仓库生成 integration head。"""
        ...
    async def open_review(self, command: RepositoryCommand) -> ReviewRef:  # type: ignore[name-defined] # noqa: F821
        """确保分支已物化后创建 review，并保存外部引用。"""
        ...
    async def refresh_review(self, ref: ReviewRef) -> RepositoryReviewState:  # type: ignore[name-defined] # noqa: F821
        """刷新 PR/本地 Review 投影，产生状态变化事件。"""
        ...
    async def merge_review(self, command: RepositoryCommand) -> MergeResult:  # type: ignore[name-defined] # noqa: F821
        """再次校验 expected head 后调用 Adapter；Gateway 不自行判断产品 Gate 是否通过。"""
        ...


@dataclass(frozen=True)
class SubmissionValidationResult:
    branch: str
    base_commit: str
    head_commit: str
    changed_paths: tuple[str, ...]


class SubmissionBranchValidator:
    """Validate a node submission against immutable Git facts (TASK-026)."""

    _SAFE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")

    def __init__(self, *, git_cli: Any, repository_path: str) -> None:
        self._git_cli = git_cli
        self._repository_path = repository_path

    async def validate_submission(
        self,
        event: CoordinationEventModel,
        current_task: CoordinationTask,
        *,
        current_control_commit: str | None = None,
    ) -> SubmissionValidationResult:
        if event.event_type != "SUBMISSION_CREATED":
            raise ArgumentError("submission validator requires SUBMISSION_CREATED")
        assignment = current_task.get("assignment") or {}
        task_id = str(current_task.get("task_id", ""))
        epoch = assignment.get("assignment_epoch")
        owner = assignment.get("node_id")
        assignment_id = assignment.get("assignment_id")
        if not task_id or not assignment or epoch is None:
            raise ValidationError("task has no current assignment")
        if event.task_id != task_id or event.node_id != owner:
            raise ValidationError("submission owner or task does not match current assignment")
        if event.assignment_id != assignment_id or event.assignment_epoch != epoch:
            raise ValidationError("submission assignment id or epoch is stale")
        if current_control_commit and event.based_on_control_commit != current_control_commit:
            raise ValidationError("submission is based on a stale control commit")

        payload = event.payload
        branch = payload.get("branch")
        base = payload.get("base_commit")
        head = payload.get("head_commit")
        changed_paths = payload.get("changed_paths")
        if not all(isinstance(value, str) and value for value in (branch, base, head)):
            raise ValidationError("submission branch, base_commit and head_commit are required")
        if not isinstance(changed_paths, list) or any(
            not isinstance(path, str) or not path for path in changed_paths
        ):
            raise ValidationError("submission changed_paths must be a string list")
        if not self._SAFE_BRANCH.fullmatch(branch) or ".." in branch:
            raise ValidationError("submission branch name is invalid")
        expected_branch = f"maf/task/{task_id}/e{epoch}-{owner}"
        if branch != expected_branch:
            raise ValidationError(
                "submission branch is outside the current assignment",
                context={"expected_branch": expected_branch, "actual_branch": branch},
            )

        resolved_head = await self._rev_parse(f"refs/heads/{branch}")
        if resolved_head != head:
            raise ValidationError("submission head does not match task branch head")
        await self._rev_parse(base)
        await self._rev_parse(head)
        rc, _out, _err = await self._git_cli.run(
            self._repository_path,
            # ``merge-base`` is intentionally not in the server Git CLI
            # allow-list. If base is reachable from head, ``base --not head``
            # has no output; a divergent base leaves at least one commit.
            ["rev-list", "--max-count=1", base, "--not", head],
            0,
        )
        if rc != 0 or _out.strip():
            raise ValidationError("submission base is not an ancestor of head")
        expected_base = (current_task.get("delivery") or {}).get("base_commit")
        if expected_base and base != expected_base:
            raise ValidationError("submission base differs from assigned base")

        rc, out, err = await self._git_cli.run(
            self._repository_path,
            ["diff", "--name-only", f"{base}..{head}"],
            0,
        )
        if rc != 0:
            raise ValidationError(f"cannot inspect submission diff: {err.strip()}")
        actual_paths = tuple(sorted(line.strip() for line in out.splitlines() if line.strip()))
        claimed_paths = tuple(sorted(set(changed_paths)))
        if actual_paths != claimed_paths:
            raise ValidationError(
                "submission changed_paths do not match Git diff",
                context={"claimed": claimed_paths, "actual": actual_paths},
            )
        if any(path == ".maf" or path.startswith(".maf/") for path in actual_paths):
            raise ValidationError("task submission must not modify control protocol files")
        allowed_paths = (current_task.get("requirements") or {}).get("allowed_paths", [])
        if allowed_paths and any(
            not any(fnmatch(path, pattern) for pattern in allowed_paths)
            for path in actual_paths
        ):
            raise ValidationError("submission modifies paths outside task scope")

        has_test_evidence = any(
            (
                isinstance(payload.get(field), str) and bool(payload.get(field).strip())
            )
            or (isinstance(payload.get(field), list) and bool(payload.get(field)))
            for field in ("test_summary", "test_report_path", "test_evidence")
        )
        if not has_test_evidence:
            raise ValidationError("submission must include test evidence")
        return SubmissionValidationResult(branch, base, head, actual_paths)

    async def _rev_parse(self, ref: str) -> str:
        rc, out, err = await self._git_cli.run(
            self._repository_path, ["rev-parse", "--verify", ref], 0
        )
        if rc != 0:
            raise ValidationError(f"submission Git ref does not exist: {err.strip()}")
        return out.strip()


# --------------------------------------------------------------------------- #
# TASK-014: Git 凭据与远端验证
# --------------------------------------------------------------------------- #


#: ``verify`` 是 SecretService 默认允许的 resolve purpose（见 LocalSecretService）。
_VERIFY_PURPOSE: str = "verify"

#: 健康报告中脱敏 URL 凭据时使用的占位符。
_REDACTED_URL_PLACEHOLDER: str = "***"

#: SSH key 路径禁止出现的 shell 元字符（防止 GIT_SSH_COMMAND 注入）。
_SSH_PATH_FORBIDDEN_CHARS: frozenset[str] = frozenset(
    {";", "|", "&", "$", "`", "(", ")", "\n", "\r", " ", "\t"}
)


@dataclass
class GitBinding:
    """仓库绑定记录（无明文）。

    ``secret_id`` 引用 SecretStore 中的 HTTPS token；``ssh_key_path`` 指向
    受控 SSH 私钥文件。两者互斥：HTTPS 绑定用 ``secret_id``，SSH 绑定用
    ``ssh_key_path``。明文绝不进入本数据类。
    """

    binding_id: str
    remote_url: str
    credential_type: Literal["HTTPS_TOKEN", "SSH_KEY"] = "HTTPS_TOKEN"
    secret_id: str | None = None
    ssh_key_path: str | None = None
    allowed_push_branches: list[str] | None = None


class LocalRepositoryGateway:
    """Server-side RepositoryGateway 具体实现（TASK-014）。

    使用 :class:`ServerGitCli` + :class:`SecretService` 验证 Git 远端可访问性
    与权限。凭据经环境变量注入子进程（HTTPS token 经 ``MAF_GIT_CREDENTIAL_TOKEN``，
    SSH 经 ``GIT_SSH_COMMAND``），绝不进入命令行参数或日志（由
    :class:`SubprocessGitCli` 保证）。

    设计决策：

    - 每个 binding 创建独立的 :class:`ServerGitCli` 实例，将凭据经 ``extra_env``
      注入子进程；凭据值不进入命令行参数、不进入日志。
    - HTTPS token 经 ``SecretService.resolve(purpose="verify")`` 短暂取回，用完
      由 Python GC 释放；明文只在该方法栈帧内存在。
    - SSH 路径必须为绝对路径且存在；``GIT_SSH_COMMAND`` 仅包含
      ``ssh -i <path>`` 形式，路径经校验不含 shell 元字符。
    - 验证流程：``git fetch --dry-run`` 探测读权限，``git push --dry-run`` 探测
      写权限（推送到临时 ``_maf_verify_*`` 分支）；两者均不修改远端状态。
    - 健康报告脱敏：remote URL 中的 ``https://<token>@`` 被替换为 ``***@``；
      错误信息原样保留（git 输出不包含明文凭据，因凭据经环境变量传递）。
    """

    def __init__(
        self,
        *,
        git_repo_root: Path,
        secret_service: SecretService | None = None,
        control_branch: str = "maf/control",
        actor_id: str = "system",
        bindings: dict[str, GitBinding] | None = None,
        fetch_timeout_seconds: int = 30,
        logger: Any = None,
    ) -> None:
        self._git_repo_root: Path = Path(git_repo_root)
        self._secret_service: SecretService | None = secret_service
        self._control_branch: str = control_branch
        self._actor_id: str = actor_id
        self._bindings: dict[str, GitBinding] = dict(bindings) if bindings else {}
        self._fetch_timeout: int = max(1, fetch_timeout_seconds)
        self._log: Any = logger or structlog.get_logger("maf.repository_gateway")

    # ------------------------------------------------------------------ #
    # 绑定注册
    # ------------------------------------------------------------------ #

    def register_binding(self, binding: GitBinding) -> None:
        """注册或更新绑定记录（不含明文）。"""
        self._bindings[binding.binding_id] = binding

    # ------------------------------------------------------------------ #
    # RepositoryGateway Protocol 实现
    # ------------------------------------------------------------------ #

    async def verify_binding(self, binding_id: str) -> dict:
        """验证远端可访问且权限正确，返回脱敏健康报告。

        流程：

        1. 查找绑定（未找到抛 :class:`NotFoundError`）。
        2. 根据 ``credential_type`` 构建凭据 env（HTTPS token 或 SSH key）。
        3. ``git fetch --dry-run <remote_url>`` 验证读权限（不修改本地/远端）。
        4. 读权限通过后，``git push --dry-run <remote_url> HEAD:refs/heads/_maf_verify_*``
           验证写权限（不修改远端）。
        5. 返回脱敏健康报告：``binding_id``、``remote_url``（已脱敏）、
           ``credential_type``、``fetch_accessible``、``push_permitted``、
           ``fetch_error``、``push_error``、``checked_at``。

        凭据绝不进入命令行参数或日志（由 :class:`SubprocessGitCli` 保证）。
        """
        binding = self._bindings.get(binding_id)
        if binding is None:
            raise NotFoundError(
                f"binding {binding_id!r} not found",
                context={"binding_id": binding_id},
            )

        # 构建凭据 env（值不记录到日志）
        extra_env = await self._build_credential_env(binding)

        # 每个 binding 独立 ServerGitCli，凭据经 extra_env 注入子进程
        cli = ServerGitCli(
            git_repo_root=self._git_repo_root,
            extra_env=extra_env,
            default_timeout_seconds=self._fetch_timeout,
        )

        # 1. fetch --dry-run 验证读权限（不修改本地/远端状态）
        rc_fetch, _out_fetch, err_fetch = await cli.run(
            str(self._git_repo_root),
            ["fetch", "--dry-run", "--", binding.remote_url],
            self._fetch_timeout,
        )
        fetch_ok = rc_fetch == 0

        # 2. push --dry-run 验证写权限（推送到临时验证分支，不修改远端）
        push_ok = False
        push_error: str | None = None
        if fetch_ok:
            safe_id = self._sanitize_binding_id(binding_id)
            verify_branch = f"_maf_verify_{safe_id}"
            rc_push, _out_push, err_push = await cli.run(
                str(self._git_repo_root),
                [
                    "push",
                    "--dry-run",
                    "--",
                    binding.remote_url,
                    f"HEAD:refs/heads/{verify_branch}",
                ],
                self._fetch_timeout,
            )
            push_ok = rc_push == 0
            push_error = err_push if not push_ok else None

        # 3. 构建脱敏健康报告
        report: dict[str, Any] = {
            "binding_id": binding_id,
            "remote_url": self._redact_remote_url(binding.remote_url),
            "credential_type": binding.credential_type,
            "fetch_accessible": fetch_ok,
            "push_permitted": push_ok,
            "fetch_error": err_fetch if not fetch_ok else None,
            "push_error": push_error,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

        self._log.info(
            "repository_binding_verified",
            binding_id=binding_id,
            credential_type=binding.credential_type,
            fetch_accessible=fetch_ok,
            push_permitted=push_ok,
        )
        return report

    # ------------------------------------------------------------------ #
    # 凭据装配
    # ------------------------------------------------------------------ #

    async def _build_credential_env(self, binding: GitBinding) -> dict[str, str]:
        """构建凭据 env。值不记录到日志。

        - HTTPS_TOKEN：经 SecretService.resolve(purpose="verify") 取回 token，
          注入 ``MAF_GIT_CREDENTIAL_TOKEN``（由部署方提供的 askpass helper 读取）。
        - SSH_KEY：校验 key 路径后构造 ``GIT_SSH_COMMAND=ssh -i <path> ...``。
        """
        env: dict[str, str] = {}
        if binding.credential_type == "HTTPS_TOKEN":
            if binding.secret_id and self._secret_service is not None:
                token = await self._secret_service.resolve(
                    binding.secret_id,
                    purpose=_VERIFY_PURPOSE,
                    actor_id=self._actor_id,
                )
                # 平台私有 env，由 askpass helper 读取（helper 由部署方提供）。
                env["MAF_GIT_CREDENTIAL_TOKEN"] = token
        elif binding.credential_type == "SSH_KEY":
            if binding.ssh_key_path:
                self._validate_ssh_key_path(binding.ssh_key_path)
                # GIT_SSH_COMMAND 经 extra_env 注入；SubprocessGitCli 保证不进日志。
                env["GIT_SSH_COMMAND"] = (
                    "ssh -o IdentitiesOnly=yes -o BatchMode=yes "
                    f"-o StrictHostKeyChecking=accept-new "
                    f"-i {binding.ssh_key_path}"
                )
        return env

    def _validate_ssh_key_path(self, path: str) -> None:
        """校验 SSH key 路径：必须绝对、存在、是文件，不含 shell 元字符。"""
        if not path:
            raise ArgumentError("ssh_key_path must not be empty")
        # 防止 shell 元字符注入到 GIT_SSH_COMMAND（路径被拼入命令字符串）。
        for ch in _SSH_PATH_FORBIDDEN_CHARS:
            if ch in path:
                raise ArgumentError(
                    "ssh_key_path contains forbidden characters",
                    context={"ssh_key_path": path},
                )
        p = Path(path)
        if not p.is_absolute():
            raise ArgumentError(
                "ssh_key_path must be an absolute path",
                context={"ssh_key_path": path},
            )
        if not p.is_file():
            raise ArgumentError(
                "ssh_key_path does not exist or is not a regular file",
                context={"ssh_key_path": path},
            )

    # ------------------------------------------------------------------ #
    # 输出脱敏
    # ------------------------------------------------------------------ #

    @staticmethod
    def _sanitize_binding_id(binding_id: str) -> str:
        """将 binding_id 转为分支安全字符串（仅字母数字和连字符）。"""
        cleaned = re.sub(r"[^A-Za-z0-9]+", "-", binding_id).strip("-")
        return cleaned or "unknown"

    @staticmethod
    def _redact_remote_url(url: str) -> str:
        """脱敏 remote URL 中的凭据片段。

        覆盖两种形式：
        - ``https://<user>:<pass>@host/...`` → ``https://***@host/...``
        - ``https://<token>@host/...`` → ``https://***@host/...``
        """
        redacted = re.sub(
            r"(https?://)[^@/:]+:[^@/:]+@",
            r"\1" + _REDACTED_URL_PLACEHOLDER + "@",
            url,
        )
        redacted = re.sub(
            r"(https?://)[^@/:]+@",
            r"\1" + _REDACTED_URL_PLACEHOLDER + "@",
            redacted,
        )
        return redacted


__all__ = [
    "GitBinding",
    "LocalRepositoryGateway",
    "RepositoryAdapter",
    "RepositoryGateway",
    "VerifyResult",
]
