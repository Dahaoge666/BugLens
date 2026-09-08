[CmdletBinding()]
param(
  [ValidateSet("full", "backend")]
  [string]$Mode = "full"
)

$ErrorActionPreference = "Stop"
$launcher = Join-Path $PSScriptRoot "distribution\buglensctl.ps1"
if (-not (Test-Path -LiteralPath $launcher)) {
  throw "BugLens launcher was not found: $launcher"
}

Write-Host "BugLens: installing and starting $Mode mode..."
& $launcher install -Mode $Mode
exit $LASTEXITCODE
