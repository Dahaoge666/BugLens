#!/usr/bin/env bash
set -euo pipefail

mode="${1:-full}"
if [[ "$mode" != "full" && "$mode" != "backend" ]]; then
  echo "Usage: ./install.sh [full|backend]" >&2
  exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$root/distribution/buglensctl.sh" install --mode "$mode"
