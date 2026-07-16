"""Workflow 编辑、校验和发布接口。"""

from typing import Protocol
from maf_contracts.common import ActorContext
from .schemas import *


class WorkflowValidator(Protocol):
    def validate(self, graph: WorkflowGraph) -> ValidationReport:
        """执行无副作用静态检查。

        至少检查唯一 start、节点/边 key 唯一、边端点存在、所有节点可达、存在成功结束路径、
        无禁止环、Agent 节点绑定已发布 Role Version、输入输出 Contract 能衔接、条件表达式
        只使用白名单字段和运算符、重试与返工有上限。一次返回全部问题。
        """
        ...


class WorkflowService(Protocol):
    async def create_workflow(self, actor: ActorContext, request: CreateWorkflowRequest) -> WorkflowView:
        """创建稳定 Workflow Definition；尚未包含可执行 Graph。"""
        ...
    async def create_version(self, actor: ActorContext, workflow_id: str, request: CreateWorkflowVersionRequest) -> WorkflowVersionView:
        """创建 DRAFT；可从已有版本复制，但新旧版本随后完全独立。"""
        ...
    async def save_graph(self, actor: ActorContext, version_id: str, request: SaveGraphRequest) -> WorkflowVersionView:
        """按 expected_version 保存完整 Graph 草稿。

        先做结构解析，再计算规范化 hash；允许保存有校验错误的 DRAFT，但状态标记 FAIL。
        PUBLISHED 版本拒绝修改。
        """
        ...
    async def validate_version(self, actor: ActorContext, version_id: str) -> ValidationReport:
        """读取已保存 Graph，运行 Validator 并持久化报告，不改变发布状态。"""
        ...
    async def publish(self, actor: ActorContext, version_id: str, request: PublishWorkflowRequest) -> WorkflowVersionView:
        """重新校验并原子发布 Workflow Version。

        仅 valid=true 时发布；固定所有 Role/Schema/Policy 精确版本与 content_hash；已发布内容
        不可修改。产生配置发布事件。
        """
        ...
    async def diff(self, actor: ActorContext, version_id: str, other_version_id: str) -> WorkflowDiff:
        """按稳定 node/edge key 比较两个可见版本，不调用模型做语义猜测。"""
        ...

