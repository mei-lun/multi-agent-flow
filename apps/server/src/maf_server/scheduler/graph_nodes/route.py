"""用受限条件表达式选择后续边的图节点接口。"""

from maf_contracts.run import CompactEdge
from maf_server.scheduler.state import RunState

import ast
import operator
from typing import Any


class _ConditionError(ValueError):
    pass


def _eval_condition(expression: str, facts: dict[str, Any]) -> bool:
    """Evaluate the deliberately tiny workflow condition language.

    Conditions are parsed as Python expressions but only boolean operators,
    comparisons, literals and names/subscript lookups are accepted.  No calls,
    attributes, comprehensions or arbitrary Python evaluation are possible.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise _ConditionError(str(exc)) from exc

    def value(node: ast.AST) -> Any:
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool, type(None))):
            return node.value
        if isinstance(node, ast.Name):
            return facts.get(node.id)
        if isinstance(node, ast.Subscript):
            base = value(node.value)
            key = value(node.slice)
            if not isinstance(base, dict):
                raise _ConditionError("subscript base must be a fact mapping")
            return base.get(key)
        if isinstance(node, ast.List):
            return [value(item) for item in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(value(item) for item in node.elts)
        raise _ConditionError(f"unsupported expression node: {type(node).__name__}")

    def boolean(node: ast.AST) -> bool:
        if isinstance(node, ast.BoolOp):
            vals = [boolean(item) for item in node.values]
            return all(vals) if isinstance(node.op, ast.And) else any(vals)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not boolean(node.operand)
        if isinstance(node, ast.Compare):
            left = value(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = value(comparator)
                fn = {
                    ast.Eq: operator.eq, ast.NotEq: operator.ne,
                    ast.Gt: operator.gt, ast.GtE: operator.ge,
                    ast.Lt: operator.lt, ast.LtE: operator.le,
                    ast.In: lambda left, right: operator.contains(right, left),
                    ast.NotIn: lambda left, right: not operator.contains(right, left),
                    ast.Is: operator.is_, ast.IsNot: operator.is_not,
                }.get(type(op))
                if fn is None or not fn(left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.Name) or isinstance(node, ast.Subscript):
            return bool(value(node))
        if isinstance(node, ast.Constant):
            return bool(value(node))
        raise _ConditionError(f"unsupported boolean node: {type(node).__name__}")

    return boolean(tree.body)


def choose_next_edge(state: RunState, outgoing_edges: list[CompactEdge], facts: dict) -> CompactEdge:
    """按 priority/key 稳定排序并返回第一个条件为真的边。

    facts 只能含设计允许的状态、Gate 决策、错误类别和计数；表达式解释器不得执行 Python、
    调用函数或访问环境。零匹配且无默认边是流程错误，多条同优先级匹配是配置错误。
    """
    if not isinstance(outgoing_edges, list):
        raise TypeError("outgoing_edges must be a list")
    # Stable order makes replay deterministic even when callers provide an
    # unsorted graph projection.
    ordered = sorted(outgoing_edges, key=lambda edge: (int(edge.get("priority", 0)), edge.get("key", "")))
    matches: list[CompactEdge] = []
    default: CompactEdge | None = None
    for edge in ordered:
        condition = edge.get("condition")
        if condition is None or not str(condition).strip():
            if default is not None:
                raise ValueError("multiple default edges configured")
            default = edge
            continue
        if _eval_condition(str(condition), facts):
            matches.append(edge)
    if len(matches) > 1 and matches[0].get("priority", 0) == matches[1].get("priority", 0):
        raise ValueError("multiple edges with the same priority matched")
    if matches:
        return matches[0]
    if default is not None:
        return default
    raise ValueError(f"no outgoing edge matched for node {getattr(state, 'current_node_ids', [])}")
