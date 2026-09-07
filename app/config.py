"""Startup configuration and immutable per-run policy resolution."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ConfigDict, Field

from .models import StrictModel


class ModelConfig(StrictModel):
    """A named, provider-neutral model endpoint.

    ``model`` is the concrete model name sent to the provider; ``base_url`` /
    ``api_key`` are optional and fall back to the ``OPENAI_BASE_URL`` /
    ``OPENAI_API_KEY`` environment variables when unset, so a single profile can
    mix OpenAI-hosted and self-hosted gateways. ``streaming`` overrides the
    runner default per model (some gateways only work in streaming mode).
    """

    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=128)
    base_url: str | None = None
    api_key: str | None = None
    timeout: float = Field(default=60.0, gt=0, le=600)
    streaming: bool | None = None


class NodePolicy(StrictModel):
    model_config = ConfigDict(extra="forbid")
    model: str = "default"
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
    models: dict[str, ModelConfig] = Field(default_factory=dict)
    graph: GraphPolicy = Field(default_factory=GraphPolicy)
    evaluation: EvaluationPolicy = Field(default_factory=EvaluationPolicy)
    nodes: dict[str, NodePolicy] = Field(default_factory=dict)
    tools: ToolPolicy = Field(default_factory=ToolPolicy)

    def node(self, name: str) -> NodePolicy:
        return self.nodes.get(name, NodePolicy())

    def model(self, name: str) -> ModelConfig:
        """Resolve a node's model reference.

        If ``name`` is a key in the ``models`` registry, return that config.
        Otherwise treat ``name`` as a bare model name and build an ephemeral
        config that inherits provider credentials from the environment; this
        keeps older profiles (which inlined the model name) working.
        """
        if name in self.models:
            return self.models[name]
        return ModelConfig(model=name)


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
    "models": {"default": {"model": "gpt-4.1-mini"}},
    "graph": {"max_investigation_attempts": 2, "max_clarification_rounds": 2},
    "evaluation": {
        "passing_score": 75,
        "min_evidence_traceability": 15,
        "min_verification_executability": 15,
        "rubric_version": "rubric-v1",
    },
    "nodes": {
        "analyze": {
            "model": "default",
            "max_turns": 6,
            "prompt_version": "analyzer-v1",
        },
        "investigate": {
            "model": "default",
            "max_turns": 6,
            "prompt_version": "tenant-prompts-v1",
        },
        "evaluate": {"model": "default", "max_turns": 6, "prompt_version": "rubric-v1"},
        "summarize": {
            "model": "default",
            "max_turns": 6,
            "prompt_version": "summary-v1",
        },
    },
    "tools": {"enabled": False, "max_results": 20, "timeout_seconds": 30},
}


class ConfigRevisionConflictError(ValueError):
    code = "config_revision_conflict"


class ConfigNotWritableError(ValueError):
    code = "config_not_writable"


class ConfigRepository:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._write_lock = threading.Lock()
        self._profiles = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if self.path is None or not self.path.exists():
            return {"default": copy.deepcopy(DEFAULT_PROFILE)}
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
            result[name] = copy.deepcopy(value)
        if "default" not in result:
            result["default"] = copy.deepcopy(DEFAULT_PROFILE)
        versions = [
            str(value.get("config_version", value.get("version", f"{name}-v1")))
            for name, value in result.items()
        ]
        if len(versions) != len(set(versions)):
            raise ValueError("profile config_version values must be unique")
        return result

    @property
    def revision(self) -> str:
        """Return a stable revision for the currently loaded public config."""
        canonical = json.dumps(self._profiles, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:24]

    @property
    def writable(self) -> bool:
        return self.path is not None

    def profiles(self) -> dict[str, dict[str, Any]]:
        """Return a deep copy suitable for an admin read model."""
        return copy.deepcopy(self._profiles)

    def resolve(self, profile: str) -> ResolvedRunConfig:
        if profile not in self._profiles:
            raise ValueError(f"unknown profile: {profile}")
        return self._resolve_raw(profile, self._profiles[profile])

    def validate_profile(
        self, profile: str, value: dict[str, Any]
    ) -> ResolvedRunConfig:
        """Validate an untrusted profile without changing active configuration."""
        if not isinstance(value, dict):
            raise ValueError("profile configuration must be an object")
        return self._resolve_raw(profile, value)

    def apply_profile(
        self,
        profile: str,
        value: dict[str, Any],
        expected_revision: str,
    ) -> ResolvedRunConfig:
        """Validate and atomically persist one profile configuration."""
        with self._write_lock:
            return self._apply_profile(profile, value, expected_revision)

    def _apply_profile(
        self,
        profile: str,
        value: dict[str, Any],
        expected_revision: str,
    ) -> ResolvedRunConfig:
        if expected_revision != self.revision:
            raise ConfigRevisionConflictError("configuration revision changed")
        if self.path is None:
            raise ConfigNotWritableError(
                "configuration is using the built-in default and is not writable"
            )
        self.validate_profile(profile, value)
        profiles = self.profiles()
        profiles[profile] = copy.deepcopy(value)
        if "default" not in profiles:
            profiles["default"] = copy.deepcopy(DEFAULT_PROFILE)
        # Validate every profile before writing so one invalid profile cannot leave
        # the repository in a state that the next process cannot load.
        versions: list[str] = []
        for name, raw in profiles.items():
            resolved = self._resolve_raw(name, raw)
            versions.append(resolved.config_version)
        if len(versions) != len(set(versions)):
            raise ValueError("profile config_version values must be unique")
        payload = yaml.safe_dump(
            {"profiles": profiles}, sort_keys=False, allow_unicode=True
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.path)
        self._profiles = profiles
        return self.resolve(profile)

    @staticmethod
    def _resolve_raw(profile: str, raw_value: dict[str, Any]) -> ResolvedRunConfig:
        raw = copy.deepcopy(raw_value)
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
        # snapshot identity excludes api_key so rotating a key never invalidates
        # in-flight runs (the run keeps its own resolved ModelConfig in memory).
        policy_dump = policy.model_dump(mode="json")
        for model_cfg in policy_dump.get("models", {}).values():
            model_cfg.pop("api_key", None)
        canonical = json.dumps(
            {
                "profile": profile,
                "config_version": config_version,
                "prompt_config_version": prompt_version,
                "policy": policy_dump,
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
    cors_origin: str | None = None
    admin_token: str | None = None
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    openai_timeout: float = Field(default=60.0, gt=0, le=600)

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
            cors_origin=os.getenv("BUGLENS_CORS_ORIGIN"),
            admin_token=os.getenv("BUGLENS_ADMIN_TOKEN"),
            openai_base_url=os.getenv("OPENAI_BASE_URL"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_timeout=float(os.getenv("BUGLENS_OPENAI_TIMEOUT", "60")),
        )
