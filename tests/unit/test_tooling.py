"""TASK-002 单元测试：验证 Monorepo 依赖与命令脚手架存在且可读。

验收标准：
1. 关键依赖锁定文件（``pyproject.toml``、``package.json``、``pnpm-workspace.yaml``）存在且非空。
2. 开发命令脚本（``scripts/bootstrap.ps1``、``scripts/verify.ps1``）存在且包含必要步骤。
3. ``pyproject.toml`` 声明运行时依赖、``dev`` 可选依赖、``[tool.mypy]`` 最小配置，
   且保留 TASK-001 已配置的 ``[tool.pytest.ini_options]`` 与 ``[tool.ruff]``。
4. 根 ``package.json`` 声明 ``scripts`` 与共享 ``devDependencies``；
   ``pnpm-workspace.yaml`` 至少声明 ``apps/web``。

测试只做文件存在性与结构断言，不实际执行 bootstrap/verify 命令。
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"


def _read_text(path: Path) -> str:
    assert path.exists(), f"文件不存在: {path}"
    content = path.read_text(encoding="utf-8")
    assert content.strip(), f"文件为空: {path}"
    return content


# --------------------------------------------------------------------------- #
# 验收标准 1：关键依赖锁定文件存在且非空
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "rel_path",
    [
        "pyproject.toml",
        "package.json",
        "pnpm-workspace.yaml",
    ],
)
def test_dependency_lock_file_exists(rel_path: str) -> None:
    file_path = PROJECT_ROOT / rel_path
    _read_text(file_path)


# --------------------------------------------------------------------------- #
# 验收标准 2：开发命令脚本存在且包含必要步骤
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "rel_path",
    [
        "scripts/bootstrap.ps1",
        "scripts/verify.ps1",
    ],
)
def test_dev_script_exists_and_readable(rel_path: str) -> None:
    _read_text(PROJECT_ROOT / rel_path)


def test_bootstrap_ps1_installs_python_and_web_deps() -> None:
    """bootstrap.ps1 必须同时安装 Python (pip install) 与 Web (pnpm install) 依赖。"""
    content = _read_text(SCRIPTS_DIR / "bootstrap.ps1")
    assert "pip install" in content, "bootstrap.ps1 缺少 pip install 步骤"
    assert 'pnpm install' in content, "bootstrap.ps1 缺少 pnpm install 步骤"


def test_verify_ps1_runs_pytest_and_ruff() -> None:
    """verify.ps1 必须运行 pytest 和 ruff check。"""
    content = _read_text(SCRIPTS_DIR / "verify.ps1")
    assert "pytest" in content, "verify.ps1 缺少 pytest 步骤"
    assert "ruff check" in content, "verify.ps1 缺少 ruff check 步骤"


# --------------------------------------------------------------------------- #
# 验收标准 3：pyproject.toml 声明依赖与工具配置
# --------------------------------------------------------------------------- #


def _load_pyproject() -> dict:
    return tomllib.loads(_read_text(PROJECT_ROOT / "pyproject.toml"))


def test_pyproject_declares_runtime_dependencies() -> None:
    """pyproject.toml 声明与系统设计文档一致的运行时依赖。"""
    data = _load_pyproject()
    deps = data.get("project", {}).get("dependencies", [])
    assert deps, "pyproject.toml 缺少 project.dependencies"
    required = {
        "fastapi",
        "pydantic",
        "pydantic-settings",
        "structlog",
        "sqlalchemy",
        "aiosqlite",
        "casbin",
        "httpx",
    }
    declared = {re.split(r"[>=<~!\[]", d, maxsplit=1)[0].strip().lower() for d in deps}
    missing = required - declared
    assert not missing, f"pyproject.toml 缺少运行时依赖: {missing}"


def test_pyproject_declares_dev_optional_dependencies() -> None:
    """pyproject.toml 在 [project.optional-dependencies].dev 声明开发工具。"""
    data = _load_pyproject()
    dev = data.get("project", {}).get("optional-dependencies", {}).get("dev", [])
    assert dev, "pyproject.toml 缺少 [project.optional-dependencies].dev"
    required = {"pytest", "pytest-asyncio", "ruff", "mypy"}
    declared = {re.split(r"[>=<~!\[]", d, maxsplit=1)[0].strip().lower() for d in dev}
    missing = required - declared
    assert not missing, f"pyproject.toml 缺少开发依赖: {missing}"


def test_pyproject_keeps_pytest_and_ruff_config() -> None:
    """TASK-001 已配置的 [tool.pytest.ini_options] 与 [tool.ruff] 必须保留。"""
    data = _load_pyproject()
    tools = data.get("tool", {})
    assert "pytest" in tools, "pyproject.toml 缺少 [tool.pytest.ini_options]"
    pytest_cfg = tools["pytest"].get("ini_options")
    assert pytest_cfg, "pyproject.toml 缺少 [tool.pytest.ini_options] 内容"
    assert "ruff" in tools, "pyproject.toml 缺少 [tool.ruff]"
    assert tools["ruff"].get("line-length") == 100, "[tool.ruff] line-length 应为 100"
    assert tools["ruff"].get("target-version") == "py311", "[tool.ruff] target-version 应为 py311"


def test_pyproject_ruff_lint_ignores_placeholder_patterns() -> None:
    """[tool.ruff.lint] 必须忽略占位文件使用的星号导入与预留类型导入。

    TASK-001 留下的 Protocol 桩依赖 ``from .schemas import *``；当 schemas 在
    TASK-005+ 落地后，这些忽略项应被逐步移除。
    """
    data = _load_pyproject()
    lint = data.get("tool", {}).get("ruff", {}).get("lint")
    assert lint is not None, "pyproject.toml 缺少 [tool.ruff.lint]"
    ignored = lint.get("ignore", [])
    for code in ("F401", "F403", "F405"):
        assert code in ignored, f"[tool.ruff.lint].ignore 缺少 {code}"


def test_pyproject_declares_mypy_config() -> None:
    """pyproject.toml 包含 [tool.mypy] 最小配置。"""
    data = _load_pyproject()
    mypy = data.get("tool", {}).get("mypy")
    assert mypy is not None, "pyproject.toml 缺少 [tool.mypy]"
    assert mypy.get("python_version") == "3.11", "[tool.mypy] python_version 应为 3.11"
    assert mypy.get("ignore_missing_imports") is True, (
        "[tool.mypy] ignore_missing_imports 应为 true"
    )


# --------------------------------------------------------------------------- #
# 验收标准 4：根 package.json 与 pnpm-workspace.yaml 结构
# --------------------------------------------------------------------------- #


def _load_package_json() -> dict:
    return json.loads(_read_text(PROJECT_ROOT / "package.json"))


def test_root_package_json_is_private_with_scripts_and_devdeps() -> None:
    """根 package.json 必须为私有包，并声明 scripts 与共享 devDependencies。"""
    data = _load_package_json()
    assert data.get("private") is True, "package.json 必须为私有包 (private: true)"
    scripts = data.get("scripts", {})
    assert scripts, "package.json 缺少 scripts"
    for script in ("dev", "build", "lint", "test"):
        assert script in scripts, f"package.json scripts 缺少 {script}"
    dev_deps = data.get("devDependencies", {})
    assert dev_deps, "package.json 缺少 devDependencies"
    required_dev = {"typescript", "vite", "eslint", "prettier"}
    declared_dev = {k.lower() for k in dev_deps}
    missing = required_dev - declared_dev
    assert not missing, f"package.json 缺少共享开发依赖: {missing}"


def test_pnpm_workspace_declares_web_package() -> None:
    """pnpm-workspace.yaml 至少声明 apps/web。"""
    content = _read_text(PROJECT_ROOT / "pnpm-workspace.yaml")
    assert content.lstrip().startswith("packages:"), (
        "pnpm-workspace.yaml 必须以 packages: 开头"
    )
    assert "apps/web" in content, "pnpm-workspace.yaml 未声明 apps/web"
