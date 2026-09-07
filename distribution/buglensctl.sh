#!/usr/bin/env bash
set -euo pipefail

command_name="${1:-help}"
mode="full"
if [[ "${2:-}" == "--mode" ]]; then mode="${3:-full}"; fi
if [[ "$mode" != "backend" && "$mode" != "full" ]]; then
  echo "mode must be backend or full" >&2
  exit 2
fi

distribution_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
runtime_root="$distribution_root/.runtime"
env_file="$runtime_root/.env"
mode_file="$runtime_root/mode"
mkdir -p "$runtime_root"
if [[ ! -f "$env_file" ]]; then
  cp "$distribution_root/.env.example" "$env_file"
  echo "Created $env_file. Set OPENAI_API_KEY, then run this command again."
  if [[ "$command_name" != "help" && "$command_name" != "init" ]]; then exit 0; fi
fi
if [[ ("$command_name" == "update" || "$command_name" == "doctor") && "${2:-}" != "--mode" && -f "$mode_file" ]]; then
  saved_mode="$(tr -d '[:space:]' < "$mode_file")"
  [[ "$saved_mode" == "backend" || "$saved_mode" == "full" ]] && mode="$saved_mode"
fi

assert_docker() {
  command -v docker >/dev/null 2>&1 || { echo "Docker Desktop with Compose v2 is required." >&2; exit 1; }
  docker compose version >/dev/null
}

compose_file="$distribution_root/compose/$mode.yaml"
compose() {
  docker compose --project-directory "$runtime_root" --env-file "$env_file" -f "$compose_file" "$@"
}

case "$command_name" in
  install)
    assert_docker
    compose up -d --remove-orphans --wait
    printf '%s' "$mode" > "$mode_file"
    if [[ "$mode" == "full" ]]; then echo "BugLens is ready at http://localhost:8080"; else echo "BugLens API is ready at http://localhost:8000"; fi
    ;;
  update)
    assert_docker
    backup_root="$runtime_root/backups/$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$backup_root"
    [[ -d "$runtime_root/data" ]] && cp -a "$runtime_root/data" "$backup_root/data"
    cp "$env_file" "$backup_root/.env"
    compose pull
    compose up -d --remove-orphans --wait
    echo "BugLens updated. Backup: $backup_root"
    ;;
  doctor)
    assert_docker
    compose ps
    health_url="http://localhost:8000/v1/admin/health"
    [[ "$mode" == "full" ]] && health_url="http://localhost:8080/v1/admin/health"
    if curl --fail --silent "$health_url" >/dev/null; then echo "Backend health: OK"; else echo "Backend health endpoint is not reachable"; fi
    ;;
  init)
    echo "Edit $env_file and set OPENAI_API_KEY, then run install --mode backend or full."
    ;;
  *)
    echo "Usage: bash ./buglensctl.sh install --mode backend|full"
    echo "       bash ./buglensctl.sh update"
    echo "       bash ./buglensctl.sh doctor"
    ;;
esac
