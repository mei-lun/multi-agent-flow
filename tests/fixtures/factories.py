"""测试数据工厂：固定 ID 与 ServerSettings/NodeSettings 构造器。

集中管理测试用固定标识符和配置构造，避免在各测试文件中重复定义
``_make_settings`` / ``_node_kwargs`` 等辅助函数。与现有测试的构造逻辑保持
一致，不引入新的必填字段。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from maf_runner.config import NodeSettings
from maf_server.config import ServerSettings

# 固定测试 ID（UUIDv7 风格，非真实节点身份）。
FIXED_NODE_ID = "node-12345678-1234-1234-1234-123456789abc"
FIXED_CONTROL_COMMIT = "abcdef1234567890abcdef1234567890abcdef12"
FIXED_ORG_ID = "org-test-001"

# 测试用 SecretKey（明显的非生产值，禁止用于真实部署）。
TEST_SECRET_KEY = "test-secret-key-not-for-production-use"


def make_server_settings(
    data_dir: Path,
    *,
    organization_id: str = FIXED_ORG_ID,
    secret_key: str = TEST_SECRET_KEY,
    **overrides: Any,
) -> ServerSettings:
    """构建测试用 ``ServerSettings``，数据库路径落在 ``data_dir`` 下。

    与 ``tests/integration/test_sqlite.py`` 等现有测试的 ``_make_settings``
    保持字段一致，``data_dir`` 必须为已存在的临时目录。
    """
    kwargs: dict[str, Any] = dict(
        organization_id=organization_id,
        business_db_path=Path("maf.db"),
        checkpointer_db_path=Path("checkpoints.db"),
        artifact_root=Path("artifacts"),
        workspace_root=Path("workspaces"),
        git_repo_root=data_dir / "repo",
        public_base_url="http://localhost:8000",
        secret_key=secret_key,
        data_dir=data_dir,
        _env_file=None,
    )
    kwargs.update(overrides)
    return ServerSettings(**kwargs)


def make_node_settings(
    workspace_root: Path,
    *,
    node_id: str = FIXED_NODE_ID,
    **overrides: Any,
) -> NodeSettings:
    """构建测试用 ``NodeSettings``。

    与 ``tests/unit/test_node_identity.py`` 的 ``_node_kwargs`` 保持字段一致。
    """
    kwargs: dict[str, Any] = dict(
        node_id=node_id,
        control_remote_url="origin",
        workspace_root=workspace_root,
        model_mapping_path=workspace_root / "model-mapping.yaml",
        capability_token_cache_path=Path("capability-tokens.db"),
        _env_file=None,
    )
    kwargs.update(overrides)
    return NodeSettings(**kwargs)


__all__ = [
    "FIXED_CONTROL_COMMIT",
    "FIXED_NODE_ID",
    "FIXED_ORG_ID",
    "TEST_SECRET_KEY",
    "make_node_settings",
    "make_server_settings",
]
