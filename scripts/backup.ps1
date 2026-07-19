param(
  [string]$DataDir = "data",
  [string]$Output = "backups/maf-$(Get-Date -Format yyyyMMdd-HHmmss).zip"
)
$ErrorActionPreference = "Stop"
$resolvedData = (Resolve-Path -LiteralPath $DataDir).Path
$parent = Split-Path -Parent $Output
if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
$staging = Join-Path ([System.IO.Path]::GetTempPath()) ("maf-backup-" + [guid]::NewGuid())
New-Item -ItemType Directory -Force -Path $staging | Out-Null
try {
  Copy-Item -Recurse -Force $resolvedData (Join-Path $staging "data")
  if (Test-Path "templates") { Copy-Item -Recurse -Force "templates" (Join-Path $staging "templates") }
  $manifest = @{ schema_version = 1; created_at = (Get-Date).ToUniversalTime().ToString("o"); data_dir = $DataDir } | ConvertTo-Json
  Set-Content -Encoding UTF8 -Path (Join-Path $staging "backup-manifest.json") -Value $manifest
  Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $Output -Force
  Write-Output $Output
} finally { Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue }
