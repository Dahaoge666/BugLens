"""CLI configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    session_db: Path = Path("data/buglens.db")
    prompt_config: Path | None = None
    tracing_enabled: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        prompt_config = os.getenv("BUGLENS_PROMPT_CONFIG")
        tracing = os.getenv("BUGLENS_TRACING", "true").lower()
        if tracing not in {"true", "false"}:
            raise ValueError("BUGLENS_TRACING must be true or false")
        return cls(
            session_db=Path(os.getenv("BUGLENS_SESSION_DB", "data/buglens.db")),
            prompt_config=Path(prompt_config) if prompt_config else None,
            tracing_enabled=tracing == "true",
        )
