"""哈希、描述、预校验和上传 Job 输出 Artifact 的接口。"""

from typing import Protocol


class ArtifactPackager(Protocol):
    async def package_outputs(self, workspace_path: str, output_contract: dict, declared_paths: list[str]) -> dict:
        """只登记声明且位于 workspace 内的路径；阻止链接/逃逸，限制数量/大小并计算哈希。

        小型代码、文档和报告随任务分支提交；超过普通 Git 阈值的文件只有仓库已配置 Git LFS
        才允许。返回路径/hash/size manifest，Schema 失败的文件不得列为成功输出。
        """
        ...
