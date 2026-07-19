"""Restricted container lifecycle for one Runner node."""

import inspect
from pathlib import Path
from typing import AsyncIterator, Protocol

from maf_runner.security.boundaries import BoundaryViolation


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


class LocalDockerManager:
    """Docker SDK adapter with fail-closed task ownership checks."""

    def __init__(self, client: object, *, node_id: str, assignment_validator=None) -> None:
        self._client = client
        self._node_id = node_id
        self._assignment_validator = assignment_validator
        self._handles: dict[str, ContainerHandle] = {}

    async def _await(self, value):
        return await value if inspect.isawaitable(value) else value

    async def create(
        self,
        job_id: str,
        image_digest: str,
        profile: dict,
        workspace_path: str,
        network: dict,
    ) -> ContainerHandle:
        if "@sha256:" not in image_digest or image_digest.endswith("@sha256:"):
            raise BoundaryViolation("container image must be digest pinned")
        if profile.get("privileged") or profile.get("pid_mode") == "host" or profile.get("ipc_mode") == "host":
            raise BoundaryViolation("privileged or host namespace container is forbidden")
        if profile.get("devices") or profile.get("cap_add"):
            raise BoundaryViolation("devices and added capabilities are forbidden")
        mounts = profile.get("mounts", [])
        if mounts:
            raise BoundaryViolation("arbitrary profile mounts are forbidden")
        workspace = Path(workspace_path).resolve()
        if not workspace.is_dir():
            raise BoundaryViolation("workspace path must be an existing directory")
        if "/var/run/docker.sock" in str(workspace):
            raise BoundaryViolation("Docker socket may not be mounted")
        network_mode = str(network.get("network_mode", "none"))
        if network_mode in {"host", "bridge", "default"}:
            raise BoundaryViolation("uncontrolled Docker network mode is forbidden")
        labels = {"maf.node_id": self._node_id, "maf.job_id": job_id}
        kwargs = {
            "image": image_digest,
            "command": profile.get("command"),
            "detach": True,
            "labels": labels,
            "network_mode": network_mode,
            "privileged": False,
            "read_only": True,
            "cap_drop": ["ALL"],
            "pids_limit": int(profile.get("pids_limit", 128)),
            "mem_limit": profile.get("memory", "512m"),
            "nano_cpus": int(float(profile.get("cpus", 1)) * 1_000_000_000),
            "volumes": {str(workspace): {"bind": "/workspace", "mode": "rw"}},
            "working_dir": "/workspace",
        }
        create = getattr(getattr(self._client, "containers", self._client), "create")
        container = await self._await(create(**kwargs))
        container_id = str(getattr(container, "id", ""))
        if not container_id:
            raise RuntimeError("Docker client returned a container without id")
        handle = ContainerHandle(
            container_id=container_id,
            job_id=job_id,
            node_id=self._node_id,
            labels=labels,
            created=True,
        )
        self._handles[container_id] = handle
        return handle

    async def _container(self, handle: ContainerHandle):
        container_id = str(handle.get("container_id", ""))
        owned = self._handles.get(container_id)
        if owned is None or owned.get("node_id") != self._node_id:
            raise BoundaryViolation("container is not owned by this Runner")
        getter = getattr(getattr(self._client, "containers", self._client), "get")
        return await self._await(getter(container_id))

    async def start(self, handle: ContainerHandle) -> None:
        if self._assignment_validator is not None:
            valid = await self._await(self._assignment_validator(dict(handle)))
            if valid is not True:
                raise BoundaryViolation("assignment changed before container start")
        container = await self._container(handle)
        await self._await(container.start())
        handle["started"] = True

    async def logs(self, handle: ContainerHandle) -> AsyncIterator[bytes]:
        container = await self._container(handle)
        stream = await self._await(container.logs(stream=True, follow=True))
        total = 0
        for chunk in stream:
            data = bytes(chunk)
            total += len(data)
            if total > 4 * 1024 * 1024:
                break
            yield data

    async def stop(self, handle: ContainerHandle, grace_seconds: int) -> None:
        container = await self._container(handle)
        try:
            await self._await(container.stop(timeout=max(0, grace_seconds)))
        except Exception:
            kill = getattr(container, "kill", None)
            if kill is not None:
                await self._await(kill())
        handle["stopped"] = True

    async def remove(self, handle: ContainerHandle) -> None:
        container_id = str(handle.get("container_id", ""))
        if container_id not in self._handles:
            return
        container = await self._container(handle)
        await self._await(container.remove(force=True, v=True))
        self._handles.pop(container_id, None)


__all__ = ["ContainerHandle", "DockerManager", "LocalDockerManager"]
