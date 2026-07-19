"""Idempotent cleanup helpers for node-owned Docker resources."""

import inspect

async def cleanup_job_resources(job_id: str, *, docker_client: object | None = None, node_id: str = "") -> dict[str, int]:
    """只按 Runner 自己的受控 label/root 查找资源；返回删除计数，单项失败继续并记录。"""
    counts = {"containers": 0, "volumes": 0, "errors": 0}
    if docker_client is None:
        return counts

    async def await_value(value):
        return await value if inspect.isawaitable(value) else value

    labels = [f"maf.job_id={job_id}"]
    if node_id:
        labels.append(f"maf.node_id={node_id}")
    containers_api = getattr(docker_client, "containers", docker_client)
    containers = await await_value(containers_api.list(all=True, filters={"label": labels}))
    for container in containers:
        container_labels = getattr(container, "labels", {}) or getattr(container, "attrs", {}).get("Config", {}).get("Labels", {})
        if container_labels.get("maf.job_id") != job_id or (node_id and container_labels.get("maf.node_id") != node_id):
            continue
        try:
            await await_value(container.remove(force=True, v=True))
            counts["containers"] += 1
        except Exception:
            counts["errors"] += 1
    return counts


__all__ = ["cleanup_job_resources"]
