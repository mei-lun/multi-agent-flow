<#
.SYNOPSIS
  Multi-Agent Flow 开发环境引导脚本。

.DESCRIPTION
  检查 Python、Node、pnpm、Docker 与 Git，安装 Python 开发依赖
  (``pip install -e ".[dev]"``) 和 Web 工作区依赖 (``pnpm install``)。

  脚本只负责建立本地开发环境，不承载业务规则，不创建数据库或运行时数据。

.PARAMETER SkipPython
  跳过 Python 依赖安装。

.PARAMETER SkipWeb
  跳过 Web 工作区依赖安装。

.NOTES
  适用平台：Windows PowerShell 5.1+。
  任何一步失败即以非零退出码终止。
#>

[CmdletBinding()]
param(
  [switch]$SkipPython,
  [switch]$SkipWeb
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path "$PSScriptRoot/..").Path
Set-Location $root

function Write-Step($message) {
  Write-Host "[bootstrap] $message" -ForegroundColor Cyan
}

function Write-OK($message) {
  Write-Host "[bootstrap] OK: $message" -ForegroundColor Green
}

function Write-Warn($message) {
  Write-Host "[bootstrap] WARN: $message" -ForegroundColor Yellow
}

function Test-CommandExists($name) {
  return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

# --------------------------------------------------------------------------- #
# 1. 检查必要工具
# --------------------------------------------------------------------------- #
Write-Step "Checking required tools..."

if (-not (Test-CommandExists "python")) {
  throw "python not found in PATH. Install Python 3.11+ and retry."
}
Write-OK "python found: $(python --version)"

if (-not (Test-CommandExists "git")) {
  throw "git not found in PATH. Install Git and retry."
}
Write-OK "git found: $(git --version)"

if (-not (Test-CommandExists "pnpm")) {
  if (Test-CommandExists "node") {
    Write-Warn "pnpm not found; attempting to enable via corepack..."
    corepack enable
    if (-not (Test-CommandExists "pnpm")) {
      throw "pnpm still not available. Run 'npm install -g pnpm' or 'corepack enable' and retry."
    }
  } else {
    throw "node/pnpm not found in PATH. Install Node.js 20+ and pnpm 9+ and retry."
  }
}
Write-OK "pnpm found: $(pnpm --version)"

# Docker 用于 Runner 容器隔离；当前阶段不强制，TASK-066+ 才需要。
if (Test-CommandExists "docker") {
  Write-OK "docker found: $(docker --version)"
} else {
  Write-Warn "docker not found in PATH; Runner execution unavailable until TASK-066+."
}

# --------------------------------------------------------------------------- #
# 2. 安装 Python 依赖
# --------------------------------------------------------------------------- #
if (-not $SkipPython) {
  Write-Step "Installing Python dependencies (pip install -e .[dev])..."
  python -m pip install --upgrade pip
  if ($LASTEXITCODE -ne 0) {
    throw "pip self-upgrade failed (exit $LASTEXITCODE)."
  }
  python -m pip install -e ".[dev]"
  if ($LASTEXITCODE -ne 0) {
    throw "Python dependency installation failed (exit $LASTEXITCODE)."
  }
  Write-OK "Python dependencies installed."
} else {
  Write-Warn "Skipping Python dependency installation (-SkipPython)."
}

# --------------------------------------------------------------------------- #
# 3. 安装 Web 工作区依赖
# --------------------------------------------------------------------------- #
if (-not $SkipWeb) {
  Write-Step "Installing Web workspace dependencies (pnpm install)..."
  pnpm install
  if ($LASTEXITCODE -ne 0) {
    throw "pnpm install failed (exit $LASTEXITCODE)."
  }
  Write-OK "Web workspace dependencies installed."
} else {
  Write-Warn "Skipping Web dependency installation (-SkipWeb)."
}

Write-OK "Bootstrap complete. Next: run 'scripts/verify.ps1' to validate the environment."
