"""TASK-001 契约测试：验证五类核心文档与 Git 协调协议一致。

验收标准：
1. 文档中不存在可执行的跨节点 ``/internal/v1`` 协议。
2. 五类核心文档对事实源、写入权和节点工作流描述一致。
3. ``doc/GitHub分布式协作协议.md`` 被所有相关文档引用。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOC_DIR = PROJECT_ROOT / "doc"

CORE_DOCS = [
    "多Agent协同工具产品需求文档-PRD.md",
    "多Agent协同工具需求分析文档.md",
    "多Agent协同工具系统设计文档.md",
    "项目框架与目录职责说明.md",
    "接口设计与实现规范.md",
]

PROTOCOL_DOC = "GitHub分布式协作协议.md"

# 旧 Runner HTTP / 中央队列 / Attempt Token 设计残留——清理后不应再出现。
FORBIDDEN_PATTERNS = [
    "runner_job_repo",
    "attempt_token",
    "register_runner",
    "claim_job",
    "server.heartbeat",
    "last_heartbeat_at",
    "host_task_queue",
    "Task Queue",
    "持久化队列",
    "Lease Coordinator",
    "长轮询",
]


def _read_doc(name: str) -> str:
    path = DOC_DIR / name
    assert path.exists(), f"文档不存在: {path}"
    return path.read_text(encoding="utf-8")


def _all_core_docs() -> dict[str, str]:
    return {name: _read_doc(name) for name in CORE_DOCS}


@pytest.fixture(scope="module")
def core_docs() -> dict[str, str]:
    return _all_core_docs()


# --------------------------------------------------------------------------- #
# 验收标准 1：不存在可执行的跨节点 /internal/v1 协议
# --------------------------------------------------------------------------- #


def test_no_executable_internal_v1_protocol(core_docs: dict[str, str]) -> None:
    """/internal/v1 只能出现在禁止声明中，不能作为可执行端点定义。"""
    prohibition_markers = ("不提供", "禁止", "不直接", "不自建", "不新增")
    offenders: list[str] = []
    for name, content in core_docs.items():
        for match in re.finditer(r"/internal/v1", content):
            line_start = content.rfind("\n", 0, match.start()) + 1
            line_end = content.find("\n", match.end())
            if line_end == -1:
                line_end = len(content)
            line = content[line_start:line_end]
            if not any(marker in line for marker in prohibition_markers):
                offenders.append(f"{name}: {line.strip()}")
    assert not offenders, (
        "以下行将 /internal/v1 作为可执行协议，而非禁止声明:\n" + "\n".join(offenders)
    )


# --------------------------------------------------------------------------- #
# 验收标准 2：五类核心文档对事实源、写入权和节点工作流描述一致
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("doc_name", CORE_DOCS)
def test_no_old_runner_http_patterns(core_docs: dict[str, str], doc_name: str) -> None:
    """旧 Runner HTTP / 中央队列 / Attempt Token 设计不应残留。"""
    content = core_docs[doc_name]
    found = [p for p in FORBIDDEN_PATTERNS if p in content]
    assert not found, f"{doc_name} 仍含旧设计模式: {found}"


def test_system_design_uses_git_coordination_baseline(core_docs: dict[str, str]) -> None:
    """系统设计文档以 Git 协调为唯一基线。"""
    sd = core_docs["多Agent协同工具系统设计文档.md"]
    for required in (
        "maf/control",
        "assignment_epoch",
        "capability_token",
        "git_coordination",
        "CLAIM_REQUESTED",
        "SUBMISSION_CREATED",
        "last_progress_at",
    ):
        assert required in sd, f"系统设计文档缺少 Git 协调关键词: {required}"


def test_no_old_queue_or_dispatcher_in_requirements(
    core_docs: dict[str, str],
) -> None:
    """需求分析文档不再描述中央队列或 Dispatcher 旧架构。"""
    req = core_docs["多Agent协同工具需求分析文档.md"]
    for forbidden in ("Dispatcher：派发到队列", "participant Q as Task Queue", "QUEUE["):
        assert forbidden not in req, f"需求分析文档仍含旧架构: {forbidden}"


def test_framework_describes_git_workflow(core_docs: dict[str, str]) -> None:
    """项目框架文档描述 Git 协调节点工作流，而非 HTTP 注册/长轮询。"""
    fw = core_docs["项目框架与目录职责说明.md"]
    assert "fetch control" in fw or "claim" in fw, "框架文档缺少 Git 协调节点工作流描述"


# --------------------------------------------------------------------------- #
# 验收标准 3：GitHub分布式协作协议.md 被所有相关文档引用
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("doc_name", CORE_DOCS)
def test_all_core_docs_reference_protocol(core_docs: dict[str, str], doc_name: str) -> None:
    """每篇核心文档都引用 GitHub 分布式协作协议。"""
    content = core_docs[doc_name]
    assert "GitHub分布式协作协议" in content, f"{doc_name} 未引用 GitHub 分布式协作协议"


def test_protocol_doc_exists() -> None:
    """协议文档本身存在。"""
    assert (DOC_DIR / PROTOCOL_DOC).exists(), "GitHub分布式协作协议.md 不存在"
