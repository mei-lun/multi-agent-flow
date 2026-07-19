param(
  [Parameter(Mandatory=$true)][string]$Backup,
  [string]$DataDir = "data"
)
$ErrorActionPreference = "Stop"
$staging = Join-Path ([System.IO.Path]::GetTempPath()) ("maf-restore-" + [guid]::NewGuid())
New-Item -ItemType Directory -Force -Path $staging | Out-Null
try {
  Expand-Archive -LiteralPath $Backup -DestinationPath $staging -Force
  $manifest = Join-Path $staging "backup-manifest.json"
  if (-not (Test-Path $manifest)) { throw "backup-manifest.json is missing" }
  $data = Join-Path $staging "data"
  if (-not (Test-Path $data)) { throw "backup data directory is missing" }
  New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
  Copy-Item -Recurse -Force (Join-Path $data "*") $DataDir
} finally { Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue }
