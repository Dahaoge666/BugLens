[CmdletBinding()]
param(
  [string]$Output = (Join-Path $PSScriptRoot ".runtime\frontend"),
  [string]$Source = (Join-Path $PSScriptRoot "dist")
)

$ErrorActionPreference = "Stop"
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
  throw "Node.js 20 or newer is required. Install it, then run this script again."
}

& node (Join-Path $PSScriptRoot "install.mjs") --source $Source --output $Output
exit $LASTEXITCODE
