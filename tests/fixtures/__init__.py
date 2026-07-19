"""测试 Fixture 包：可复用的测试夹具与数据工厂。

子模块约定：
- ``virtual_clock``：虚拟时钟，实现 ``maf_server.core.clock.Clock`` Protocol；
- ``temp_dir``：显式清理的临时目录；
- ``git_repo``：本地 Git 仓库（真实 git，不依赖 GitHub）；
- ``mock_providers``：假 Model/Tool Adapter（不依赖真实 API Key）；
- ``factories``：固定 ID 与 ServerSettings/NodeSettings 工厂；
- ``sensitive_config``：脱敏配置（不含真实凭据）。

禁止提交真实密钥或用户仓库内容。
"""
