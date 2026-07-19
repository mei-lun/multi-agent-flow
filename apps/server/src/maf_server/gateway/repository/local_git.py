"""本地 bare/working-tree Git Adapter 接口说明。

实现必须把配置路径解析到允许的 repository roots 内，使用独立 worktree，不能在用户当前
工作树直接清理、reset 或覆盖未提交文件。本地 Review 使用系统内记录代替 GitHub PR。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


class LocalGitReviewAdapter:
    """在受控 worktree 中物化变更、保存本地 Review 并执行显式合并。

    用户当前工作树从不被 checkout/reset；所有临时状态位于 ``workspace_root``。
    Review 以 ``idempotency_key`` 去重，且 merge 前重新读取 head，避免审批后悄然漂移。
    """

    def __init__(self, repository_root: Path, *, workspace_root: Path | None = None) -> None:
        self.repository_root = Path(repository_root).resolve()
        if not self.repository_root.exists():
            raise ValueError("repository root does not exist")
        self.workspace_root = (Path(workspace_root) if workspace_root else self.repository_root / ".maf-review").resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        try:
            self.workspace_root.relative_to(self.repository_root)
        except ValueError:
            # A separate temporary root is allowed, but it must never be the user worktree.
            if self.workspace_root == self.repository_root:
                raise ValueError("review workspace cannot be repository root")
        self._reviews: dict[str, dict[str, Any]] = {}

    async def _git(self, cwd: Path, *args: str) -> str:
        def run() -> str:
            result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
            return result.stdout.strip()
        return await asyncio.to_thread(run)

    @staticmethod
    def _safe(value: str) -> str:
        cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in str(value))
        return cleaned.strip("-") or "review"

    async def materialize_change(self, command: dict[str, Any]) -> dict[str, str]:
        base = str(command.get("base_commit") or "")
        branch = str(command.get("work_branch") or "")
        if not base or not branch or branch in {"main", "master"} or ".." in branch:
            raise ValueError("fixed base_commit and non-protected work_branch are required")
        name = self._safe(str(command.get("run_id") or command.get("task_id") or hashlib.sha256(branch.encode()).hexdigest()[:12]))
        worktree = self.workspace_root / f"materialize-{name}"
        if worktree.exists():
            shutil.rmtree(worktree)
        await self._git(self.repository_root, "worktree", "add", "--detach", str(worktree), base)
        try:
            await self._git(worktree, "switch", "-c", branch)
            patch_path = command.get("patch_path")
            if patch_path:
                patch = Path(str(patch_path)).resolve()
                if not patch.is_file():
                    raise ValueError("patch_path is not a regular file")
                await self._git(worktree, "apply", "--whitespace=nowarn", str(patch))
                await self._git(worktree, "add", "--all")
                await self._git(worktree, "-c", "user.name=MAF Review", "-c", "user.email=maf-review@localhost", "commit", "-m", "maf: materialize review change")
            head = await self._git(worktree, "rev-parse", "HEAD")
            tree = await self._git(worktree, "rev-parse", "HEAD^{tree}")
            return {"branch": branch, "head_commit": head, "tree": tree, "workspace_path": str(worktree)}
        except Exception:
            await self.cleanup(worktree)
            raise

    async def open_review(self, command: dict[str, Any]) -> dict[str, Any]:
        key = str(command.get("idempotency_key") or "")
        if not key:
            raise ValueError("idempotency_key is required")
        existing = self._reviews.get(key)
        if existing is not None:
            return dict(existing)
        head = str(command.get("head_commit") or "")
        base = str(command.get("base_commit") or "")
        if not head or not base:
            raise ValueError("base_commit and head_commit are required")
        review = {
            "provider": "LOCAL_GIT", "external_id": f"local-{hashlib.sha256(key.encode()).hexdigest()[:24]}",
            "url": None, "head_commit": head, "base_commit": base,
            "state": "OPEN", "checks_state": "PENDING", "approvals": 0,
            "changes_requested": False, "mergeable": True, "idempotency_key": key,
        }
        self._reviews[key] = review
        return dict(review)

    async def refresh_review(self, ref: dict[str, Any]) -> dict[str, Any]:
        for review in self._reviews.values():
            if review["external_id"] == ref.get("external_id"):
                return dict(review)
        raise KeyError("local review not found")

    async def merge_review(self, command: dict[str, Any]) -> dict[str, Any]:
        key = str(command.get("idempotency_key") or "")
        review = self._reviews.get(key)
        if review is None:
            raise KeyError("local review not found")
        expected = str(command.get("expected_head_commit") or "")
        if expected != review["head_commit"]:
            return {"status": "CONFLICTED", "merge_commit": None, "message": "head changed after approval"}
        if review["checks_state"] != "PASS" or review["approvals"] < 1 or review["changes_requested"] or review["mergeable"] is not True:
            return {"status": "FAILED", "merge_commit": None, "message": "review gates are not satisfied"}
        integration = str(command.get("integration_branch") or "maf/integration")
        if integration in {"main", "master"}:
            raise ValueError("integration branch must not be protected main")
        target = self.workspace_root / f"merge-{self._safe(integration)}"
        if target.exists():
            shutil.rmtree(target)
        try:
            await self._git(self.repository_root, "worktree", "add", "-B", integration, str(target), str(command.get("base_branch") or "main"))
            await self._git(target, "merge", "--no-edit", review["head_commit"])
            merge_commit = await self._git(target, "rev-parse", "HEAD")
            review["state"] = "MERGED"
            return {"status": "MERGED", "merge_commit": merge_commit, "message": "merged in controlled worktree"}
        except RuntimeError as exc:
            return {"status": "CONFLICTED", "merge_commit": None, "message": str(exc)}
        finally:
            await self.cleanup(target)

    async def cleanup(self, worktree: Path | str) -> None:
        path = Path(worktree).resolve()
        try:
            path.relative_to(self.workspace_root)
        except ValueError:
            raise ValueError("worktree is outside review workspace")
        if path.exists():
            await self._git(self.repository_root, "worktree", "remove", "--force", str(path))


__all__ = ["LocalGitReviewAdapter"]
