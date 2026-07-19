param([string]$DataDir = "data")
$ErrorActionPreference = "Stop"
$template = Join-Path $PSScriptRoot "../templates/website_delivery"
$target = Join-Path $DataDir "templates/website_delivery"
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -Recurse -Force (Join-Path $template "*") $target
Write-Output "website_delivery template seeded at $target"
