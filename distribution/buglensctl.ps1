[CmdletBinding()]
param(
  [Parameter(Position = 0)]
  [ValidateSet("install", "start", "stop", "restart", "update", "status", "doctor", "uninstall", "init", "help")]
  [string]$Command = "help",
  [Parameter(Position = 1)]
  [ValidateSet("backend", "full")]
  [string]$Mode = "full"
)

$ErrorActionPreference = "Stop"
$controller = Join-Path $PSScriptRoot "buglensctl.py"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw "uv is required. Install it from https://docs.astral.sh/uv/."
}

$arguments = @("run", $controller, $Command)
if ($PSBoundParameters.ContainsKey("Mode") -or $Command -eq "install") {
  $arguments += @("--mode", $Mode)
}
& uv @arguments

exit $LASTEXITCODE
