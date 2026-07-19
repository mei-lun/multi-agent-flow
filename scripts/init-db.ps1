<#
.SYNOPSIS
  初始化或升级 maf.db 业务数据库。

.DESCRIPTION
  调用 MigrationRunner（migrations/runner.py）应用 migrations/ 目录下所有
  待应用 SQL 迁移。幂等：重复执行跳过已应用迁移；已应用迁移被修改时报校验和
  冲突并以非零退出码终止。

  本脚本只负责调用迁移器，不直接执行 SQL。checkpoints.db 由 LangGraph
  SQLite Adapter 自行管理，不在此处初始化。

  PRAGMA 基线（WAL、foreign_keys、busy_timeout、synchronous、temp_store）
  由 MigrationRunner 在每个连接上应用，与 infra/sqlite/pragmas.sql 一致。

.PARAMETER DatabasePath
  maf.db 路径。默认从 MAF_DATA_DIR 与 MAF_DATABASE_PATH 环境变量推导；
  若未设置则使用 ./data/maf.db。绝对路径原样使用，相对路径相对于当前工作目录。

.PARAMETER MigrationsDir
  迁移脚本目录，默认 ./migrations。

.EXAMPLE
  scripts/init-db.ps1
  scripts/init-db.ps1 -DatabasePath ./data/maf.db
  scripts/init-db.ps1 -DatabasePath D:\data\maf.db -MigrationsDir D:\migrations

.NOTES
  适用平台：Windows PowerShell 5.1+。
  任何一步失败即以非零退出码终止。
#>

[CmdletBinding()]
param(
  [string]$DatabasePath,
  [string]$MigrationsDir
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path "$PSScriptRoot/..").Path
Set-Location $root

# --------------------------------------------------------------------------- #
# 解析路径
# --------------------------------------------------------------------------- #
if (-not $MigrationsDir) {
  $MigrationsDir = "$root/migrations"
}

if (-not $DatabasePath) {
  $dataDir = $env:MAF_DATA_DIR
  if (-not $dataDir) {
    $dataDir = "$root/data"
  }

  $dbName = $env:MAF_DATABASE_PATH
  if (-not $dbName) {
    $dbName = "maf.db"
  }

  # 绝对路径原样使用；相对路径拼接 dataDir（与 ServerSettings 的 confinement 行为一致）
  if ([System.IO.Path]::IsPathRooted($dbName)) {
    $DatabasePath = $dbName
  } else {
    $DatabasePath = Join-Path $dataDir $dbName
  }
}

# 确保数据库父目录存在
$dbDir = Split-Path $DatabasePath -Parent
if ($dbDir -and -not (Test-Path $dbDir)) {
  Write-Host "[init-db] Creating database directory: $dbDir" -ForegroundColor Cyan
  New-Item -ItemType Directory -Path $dbDir -Force | Out-Null
}

Write-Host "[init-db] Database:    $DatabasePath" -ForegroundColor Cyan
Write-Host "[init-db] Migrations:  $MigrationsDir" -ForegroundColor Cyan

# --------------------------------------------------------------------------- #
# 调用迁移器
# --------------------------------------------------------------------------- #
# python -m migrations.runner 将当前工作目录（项目根）加入 sys.path，
# 因此 migrations 包可被正常导入。
python -m migrations.runner `
  --db-path $DatabasePath `
  --migrations-dir $MigrationsDir

if ($LASTEXITCODE -ne 0) {
  throw "Database migration failed (exit $LASTEXITCODE). See stderr above."
}

Write-Host "[init-db] Database initialized successfully." -ForegroundColor Green
