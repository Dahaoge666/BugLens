from __future__ import annotations

import json
from pathlib import Path

from .models import DiagnosisState


class RunNotFoundError(FileNotFoundError):
    pass


class JsonStateStore:
    """One JSON document per graph run; writes are atomic within a local filesystem."""

    def __init__(self, root: Path = Path("data/runs")) -> None:
        self.root = root

    def _path(self, run_id: str) -> Path:
        if not run_id.replace("-", "").replace("_", "").isalnum():
            raise RunNotFoundError("Invalid run_id")
        return self.root / f"{run_id}.json"

    def save(self, state: DiagnosisState) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self._path(state.run_id)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(destination)

    def load(self, run_id: str) -> DiagnosisState:
        path = self._path(run_id)
        if not path.exists():
            raise RunNotFoundError(run_id)
        return DiagnosisState.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
