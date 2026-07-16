"""代码任务 worktree、分支元数据和 Patch 输出接口。"""

from typing import Protocol


class GitWorkspace(Protocol):
    async def prepare(self, job_id: str, source_artifact_version_id: str, base_commit: str, expected_tree_hash: str, writable_subpaths: list[str]) -> str:
        """在新目录导入 bundle/archive，校验 commit/tree，创建本地 worktree；不配置远端凭据。"""
        ...
    async def collect(self, workspace_path: str) -> dict:
        """检查改动未越过允许路径，生成 Patch、可选 bundle、producer commit 和 tree hash。"""
        ...
    async def cleanup(self, workspace_path: str) -> None:
        """路径必须在 Runner workspace root，清理前停止使用它的容器。"""
        ...
