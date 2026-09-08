"""Install the BugLens backend into a runtime directory managed by the launcher."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent


def install(runtime_root: Path) -> Path:
    """Synchronize the production backend environment and return its venv path."""
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv is required. Install it from https://docs.astral.sh/uv/.")

    runtime_root = runtime_root.expanduser().resolve()
    venv_root = runtime_root / "venv"
    runtime_root.mkdir(parents=True, exist_ok=True)

    environment = os.environ.copy()
    environment["UV_PROJECT_ENVIRONMENT"] = str(venv_root)
    command = [
        uv,
        "sync",
        "--project",
        str(BACKEND_ROOT),
        "--locked",
        "--no-dev",
        "--extra",
        "web",
    ]
    print("Installing backend dependencies...")
    subprocess.run(command, cwd=BACKEND_ROOT, env=environment, check=True)
    return venv_root


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=BACKEND_ROOT / ".runtime",
        help="Directory that receives the isolated backend environment.",
    )
    args = parser.parse_args()
    install(args.runtime_root)


if __name__ == "__main__":
    main()
