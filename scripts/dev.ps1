param([switch]$Build)
$ErrorActionPreference = "Stop"
$compose = Join-Path $PSScriptRoot "../infra/compose/docker-compose.yml"
$args = @("compose", "-f", $compose, "up")
if ($Build) { $args += "--build" }
& docker @args
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
