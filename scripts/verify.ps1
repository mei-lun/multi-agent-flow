<#
.SYNOPSIS
  Multi-Agent Flow 静态检查与测试入口。

.DESCRIPTION
  依次运行：
    1. pytest (tests/ 目录)；
    2. ruff check (apps/、packages/、tests/)；
    3. mypy 最小配置 (可选，使用 -SkipMypy 跳过)。

  任何一步失败即以非零退出码终止。脚本不修改代码，不写运行时数据。

.PARAMETER SkipMypy
  跳过 mypy 检查。

.PARAMETER PytestArgs
  透传给 pytest 的额外参数。

.NOTES
  适用平台：Windows PowerShell 5.1+。
#>

[CmdletBinding()]
param(
  [switch]$SkipMypy,
  [string]$PytestArgs
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path "$PSScriptRoot/..").Path
Set-Location $root

function Write-Step($message) {
  Write-Host "[verify] $message" -ForegroundColor Cyan
}

function Write-OK($message) {
  Write-Host "[verify] OK: $message" -ForegroundColor Green
}

function Invoke-Step($name, $command) {
  Write-Step "$name..."
  Write-Host "[verify] > $command" -ForegroundColor DarkGray
  Invoke-Expression $command
  if ($LASTEXITCODE -ne 0) {
    Write-Error "[verify] $name failed (exit $LASTEXITCODE)."
    exit $LASTEXITCODE
  }
  Write-OK "$name passed."
}

# --------------------------------------------------------------------------- #
# 1. pytest
# --------------------------------------------------------------------------- #
$pytestCommand = "python -m pytest tests/"
if ($PytestArgs) {
  $pytestCommand = "$pytestCommand $PytestArgs"
} else {
  $pytestCommand = "$pytestCommand -v"
}
Invoke-Step "pytest" $pytestCommand

# --------------------------------------------------------------------------- #
# 2. ruff check
# --------------------------------------------------------------------------- #
Invoke-Step "ruff check" "python -m ruff check apps/ packages/ tests/"

# --------------------------------------------------------------------------- #
# 3. mypy (可选)
# --------------------------------------------------------------------------- #
if (-not $SkipMypy) {
  $mypyAvailable = $false
  try {
    & python -m mypy --version 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
      $mypyAvailable = $true
    }
  } catch {
    $mypyAvailable = $false
  }

  if ($mypyAvailable) {
    Invoke-Step "mypy" "python -m mypy apps/ packages/ tests/"
  } else {
    Write-Host "[verify] mypy not installed; skipping (use -SkipMypy to silence)." -ForegroundColor Yellow
  }
} else {
  Write-Host "[verify] mypy skipped (-SkipMypy)." -ForegroundColor Yellow
}

Write-Host "[verify] All checks passed." -ForegroundColor Green
