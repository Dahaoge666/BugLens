#!/usr/bin/env bash
set -euo pipefail

frontend_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_dir="${1:-$frontend_root/dist}"
output_dir="${2:-$frontend_root/.runtime/frontend}"

if ! command -v node >/dev/null 2>&1; then
  echo "Node.js 20 or newer is required. Install it, then run this script again." >&2
  exit 1
fi

exec node "$frontend_root/install.mjs" --source "$source_dir" --output "$output_dir"
