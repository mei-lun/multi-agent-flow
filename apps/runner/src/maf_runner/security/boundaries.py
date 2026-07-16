"""拒绝超出 Job Grant 的路径、命令、挂载、URL 和输出。"""

from typing import Protocol


class BoundaryValidator(Protocol):
    def require_workspace_path(self, workspace_root: str, candidate: str) -> str:
        """解析链接和规范化路径，返回安全绝对路径；不在 root 内则抛边界错误。"""
        ...
    def require_allowed_command(self, executable: str, arguments: list[str], grant: dict) -> None:
        """按结构化 executable/arguments 校验，禁止 Shell 拼接和未授权子命令。"""
        ...
    def require_allowed_url(self, url: str, grant: dict) -> str:
        """验证 scheme/host/port/DNS 结果/重定向，拒绝 loopback、link-local 和私网绕过。"""
        ...
    def require_output_limits(self, path_count: int, total_bytes: int, grant: dict) -> None:
        """超过文件数或字节上限立即拒绝打包。"""
        ...

