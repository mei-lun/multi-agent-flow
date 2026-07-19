"""pytest 全局 fixture：TASK-010 测试基线。

提供跨测试目录复用的 fixture：

- ``_clear_maf_env``（autouse）：清除 ``MAF_*`` 环境变量，保证测试隔离；
- ``tmp_project_dir``：显式 ``try/finally`` 清理的临时目录；
- ``virtual_clock``：可推进的虚拟时钟（实现 ``Clock`` Protocol）；
- ``git_repo`` / ``empty_git_repo``：本地 Git 仓库（真实 git，不依赖 GitHub）；
- ``mock_model_adapter`` / ``mock_tool_adapter``：假模型/工具 Adapter（不依赖真实 Key）；
- ``server_settings_factory`` / ``node_settings_factory``：测试配置工厂；
- ``fixed_node_id`` / ``fixed_control_commit``：固定测试 ID。

设计原则：
- 不依赖真实 GitHub 或模型 API Key（用本地 git + mock adapter）；
- 临时资源在成功和失败后均清理（``yield`` + ``finally``）；
- 不破坏现有测试：现有测试模块的同名局部 fixture 优先级更高，pytest 优先
  使用局部定义，不会重复执行。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

# 将 tests/ 加入 sys.path，使 ``fixtures`` 包可导入（与现有测试在模块顶部
# 显式 ``sys.path.insert`` 的模式一致，不污染 pyproject.toml 的 pythonpath）。
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from fixtures.factories import (  # noqa: E402
    FIXED_CONTROL_COMMIT,
    FIXED_NODE_ID,
    make_node_settings,
    make_server_settings,
)
from fixtures.git_repo import LocalGitRepo, init_local_git_repo  # noqa: E402
from fixtures.mock_providers import MockModelAdapter, MockToolAdapter  # noqa: E402
from fixtures.temp_dir import cleanup_temp_dir, make_temp_dir  # noqa: E402
from fixtures.virtual_clock import VirtualClock  # noqa: E402


# --------------------------------------------------------------------------- #
# autouse: 清除 MAF_* 环境变量
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_maf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除所有 ``MAF_*`` 环境变量，避免本地 ``.env`` 污染测试。

    现有测试模块若已定义同名 fixture，pytest 优先使用局部定义，不重复执行；
    未定义的模块自动获得此默认清理。
    """
    for key in list(os.environ):
        if key.startswith("MAF_"):
            monkeypatch.delenv(key, raising=False)


# --------------------------------------------------------------------------- #
# 临时目录（显式清理）
# --------------------------------------------------------------------------- #


@pytest.fixture
def tmp_project_dir() -> Iterator[Path]:
    """显式创建并清理的临时目录，在成功和失败后均清理。

    与 pytest 内置 ``tmp_path`` 互补：本 fixture 用 ``try/finally`` 保证即使
    测试抛异常也执行清理，并可在测试中显式断言目录已删除。
    """
    path = make_temp_dir()
    try:
        yield path
    finally:
        cleanup_temp_dir(path)


# --------------------------------------------------------------------------- #
# 虚拟时钟
# --------------------------------------------------------------------------- #


@pytest.fixture
def virtual_clock() -> VirtualClock:
    """可手动推进的虚拟时钟，实现 ``Clock`` Protocol（``now``/``wait_until``）。

    与设计文档 §27.4「使用可控时钟测试审批和 lease 超时」对齐。
    """
    return VirtualClock()


# --------------------------------------------------------------------------- #
# 本地 Git 仓库
# --------------------------------------------------------------------------- #


@pytest.fixture
def git_repo(tmp_path: Path) -> LocalGitRepo:
    """本地 Git 仓库（真实 ``git init`` + 初始提交），不依赖 GitHub。"""
    return init_local_git_repo(tmp_path / "repo")


@pytest.fixture
def empty_git_repo(tmp_path: Path) -> LocalGitRepo:
    """本地 Git 仓库但不带初始提交，供需要从空状态开始的测试使用。"""
    return init_local_git_repo(tmp_path / "empty-repo", initial_commit=False)


# --------------------------------------------------------------------------- #
# Mock Model / Tool Adapter
# --------------------------------------------------------------------------- #


@pytest.fixture
def mock_model_adapter() -> MockModelAdapter:
    """假模型 Adapter，返回固定成功响应，不依赖真实模型 API Key。"""
    return MockModelAdapter()


@pytest.fixture
def mock_tool_adapter() -> MockToolAdapter:
    """假 Tool Adapter，回显参数，不执行真实副作用。"""
    return MockToolAdapter()


# --------------------------------------------------------------------------- #
# 配置工厂
# --------------------------------------------------------------------------- #


@pytest.fixture
def server_settings_factory(tmp_path: Path):
    """``ServerSettings`` 工厂，数据库路径落在 ``tmp_path`` 下。"""

    def _factory(**overrides: Any) -> Any:
        return make_server_settings(tmp_path, **overrides)

    return _factory


@pytest.fixture
def node_settings_factory(tmp_path: Path):
    """``NodeSettings`` 工厂。"""

    def _factory(**overrides: Any) -> Any:
        return make_node_settings(tmp_path, **overrides)

    return _factory


# --------------------------------------------------------------------------- #
# 固定常量（便于测试引用）
# --------------------------------------------------------------------------- #


@pytest.fixture
def fixed_node_id() -> str:
    return FIXED_NODE_ID


@pytest.fixture
def fixed_control_commit() -> str:
    return FIXED_CONTROL_COMMIT
