"""白名单镜像、CPU、内存、进程和超时 Profile 接口。"""

from typing import Protocol


class DockerProfileRegistry(Protocol):
    def resolve(self, profile_key: str, image_digest: str) -> dict:
        """Profile 和 digest 都必须在本地配置；Job 只能进一步收紧，不能放宽。"""
        ...
