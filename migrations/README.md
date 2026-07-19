# 数据库迁移

保存按序号不可变的业务库迁移，例如 `0001_initial.sql`。LangGraph checkpoint 库由其适配器管理，不混入业务迁移。

## 命名规范

迁移文件名格式为 `NNNN_description.sql`：

- `NNNN`：4 位零填充序号，从 `0001` 开始，必须连续（禁止跳号）；
- `description`：小写 `snake_case`，只能包含 `[a-z0-9_]`；
- 后缀固定为 `.sql`。

示例：`0001_initial.sql`、`0012_add_role_versions.py`（本目录仅承载 `.sql`）。

## 不可变原则

**已发布的迁移不得修改**。`MigrationRunner` 会对每个已应用迁移计算 SHA-256
校验和并与 `schema_migrations.checksum` 比对，任何字节变化（含空格、行尾、BOM）
都会触发 `MigrationError`。如需变更 schema，请新增迁移，不要编辑旧迁移。

## schema_migrations 表

由 `MigrationRunner` 在应用任何迁移前自动创建（`CREATE TABLE IF NOT EXISTS`），
本身不作为编号迁移管理，结构与 `migrations/runner.py` 中 `_SCHEMA_MIGRATIONS_DDL` 一致：

| 字段         | 类型 | 说明                                       |
|--------------|------|--------------------------------------------|
| `version`    | TEXT | PRIMARY KEY，4 位序号，如 `0001`           |
| `description`| TEXT | 文件名中下划线后的描述，如 `initial`       |
| `checksum`   | TEXT | SHA-256 hex（基于文件原始字节）            |
| `applied_at` | TEXT | 应用时间，UTC RFC3339（`YYYY-MM-DDTHH:MM:SSZ`） |
| `filename`   | TEXT | 完整文件名，如 `0001_initial.sql`          |

## 运行方式

### 命令行

```powershell
# 默认路径：data/maf.db + migrations/
python -m migrations.runner

# 指定路径
python -m migrations.runner --db-path ./data/maf.db --migrations-dir ./migrations
```

### PowerShell 脚本

```powershell
scripts/init-db.ps1
scripts/init-db.ps1 -DatabasePath ./data/maf.db
```

`scripts/init-db.ps1` 会从 `MAF_DATA_DIR` 与 `MAF_DATABASE_PATH` 环境变量推导
默认数据库路径，等价于 `apps/server/.../config.py` 的解析规则。

## 校验和算法

- 算法：SHA-256，输出 64 位小写 hex；
- 输入：迁移文件原始字节（`Path.read_bytes()`），不经过任何编码/换行归一化；
- 因此跨平台行尾差异（CRLF/LF）会改变校验和——提交时请统一使用 LF。

## 事务与回滚

- 每个迁移在独立 `BEGIN IMMEDIATE` 事务中执行，迁移 SQL 与 `schema_migrations`
  记录写入同一事务，失败时整体回滚；
- 迁移脚本**禁止**包含 `BEGIN` / `COMMIT` / `ROLLBACK` 等事务控制语句，
  事务由 `MigrationRunner` 统一管理；
- 任何迁移失败立即终止，不继续后续迁移。

## 范围边界

- 本目录仅承载 `maf.db` 业务库迁移；
- `checkpoints.db` 由 LangGraph SQLite Adapter 自行管理；
- SQLite 是 Git control 分支的可重建投影，迁移器用于建立 schema。
