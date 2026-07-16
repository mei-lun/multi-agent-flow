"""复用前轻量源码和依赖安全扫描接口。"""

from typing import Protocol


class ExternalSourceScanner(Protocol):
    async def scan(self, source_artifact_version_id: str, ecosystem: str | None) -> dict:
        """检查可疑脚本、二进制、依赖漏洞和密钥痕迹；不执行候选项目安装脚本。"""
        ...
