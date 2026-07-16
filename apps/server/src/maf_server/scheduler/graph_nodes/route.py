"""用受限条件表达式选择后续边的图节点接口。"""

from maf_contracts.run import CompactEdge
from maf_server.scheduler.state import RunState


def choose_next_edge(state: RunState, outgoing_edges: list[CompactEdge], facts: dict) -> CompactEdge:
    """按 priority/key 稳定排序并返回第一个条件为真的边。

    facts 只能含设计允许的状态、Gate 决策、错误类别和计数；表达式解释器不得执行 Python、
    调用函数或访问环境。零匹配且无默认边是流程错误，多条同优先级匹配是配置错误。
    """
    ...
