#!/usr/bin/env bash
set -euo pipefail

backend_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
runtime_root="${1:-$backend_root/.runtime}"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it from https://docs.astral.sh/uv/." >&2
  exit 1
fi

exec uv run --no-project "$backend_root/install.py" --runtime-root "$runtime_root"
