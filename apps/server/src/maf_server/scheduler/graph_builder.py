"""把已发布 Workflow Version 编译为 LangGraph 图的接口。"""

from typing import Any


def compile_workflow(workflow_version: object, checkpointer: object) -> Any:
    """返回可执行但尚未运行的 compiled graph。

    输入必须是通过静态校验且不可变的 Workflow Version；为每种 node kind 映射固定节点
    函数，为 edge 使用受限条件求值器，绑定传入 checkpointer。编译不得读取项目当前配置、
    调用模型或写数据库。相同 content hash 应产生等价图；未知节点类型立即失败。
    """
    ...
