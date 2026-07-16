# Skill 接口与调用逻辑

```text
上传 → 临时文件 → 哈希核对 → SkillPackageScanner → 隔离/正式 ArtifactStore
                                                → SkillRepository → DRAFT
DRAFT → Runner 测试 → TESTED → 发布校验 → PUBLISHED
Node fetch → 已验证 Git commit → 精确版本授权 → 路径规范化 → 本地只读文件
```

Skill 是只读能力说明和资产，不等于 Tool 权限。发布版本及 hash 随 control/task 输入固定，节点从已验证 Git 工作树读取。包声明需要某 Tool，只表示依赖；Role Version 仍必须显式绑定该 Tool。
