"""按 Job 应用离线、白名单或获批外网策略的接口。"""

from typing import Protocol


class NetworkPolicyApplier(Protocol):
    async def prepare(self, policy_ref: dict) -> dict:
        """从已拉取的不可变策略文件解析允许域名/IP/端口；默认无外网，防止 DNS 重绑定和私网访问。"""
        ...
    async def cleanup(self, network_handle: dict) -> None:
        """删除本 Job 网络规则，不影响其他容器。"""
        ...
