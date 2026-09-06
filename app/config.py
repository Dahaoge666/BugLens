"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    state_dir: Path = Path("data/runs")
    prompt_config: Path | None = None
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"

    @classmethod
    def from_env(cls) -> Settings:
        prompt_config = os.getenv("BUGLENS_PROMPT_CONFIG")
        try:
            port = int(os.getenv("BUGLENS_PORT", "8000"))
        except ValueError as exc:
            raise ValueError("BUGLENS_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("BUGLENS_PORT must be between 1 and 65535")
        log_level = os.getenv("BUGLENS_LOG_LEVEL", "info").lower()
        valid_levels = {"critical", "error", "warning", "info", "debug", "trace"}
        if log_level not in valid_levels:
            raise ValueError("BUGLENS_LOG_LEVEL is invalid")
        return cls(
            state_dir=Path(os.getenv("BUGLENS_STATE_DIR", "data/runs")),
            prompt_config=Path(prompt_config) if prompt_config else None,
            host=os.getenv("BUGLENS_HOST", "127.0.0.1"),
            port=port,
            log_level=log_level,
        )
