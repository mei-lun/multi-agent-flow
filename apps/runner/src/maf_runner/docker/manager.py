"""为单个 Git 分配任务创建、观察、停止和删除容器的接口。"""

from typing import AsyncIterator, Protocol


class ContainerHandle(dict):
    """包含 opaque container_id、job_id 和启动时间，不暴露 Docker Socket。"""


class DockerManager(Protocol):
    async def create(self, job_id: str, image_digest: str, profile: dict, workspace_path: str, network: dict) -> ContainerHandle:
        """只接受白名单 digest/profile；只挂载该 Job 工作区；禁 privileged、宿主 PID/IPC、
        Docker Socket 和任意设备；设置 CPU/内存/pids/read-only root/cap-drop。
        """
        ...
    async def start(self, handle: ContainerHandle) -> None:
        """启动前再次 fetch control，确认 assignment_id/epoch 仍有效。"""
        ...
    async def logs(self, handle: ContainerHandle) -> AsyncIterator[bytes]:
        """流式读取有大小上限的 stdout/stderr，交 ProgressReporter 脱敏。"""
        ...
    async def stop(self, handle: ContainerHandle, grace_seconds: int) -> None:
        """先协作停止，超时后强制终止；幂等。"""
        ...
    async def remove(self, handle: ContainerHandle) -> None:
        """只删除由本 Runner 以 job label 创建的容器和临时卷。"""
        ...
