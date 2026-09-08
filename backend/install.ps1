[CmdletBinding()]
param(
  [string]$RuntimeRoot = (Join-Path $PSScriptRoot ".runtime")
)

$ErrorActionPreference = "Stop"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw "uv is required. Install it from https://docs.astral.sh/uv/."
}

& uv run --no-project (Join-Path $PSScriptRoot "install.py") --runtime-root $RuntimeRoot
exit $LASTEXITCODE
