"""文档型任务空工作区接口。"""

async def prepare_generic_workspace(job_id: str, input_bundle_ref: str, writable_subpaths: list[str]) -> str:
    """在受控 root 下创建唯一目录，校验并展开输入 Artifact，只赋予声明子路径写权限。"""
    ...
