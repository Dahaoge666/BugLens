[CmdletBinding()]
param(
  [Parameter(Position = 0)]
  [ValidateSet("install", "update", "doctor", "init", "help")]
  [string]$Command = "help",
  [Parameter(Position = 1)]
  [ValidateSet("backend", "full")]
  [string]$Mode = "full"
)

$ErrorActionPreference = "Stop"
$distributionRoot = (Resolve-Path (Join-Path $PSScriptRoot ".")).Path
$runtimeRoot = Join-Path $distributionRoot ".runtime"
$envFile = Join-Path $runtimeRoot ".env"
$modeFile = Join-Path $runtimeRoot "mode"

function Assert-Docker {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker Desktop with Compose v2 is required."
  }
  docker compose version | Out-Null
}

function Ensure-Runtime {
  New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
  if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath (Join-Path $distributionRoot ".env.example") -Destination $envFile
    Write-Host "Created $envFile. Set OPENAI_API_KEY, then run this command again."
    return $false
  }
  return $true
}

function Compose-File([string]$installMode) {
  if ($installMode -eq "backend") { return Join-Path $distributionRoot "compose\backend.yaml" }
  return Join-Path $distributionRoot "compose\full.yaml"
}

function Invoke-Compose([string[]]$Arguments, [string]$installMode) {
  $compose = Compose-File $installMode
  & docker compose --project-directory $runtimeRoot --env-file $envFile -f $compose @Arguments
  if ($LASTEXITCODE -ne 0) { throw "Docker Compose failed with exit code $LASTEXITCODE." }
}

$runtimeReady = Ensure-Runtime
if (-not $runtimeReady -and $Command -notin @("help", "init")) { exit 0 }
if ($Command -in @("update", "doctor") -and -not $PSBoundParameters.ContainsKey("Mode") -and (Test-Path -LiteralPath $modeFile)) {
  $savedMode = (Get-Content -LiteralPath $modeFile -Raw).Trim()
  if ($savedMode -in @("backend", "full")) { $Mode = $savedMode }
}

switch ($Command) {
  "install" {
    Assert-Docker
    Invoke-Compose @("up", "-d", "--remove-orphans", "--wait") $Mode
    Set-Content -LiteralPath $modeFile -Value $Mode -NoNewline
    Write-Host "BugLens $Mode installation is ready."
    if ($Mode -eq "full") { Write-Host "Open http://localhost:8080" } else { Write-Host "API: http://localhost:8000" }
  }
  "update" {
    Assert-Docker
    $backupRoot = Join-Path $runtimeRoot ("backups\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
    New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
    $dataRoot = Join-Path $runtimeRoot "data"
    if (Test-Path -LiteralPath $dataRoot) { Copy-Item -LiteralPath $dataRoot -Destination $backupRoot -Recurse }
    Copy-Item -LiteralPath $envFile -Destination (Join-Path $backupRoot ".env")
    Invoke-Compose @("pull") $Mode
    Invoke-Compose @("up", "-d", "--remove-orphans", "--wait") $Mode
    Write-Host "BugLens updated. Backup: $backupRoot"
  }
  "doctor" {
    Assert-Docker
    Invoke-Compose @("ps") $Mode
    try {
      $healthUrl = if ($Mode -eq "full") { "http://localhost:8080/v1/admin/health" } else { "http://localhost:8000/v1/admin/health" }
      $health = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl
      Write-Host "Backend health: $($health.StatusCode)"
    } catch {
      Write-Warning "Backend health endpoint is not reachable."
    }
  }
  "init" {
    Write-Host "Edit $envFile and set OPENAI_API_KEY, then run install --mode backend or full."
  }
  default {
    Write-Host "Usage: .\buglensctl.ps1 install --mode backend|full"
    Write-Host "       .\buglensctl.ps1 update"
    Write-Host "       .\buglensctl.ps1 doctor"
  }
}
