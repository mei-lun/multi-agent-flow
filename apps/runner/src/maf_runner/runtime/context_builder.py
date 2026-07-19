"""Build the immutable, least-privilege Agent execution context."""

import re
from pathlib import Path
from typing import Any, Protocol
from maf_contracts.job import TaskDispatchEnvelope
from maf_runner.security.boundaries import BoundaryViolation, LocalBoundaryValidator


class ContextBuilder(Protocol):
    async def build(self, envelope: TaskDispatchEnvelope, workspace_path: str) -> dict[str, Any]:
        """构造 Agent Loop 唯一上下文。

        校验 control commit、任务分支 base 和输入 hash；从仓库读取精确 Skill Version；从节点
        本地注册表取得允许 Tool/Model 映射；放入 Prompt、任务说明、输出 Contract、预算和取消
        信号。不得添加节点本机拥有但 envelope
        未授权的 Skill/Tool/Model；大输入只放索引和按需读取句柄。
        """
        ...


class LocalContextBuilder:
    """Pure context builder using only immutable envelope references."""

    _COMMIT = re.compile(r"^[0-9a-fA-F]{7,64}$")

    def __init__(
        self,
        *,
        skill_client: object | None = None,
        tool_registry: dict[str, object] | None = None,
        model_registry: dict[str, object] | None = None,
        workspace_root: str | None = None,
    ) -> None:
        self._skill_client = skill_client
        self._tools = dict(tool_registry or {})
        self._models = dict(model_registry or {})
        self._workspace_root = workspace_root
        self._boundary = LocalBoundaryValidator()

    async def build(
        self, envelope: TaskDispatchEnvelope, workspace_path: str
    ) -> dict[str, Any]:
        if not isinstance(envelope, dict):
            raise BoundaryViolation("dispatch envelope must be an object")
        epoch = envelope.get("assignment_epoch")
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            raise BoundaryViolation("assignment_epoch must be a positive integer")
        control_commit = str(envelope.get("based_on_control_commit", ""))
        if not self._COMMIT.fullmatch(control_commit):
            raise BoundaryViolation("based_on_control_commit is not a commit hash")
        workspace_spec = envelope.get("workspace")
        if not isinstance(workspace_spec, dict):
            raise BoundaryViolation("workspace spec is missing")
        base_commit = str(workspace_spec.get("base_commit", ""))
        if workspace_spec.get("kind") == "GIT" and not self._COMMIT.fullmatch(base_commit):
            raise BoundaryViolation("Git workspace base_commit is invalid")
        if self._workspace_root:
            workspace_path = self._boundary.require_workspace_path(
                self._workspace_root, workspace_path
            )
        elif not Path(workspace_path).resolve().is_dir():
            raise BoundaryViolation("workspace does not exist")

        role = envelope.get("role_version_ref") or {}
        if not isinstance(role, dict):
            raise BoundaryViolation("role_version_ref must be an object")
        if role.get("status") not in {None, "PUBLISHED"}:
            raise BoundaryViolation("role version is not published")
        for field, expected in (
            ("based_on_control_commit", control_commit),
            ("assignment_epoch", epoch),
            ("base_commit", base_commit),
        ):
            if field in role and role[field] != expected:
                raise BoundaryViolation(f"role snapshot {field} does not match envelope")

        requested_skills = [str(item) for item in role.get("skill_version_ids", [])]
        requested_tools = [str(item) for item in role.get("tool_keys", role.get("tools", []))]
        model_alias = str(role.get("model_alias", ""))
        if requested_skills and self._skill_client is None:
            raise BoundaryViolation("role requests skills but no verified SkillClient exists")
        missing_tools = [key for key in requested_tools if key not in self._tools]
        if missing_tools:
            raise BoundaryViolation(f"authorized tools unavailable locally: {missing_tools}")
        if model_alias and model_alias not in self._models:
            raise BoundaryViolation(f"authorized model alias unavailable locally: {model_alias}")

        input_refs = envelope.get("input_refs", [])
        if not isinstance(input_refs, list) or any(not isinstance(ref, str) for ref in input_refs):
            raise BoundaryViolation("input_refs must be opaque string references")
        return {
            "attempt_id": str(envelope.get("assignment_id", "")),
            "task_id": str(envelope.get("task_id", "")),
            "assignment_epoch": epoch,
            "control_commit": control_commit,
            "base_commit": base_commit,
            "workspace_path": workspace_path,
            "input_handles": [{"artifact_version_id": ref} for ref in input_refs],
            "output_contract": dict(envelope.get("output_contract") or {}),
            "skills": [{"version_id": skill_id} for skill_id in requested_skills],
            "tools": [self._tools[key] for key in requested_tools],
            "model_alias": model_alias,
            "model_client": self._models.get(model_alias),
            "max_steps": int(envelope.get("max_steps", 1)),
            "max_tool_calls": int(envelope.get("max_tool_calls", 0)),
            "timeout_seconds": int(envelope.get("timeout_seconds", 1)),
            "budget": dict(envelope.get("budget") or {}),
            "network_policy_ref": dict(envelope.get("network_policy_ref") or {}),
            "capability_policy_ref": dict(envelope.get("capability_policy_ref") or {}),
        }


__all__ = ["ContextBuilder", "LocalContextBuilder"]
