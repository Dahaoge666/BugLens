"""Startup configuration and immutable per-run policy resolution."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ConfigDict, Field

from .models import StrictModel


class NodePolicy(StrictModel):
    model_config = ConfigDict(extra="forbid")
    model: str = "gpt-4.1-mini"
    max_turns: int = Field(default=6, ge=1, le=20)
    prompt_version: str = Field(default="default-v1", min_length=1, max_length=128)


class GraphPolicy(StrictModel):
    model_config = ConfigDict(extra="forbid")
    max_investigation_attempts: int = Field(default=2, ge=1, le=2)
    max_clarification_rounds: int = Field(default=2, ge=0, le=10)


class EvaluationPolicy(StrictModel):
    model_config = ConfigDict(extra="forbid")
    passing_score: int = Field(default=75, ge=0, le=100)
    min_evidence_traceability: int = Field(default=15, ge=0, le=25)
    min_verification_executability: int = Field(default=15, ge=0, le=20)
    rubric_version: str = "rubric-v1"


class ToolPolicy(StrictModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    max_results: int = Field(default=20, ge=0, le=100)
    timeout_seconds: int = Field(default=30, ge=1, le=120)


class RuntimePolicy(StrictModel):
    """Only strategy values belong here; credentials are deliberately absent."""

    model_config = ConfigDict(extra="forbid")
    graph: GraphPolicy = Field(default_factory=GraphPolicy)
    evaluation: EvaluationPolicy = Field(default_factory=EvaluationPolicy)
    nodes: dict[str, NodePolicy] = Field(default_factory=dict)
    tools: ToolPolicy = Field(default_factory=ToolPolicy)

    def node(self, name: str) -> NodePolicy:
        return self.nodes.get(name, NodePolicy())


class ResolvedRunConfig(StrictModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_id: str
    profile: str
    config_version: str
    prompt_config_version: str
    policy: RuntimePolicy
    resolved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def as_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )


DEFAULT_PROFILE: dict[str, Any] = {
    "config_version": "default-v1",
    "prompt_config_version": "tenant-prompts-v1",
    "graph": {"max_investigation_attempts": 2, "max_clarification_rounds": 2},
    "evaluation": {
        "passing_score": 75,
        "min_evidence_traceability": 15,
        "min_verification_executability": 15,
        "rubric_version": "rubric-v1",
    },
    "nodes": {
        "analyze": {"max_turns": 6, "prompt_version": "analyzer-v1"},
        "investigate": {"max_turns": 6, "prompt_version": "tenant-prompts-v1"},
        "evaluate": {"max_turns": 6, "prompt_version": "rubric-v1"},
        "summarize": {"max_turns": 6, "prompt_version": "summary-v1"},
    },
    "tools": {"enabled": False, "max_results": 20, "timeout_seconds": 30},
}


class ConfigRepository:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._profiles = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if self.path is None or not self.path.exists():
            return {"default": DEFAULT_PROFILE}
        loaded = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("configuration root must be a mapping")
        profiles = loaded.get("profiles", loaded)
        if not isinstance(profiles, dict) or not profiles:
            raise ValueError("configuration must define at least one profile")
        result: dict[str, dict[str, Any]] = {}
        for name, value in profiles.items():
            if not isinstance(name, str) or not isinstance(value, dict):
                raise ValueError("each profile must be a mapping")
            result[name] = value
        if "default" not in result:
            result["default"] = DEFAULT_PROFILE
        versions = [
            str(value.get("config_version", value.get("version", f"{name}-v1")))
            for name, value in result.items()
        ]
        if len(versions) != len(set(versions)):
            raise ValueError("profile config_version values must be unique")
        return result

    def resolve(self, profile: str) -> ResolvedRunConfig:
        if profile not in self._profiles:
            raise ValueError(f"unknown profile: {profile}")
        raw = dict(self._profiles[profile])
        config_version = str(
            raw.pop("config_version", raw.pop("version", f"{profile}-v1"))
        )
        prompt_version = str(raw.pop("prompt_config_version", "tenant-prompts-v1"))
        nodes = raw.get("nodes")
        required_nodes = {"analyze", "investigate", "evaluate", "summarize"}
        if not isinstance(nodes, dict) or not required_nodes.issubset(nodes):
            missing = sorted(required_nodes - set(nodes or {}))
            raise ValueError(f"profile {profile} is missing node policies: {missing}")
        unknown_nodes = set(nodes) - required_nodes
        if unknown_nodes:
            raise ValueError(f"unknown node policies: {sorted(unknown_nodes)}")
        prompt_versions = [
            str(value.get("prompt_version", ""))
            for value in nodes.values()
            if isinstance(value, dict)
        ]
        if len(prompt_versions) != len(set(prompt_versions)):
            raise ValueError("node prompt_version values must be unique")
        policy = RuntimePolicy.model_validate(raw)
        canonical = json.dumps(
            {
                "profile": profile,
                "config_version": config_version,
                "prompt_config_version": prompt_version,
                "policy": policy.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        snapshot_id = "cfg_" + hashlib.sha256(canonical.encode()).hexdigest()[:24]
        return ResolvedRunConfig(
            snapshot_id=snapshot_id,
            profile=profile,
            config_version=config_version,
            prompt_config_version=prompt_version,
            policy=policy,
        )


class Settings(StrictModel):
    model_config = ConfigDict(extra="forbid")
    session_db: Path = Path("data/buglens.db")
    prompt_config: Path | None = None
    config_path: Path | None = None
    tracing_enabled: bool = True
    default_profile: str = "default"
    remote: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        tracing = os.getenv("BUGLENS_TRACING", "true").lower()
        if tracing not in {"true", "false"}:
            raise ValueError("BUGLENS_TRACING must be true or false")
        prompt_config = os.getenv("BUGLENS_PROMPT_CONFIG")
        config = os.getenv("BUGLENS_CONFIG")
        return cls(
            session_db=Path(os.getenv("BUGLENS_SESSION_DB", "data/buglens.db")),
            prompt_config=Path(prompt_config) if prompt_config else None,
            config_path=Path(config) if config else None,
            tracing_enabled=tracing == "true",
            default_profile=os.getenv("BUGLENS_PROFILE", "default"),
            remote=os.getenv("BUGLENS_REMOTE"),
        )
