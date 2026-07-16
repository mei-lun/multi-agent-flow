"""模型配置与用量持久化接口。"""

from typing import Protocol
from .schemas import *


class ModelConfigurationRepository(Protocol):
    async def save_connection(self, item: ModelConnectionView, expected_version: int | None = None) -> ModelConnectionView:
        """创建/乐观锁保存脱敏连接；输入和表字段都不能含 api_key。"""
        ...
    async def get_connection(self, connection_id: str) -> ModelConnectionView | None:
        """返回连接配置与 secret_id 的内部关联视图；API 映射时继续脱敏。"""
        ...
    async def list_connections(self, query: ConnectionQuery) -> ConnectionPage:
        """按状态/adapter 分页；稳定排序为 created_at,id。"""
        ...
    async def save_profile(self, item: ModelProfileView, expected_version: int | None = None) -> ModelProfileView:
        """保存模型名称和经探测能力；并发探测结果使用 expected_version 防覆盖。"""
        ...
    async def get_profile(self, profile_id: str) -> ModelProfileView | None:
        """返回模型配置；不存在为 None。"""
        ...
    async def save_policy(self, item: ModelPolicyView) -> ModelPolicyView:
        """保存主模型及有序 fallback 的不可变策略版本。"""
        ...
    async def query_usage(self, query: UsageQuery, visible_project_ids: set[str]) -> UsagePage:
        """只聚合 visible_project_ids 内记录，金额使用十进制 SQL/应用计算。"""
        ...
