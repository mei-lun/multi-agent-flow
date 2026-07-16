# Scripts

计划固定以下 PowerShell 脚本名；脚本在对应能力实现时加入，避免当前提供“看似可运行但没有实现”的命令：

- `bootstrap.ps1`：检查 Python、Node、Docker 与 Git，创建本地开发环境。
- `dev.ps1`：并行启动 Web、Server 和本地 Runner。
- `init-db.ps1`：创建或升级业务库和 checkpoint 库。
- `seed-demo.ps1`：导入网站交付模板与演示角色，不写入模型密钥。
- `backup.ps1`：一致性备份 SQLite、ArtifactStore 和配置清单。
- `restore.ps1`：校验备份后恢复到空数据目录。
- `verify.ps1`：统一运行格式、静态检查和各级测试。

脚本只负责开发和维护操作，不承载业务规则。

计划文件：

- `dev.ps1` / `dev.sh`：启动 web、server、runner。
- `init_db.py`：初始化和迁移 SQLite。
- `backup.py`：备份 SQLite、checkpoint 和 Artifact。
- `restore.py`：恢复并校验备份。
- `seed_template.py`：导入网站开发模板。
