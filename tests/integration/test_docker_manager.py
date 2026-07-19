from types import SimpleNamespace

import pytest

from maf_runner.docker.cleanup import cleanup_job_resources
from maf_runner.docker.manager import LocalDockerManager
from maf_runner.security.boundaries import BoundaryViolation


class _Container:
    def __init__(self):
        self.id = "container-1"
        self.labels = {"maf.node_id": "node-a", "maf.job_id": "job-1"}
        self.started = self.stopped = self.removed = False
    def start(self): self.started = True
    def stop(self, timeout=0): self.stopped = True
    def remove(self, **kwargs): self.removed = True
    def logs(self, **kwargs): return [b"one", b"two"]


class _Containers:
    def __init__(self):
        self.item = _Container()
        self.create_kwargs = None
    def create(self, **kwargs):
        self.create_kwargs = kwargs
        return self.item
    def get(self, _id): return self.item
    def list(self, **kwargs): return [self.item]


@pytest.mark.asyncio
async def test_container_lifecycle_uses_restricted_profile_and_owner_labels(tmp_path):
    api = _Containers()
    manager = LocalDockerManager(SimpleNamespace(containers=api), node_id="node-a", assignment_validator=lambda _: True)
    handle = await manager.create("job-1", "repo/image@sha256:" + "a" * 64, {}, str(tmp_path), {"network_mode": "none"})
    assert api.create_kwargs["privileged"] is False
    assert api.create_kwargs["read_only"] is True
    assert api.create_kwargs["cap_drop"] == ["ALL"]
    await manager.start(handle)
    assert api.item.started
    assert [chunk async for chunk in manager.logs(handle)] == [b"one", b"two"]
    await manager.stop(handle, 1)
    await manager.remove(handle)
    await manager.remove(handle)
    assert api.item.removed


@pytest.mark.asyncio
async def test_container_rejects_dangerous_profile_and_cleanup_filters_owner(tmp_path):
    api = _Containers()
    manager = LocalDockerManager(SimpleNamespace(containers=api), node_id="node-a")
    with pytest.raises(BoundaryViolation):
        await manager.create("job-1", "repo/image@sha256:" + "a" * 64, {"privileged": True}, str(tmp_path), {"network_mode": "none"})
    result = await cleanup_job_resources("job-1", docker_client=SimpleNamespace(containers=api), node_id="node-a")
    assert result["containers"] == 1
