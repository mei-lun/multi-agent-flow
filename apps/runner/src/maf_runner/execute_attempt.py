"""Execute one bounded Agent attempt and normalize its result."""

from maf_contracts.job import AttemptResult, TaskDispatchEnvelope
from maf_runner.runtime.agent_loop import run_agent
from maf_runner.runtime.artifact_packager import LocalArtifactPackager
from maf_runner.runtime.context_builder import LocalContextBuilder


class AttemptExecutor:
    def __init__(self, context_builder, *, agent_runner=run_agent, artifact_packager=None) -> None:
        self._context_builder = context_builder
        self._agent_runner = agent_runner
        self._packager = artifact_packager or LocalArtifactPackager()

    async def execute(
        self, envelope: TaskDispatchEnvelope, workspace_path: str
    ) -> AttemptResult:
        try:
            context = await self._context_builder.build(envelope, workspace_path)
            result = await self._agent_runner(context)
            if not isinstance(result, dict):
                raise RuntimeError("Agent loop returned a non-object result")
            loop_status = str(result.get("status", "FAILED"))
            declared = result.get("output_paths")
            if not isinstance(declared, list):
                declared = list((envelope.get("output_contract") or {}).get("required_paths", []))
            manifest = await self._packager.package_outputs(
                workspace_path, envelope.get("output_contract") or {}, declared
            )
            if loop_status == "COMPLETED":
                status = "SUBMITTED"
            elif loop_status == "CANCELLED":
                status = "CANCELLED"
            elif (result.get("error") or {}).get("code") == "APPROVAL_REQUIRED":
                status = "BLOCKED"
            else:
                status = "FAILED"
            return AttemptResult(
                task_id=envelope["task_id"],
                assignment_id=envelope["assignment_id"],
                assignment_epoch=envelope["assignment_epoch"],
                status=status,
                output_paths=[item["path"] for item in manifest["files"]],
                execution_summary=str(result.get("summary", loop_status)),
                self_check=[{"kind": "artifact_manifest", "result": manifest}],
                known_risks=list(result.get("known_risks", [])),
                remaining_items=list(result.get("remaining_items", [])),
                model_usage=dict(result.get("model_usage", {})),
                tool_usage=dict(result.get("tool_usage", {})),
                workspace_result=None,
                error=dict(result.get("error") or {}) or None,
            )
        except Exception as exc:
            return AttemptResult(
                task_id=str(envelope.get("task_id", "")),
                assignment_id=str(envelope.get("assignment_id", "")),
                assignment_epoch=int(envelope.get("assignment_epoch", 0)),
                status="FAILED",
                output_paths=[],
                execution_summary="attempt failed",
                self_check=[],
                known_risks=[],
                remaining_items=[],
                model_usage={},
                tool_usage={},
                workspace_result=None,
                error={"code": "ATTEMPT_FAILED", "message": str(exc), "retryable": False},
            )


_default_executor: AttemptExecutor | None = None


def configure_attempt_executor(executor: AttemptExecutor) -> None:
    global _default_executor
    _default_executor = executor


async def execute_attempt(envelope: TaskDispatchEnvelope, workspace_path: str) -> AttemptResult:
    """构建只含获授权能力的 AgentContext，运行有界 Agent Loop 并封装结果。

    不接受额外 Role/Skill/Tool/Model ID；配置来自 control 任务、仓库版本文件和节点本地映射。异常必须
    映射为 NormalizedError，部分输出只有通过 Artifact 校验后才可列入结果。
    """
    executor = _default_executor
    if executor is None:
        executor = AttemptExecutor(LocalContextBuilder())
    return await executor.execute(envelope, workspace_path)


__all__ = ["AttemptExecutor", "configure_attempt_executor", "execute_attempt"]
