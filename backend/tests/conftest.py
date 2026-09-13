"""Make the repository's independently packaged plugins importable in tests."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_root in (
    ROOT / "plugin-api",
    ROOT / "plugins" / "sqlite",
    ROOT / "plugins" / "file-logs",
    ROOT / "plugins" / "postgresql",
    ROOT / "plugins" / "ssh",
    ROOT / "plugins" / "ssh-logs",
):
    value = str(package_root)
    if value not in sys.path:
        sys.path.insert(0, value)
