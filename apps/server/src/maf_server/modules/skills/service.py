"""Skill 注册、版本和受控读取接口。"""

from typing import BinaryIO, Protocol
from maf_contracts.common import ActorContext
from .schemas import *


class SkillPackageScanner(Protocol):
    def scan(self, archive: BinaryIO) -> ScanResult:
        """在写入正式 SkillStore 前扫描压缩包。

        必须限制压缩后/解压后大小、文件数、路径长度和压缩比；拒绝绝对路径、`..`、链接、
        设备文件和重复规范化路径；解析 manifest，列出脚本、外网与 Tool 声明。输出只给出
        可复现结果，不执行包内代码。
        """
        ...


class SkillService(Protocol):
    async def import_package(
        self, actor: ActorContext, request: ImportSkillRequest, archive: BinaryIO
    ) -> SkillVersionView:
        """导入新 Skill 或新包的首版本。

        先流式计算 SHA-256 并核对请求，再调用 Scanner；扫描通过后按内容寻址保存原包与
        规范化文件，创建 DRAFT 版本。失败包进入隔离区且不可被 Runtime 读取。
        """
        ...

    async def create_version(
        self, actor: ActorContext, skill_id: str, request: CreateSkillVersionRequest
    ) -> SkillVersionView:
        """从已上传 Artifact 创建递增的不可变 DRAFT 版本，并重新扫描全部内容。"""
        ...

    async def test_version(
        self, actor: ActorContext, version_id: str, request: TestSkillRequest
    ) -> SkillTestResult:
        """在隔离 Runner 中执行固定夹具测试。

        测试 Job 的 Tool、网络和预算权限不得超过 Skill manifest 声明；结果保存为报告
        Artifact。只有 PASS 才将版本状态改为 TESTED。
        """
        ...

    async def publish_version(
        self, actor: ActorContext, version_id: str, request: PublishSkillRequest
    ) -> SkillVersionView:
        """发布已扫描且测试通过的版本。

        检查 expected_version、扫描阻断项和测试结果；发布后内容不可修改，只能新建版本。
        产生 `configuration.version.published` 事件。
        """
        ...

