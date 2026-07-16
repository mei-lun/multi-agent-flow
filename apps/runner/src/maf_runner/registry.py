"""通过节点清单和 Git 事件声明能力、容量与状态的接口。"""

from typing import Protocol
from maf_contracts.coordination import CoordinationEvent, NodeManifest


class RunnerRegistry(Protocol):
    def build_manifest(self) -> NodeManifest:
        """从本地可信配置和实际探测构造清单；不能由远程任务修改能力或容量。"""
        ...
    def build_registration_event(self, manifest: NodeManifest, control_commit: str) -> CoordinationEvent:
        """创建 NODE_REGISTERED/NODE_UPDATED 事件；node_id 与 Git 签名身份必须稳定。"""
        ...
