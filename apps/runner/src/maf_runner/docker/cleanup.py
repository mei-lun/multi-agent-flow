"""幂等清理容器、临时卷和过期工作区的接口。"""

async def cleanup_job_resources(job_id: str) -> dict[str, int]:
    """只按 Runner 自己的受控 label/root 查找资源；返回删除计数，单项失败继续并记录。"""
    ...

