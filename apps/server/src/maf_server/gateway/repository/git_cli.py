"""Git 白名单子进程接口。"""

from typing import Protocol


class GitCli(Protocol):
    async def run(self, repository_path: str, arguments: list[str], timeout_seconds: int) -> tuple[int, str, str]:
        """执行参数数组而非 Shell 字符串。

        repository_path 必须通过受控根目录检查；arguments 首项在允许子命令集合；禁止从用户
        输入拼接 `-c`、hook、external diff、credential helper 等配置；输出限长并脱敏。
        """
        ...
