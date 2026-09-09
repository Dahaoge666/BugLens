"""Strict environment catalog, deterministic target resolution and snapshots.

The catalog is deliberately separate from the model/profile configuration.  A
run stores only a credential-free :class:`ResolvedEnvironmentSnapshot`; a
subsequent catalog edit therefore cannot silently change the data sources that
an existing run is allowed to inspect.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ConfigDict, Field, model_validator

from .models import (
    DiagnosisContext,
    EnvironmentTargetCandidate,
    ResolvedTarget,
    StrictModel,
    TargetSpec,
)


class EnvironmentConfigError(ValueError):
    code = "environment_config_invalid"


class EnvironmentRevisionConflictError(ValueError):
    code = "environment_config_revision_conflict"


class EnvironmentConfigNotWritableError(ValueError):
    code = "environment_config_not_writable"


class ToolLimits(StrictModel):
    model_config = ConfigDict(extra="forbid")
    max_results: int = Field(default=200, ge=1, le=1_000)
    timeout_seconds: int = Field(default=10, ge=1, le=30)
    max_bytes: int = Field(default=65_536, ge=1_024, le=1_048_576)
    max_scan_files: int = Field(default=100, ge=1, le=10_000)
    max_scan_bytes: int = Field(default=16 * 1024 * 1024, ge=1_024, le=1_073_741_824)


class PluginInstanceConfig(StrictModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    plugin_id: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    # Plugin-specific non-secret settings.  Credentials stay in separate
    # fields so the admin API can update them independently and redact them.
    config: dict[str, Any] = Field(default_factory=dict)
    username: str | None = Field(
        default=None,
        max_length=512,
        json_schema_extra={"writeOnly": True, "x-buglens-secret": True},
    )
    password: str | None = Field(
        default=None,
        max_length=4_096,
        json_schema_extra={"writeOnly": True, "x-buglens-secret": True},
    )
    token: str | None = Field(
        default=None,
        max_length=4_096,
        json_schema_extra={"writeOnly": True, "x-buglens-secret": True},
    )
    default_limits: ToolLimits = Field(default_factory=ToolLimits)


class EnvironmentConfig(StrictModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=256)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    level: str = Field(default="unknown", min_length=1, max_length=64)
    region: str | None = Field(default=None, max_length=128)
    timezone: str = Field(default="UTC", min_length=1, max_length=128)
    tags: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class ServiceConfig(StrictModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=256)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    version: str | None = Field(default=None, max_length=128)
    dependencies: list[str] = Field(default_factory=list, max_length=100)
    environment_id: str | None = Field(default=None, max_length=128)


class NodeConfig(StrictModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    hostname: str = Field(min_length=1, max_length=512)
    instance_id: str | None = Field(default=None, max_length=256)
    tags: dict[str, str] = Field(default_factory=dict)
    environment_id: str | None = Field(default=None, max_length=128)


class SourceConfig(StrictModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    kind: Literal["database", "logs"]
    plugin_instance_id: str = Field(min_length=1, max_length=128)
    environment_id: str | None = Field(default=None, max_length=128)
    service_ids: list[str] = Field(default_factory=list, max_length=100)
    node_ids: list[str] = Field(default_factory=list, max_length=100)
    config: dict[str, Any] = Field(default_factory=dict)
    limits: ToolLimits = Field(default_factory=ToolLimits)
    enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def accept_plugin_instance_alias(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "plugin_instance_id" not in data and "plugin_instance" in data:
            data["plugin_instance_id"] = data.pop("plugin_instance")
        return data


class EnvironmentDirectory(StrictModel):
    """The complete YAML environment catalog."""

    model_config = ConfigDict(extra="forbid")
    config_version: str = Field(default="environments-v1", min_length=1, max_length=128)
    plugin_instances: list[PluginInstanceConfig] = Field(
        default_factory=list, max_length=500
    )
    environments: list[EnvironmentConfig] = Field(default_factory=list, max_length=500)
    services: list[ServiceConfig] = Field(default_factory=list, max_length=2_000)
    nodes: list[NodeConfig] = Field(default_factory=list, max_length=10_000)
    sources: list[SourceConfig] = Field(default_factory=list, max_length=2_000)

    @model_validator(mode="before")
    @classmethod
    def accept_mapping_collections(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        for field_name in (
            "plugin_instances",
            "environments",
            "services",
            "nodes",
            "sources",
        ):
            collection = data.get(field_name)
            if isinstance(collection, dict):
                normalized = []
                for item_id, item in collection.items():
                    if not isinstance(item, dict):
                        normalized.append(item)
                        continue
                    record = dict(item)
                    record.setdefault("id", item_id)
                    normalized.append(record)
                data[field_name] = normalized
        return data

    @model_validator(mode="after")
    def validate_references(self) -> EnvironmentDirectory:
        def unique(items: list[Any], label: str) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for item in items:
                item_id = str(item.id)
                if item_id in result:
                    raise EnvironmentConfigError(f"duplicate {label} id: {item_id}")
                result[item_id] = item
            return result

        instances = unique(self.plugin_instances, "plugin instance")
        environments = unique(self.environments, "environment")
        services = unique(self.services, "service")
        nodes = unique(self.nodes, "node")
        sources = unique(self.sources, "source")
        if not environments and (services or nodes or sources):
            raise EnvironmentConfigError(
                "services, nodes and sources require at least one environment"
            )
        for service in services.values():
            if service.environment_id and service.environment_id not in environments:
                raise EnvironmentConfigError(
                    f"service {service.id} references unknown environment "
                    f"{service.environment_id}"
                )
            unknown = set(service.dependencies) - set(services)
            if unknown:
                raise EnvironmentConfigError(
                    f"service {service.id} references unknown dependencies: "
                    f"{sorted(unknown)}"
                )
        for node in nodes.values():
            if node.environment_id and node.environment_id not in environments:
                raise EnvironmentConfigError(
                    f"node {node.id} references unknown environment {node.environment_id}"
                )
        for source in sources.values():
            if source.plugin_instance_id not in instances:
                raise EnvironmentConfigError(
                    f"source {source.id} references unknown plugin instance "
                    f"{source.plugin_instance_id}"
                )
            if source.environment_id and source.environment_id not in environments:
                raise EnvironmentConfigError(
                    f"source {source.id} references unknown environment "
                    f"{source.environment_id}"
                )
            unknown_services = set(source.service_ids) - set(services)
            unknown_nodes = set(source.node_ids) - set(nodes)
            if unknown_services:
                raise EnvironmentConfigError(
                    f"source {source.id} references unknown services: "
                    f"{sorted(unknown_services)}"
                )
            if unknown_nodes:
                raise EnvironmentConfigError(
                    f"source {source.id} references unknown nodes: {sorted(unknown_nodes)}"
                )
            associated_environments = {
                services[item].environment_id
                for item in source.service_ids
                if services[item].environment_id
            } | {
                nodes[item].environment_id
                for item in source.node_ids
                if nodes[item].environment_id
            }
            associated_environments.discard(None)
            if source.environment_id:
                associated_environments.add(source.environment_id)
                if len(associated_environments) > 1:
                    raise EnvironmentConfigError(
                        f"source {source.id} mixes environments: "
                        f"{sorted(associated_environments)}"
                    )
            elif len(associated_environments) == 1:
                object.__setattr__(
                    source, "environment_id", next(iter(associated_environments))
                )
            elif len(associated_environments) > 1:
                raise EnvironmentConfigError(
                    f"source {source.id} mixes environments: "
                    f"{sorted(associated_environments)}"
                )
            elif len(environments) == 1:
                object.__setattr__(source, "environment_id", next(iter(environments)))
            elif not source.environment_id:
                raise EnvironmentConfigError(
                    f"source {source.id} must identify an environment"
                )
        return self


class SnapshotService(StrictModel):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    version: str | None = None
    dependencies: list[str] = Field(default_factory=list)


class SnapshotNode(StrictModel):
    id: str
    hostname: str
    instance_id: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)


class SnapshotSource(StrictModel):
    id: str
    kind: Literal["database", "logs"]
    plugin_instance_id: str
    plugin_id: str
    service_ids: list[str] = Field(default_factory=list)
    node_ids: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    limits: ToolLimits = Field(default_factory=ToolLimits)


class ResolvedEnvironmentSnapshot(StrictModel):
    """Credential-free, immutable environment data used by one run."""

    snapshot_id: str = Field(min_length=1, max_length=128)
    config_revision: str = Field(min_length=1, max_length=128)
    environment_id: str
    display_name: str
    level: str
    region: str | None = None
    timezone: str
    tags: dict[str, str] = Field(default_factory=dict)
    primary_service_id: str | None = None
    services: list[SnapshotService] = Field(default_factory=list)
    nodes: list[SnapshotNode] = Field(default_factory=list)
    sources: list[SnapshotSource] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def source(self, source_id: str) -> SnapshotSource | None:
        return next((source for source in self.sources if source.id == source_id), None)


def _normalize(value: str | None) -> str:
    return " ".join((value or "").strip().casefold().split())


def _remove_secrets(value: Any) -> Any:
    secret_keys = {
        "username",
        "password",
        "token",
        "api_key",
        "apikey",
        "secret",
        "secret_key",
    }
    if isinstance(value, dict):
        return {
            key: _remove_secrets(item)
            for key, item in value.items()
            if str(key).casefold() not in secret_keys
        }
    if isinstance(value, list):
        return [_remove_secrets(item) for item in value]
    return value


def _public_value(value: Any) -> Any:
    """Mask secret-shaped nested configuration without returning its value."""
    secret_keys = {
        "username",
        "password",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "secret",
        "secret_key",
    }
    if isinstance(value, dict):
        return {
            key: {"is_set": bool(item)}
            if str(key).casefold() in secret_keys
            else _public_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    return value


class EnvironmentRepository:
    """Thread-safe YAML repository with optimistic, atomic updates."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._write_lock = threading.Lock()
        self._directory = self._load()

    @property
    def configured(self) -> bool:
        return self.path is not None

    @property
    def writable(self) -> bool:
        return self.path is not None

    @property
    def revision(self) -> str:
        payload = _remove_secrets(self._directory.model_dump(mode="json"))
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]

    def _load(self) -> EnvironmentDirectory:
        if self.path is None:
            return EnvironmentDirectory()
        if not self.path.exists():
            raise EnvironmentConfigError(
                f"environment config does not exist: {self.path}"
            )
        try:
            loaded = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            return EnvironmentDirectory.model_validate(loaded)
        except EnvironmentConfigError:
            raise
        except Exception as exc:
            raise EnvironmentConfigError(
                "environment config failed validation"
            ) from exc

    def directory(self) -> EnvironmentDirectory:
        return self._directory.model_copy(deep=True)

    def public_config(self) -> dict[str, Any]:
        return _public_value(self._directory.model_dump(mode="python"))

    def environments(self) -> list[EnvironmentConfig]:
        return [
            item.model_copy(deep=True)
            for item in self._directory.environments
            if item.enabled
        ]

    def get_environment(self, environment_id: str) -> EnvironmentConfig:
        for environment in self._directory.environments:
            if environment.id == environment_id and environment.enabled:
                return environment
        raise EnvironmentConfigError(f"environment is unavailable: {environment_id}")

    def _service_matches(self, service: ServiceConfig, hint: str) -> bool:
        needle = _normalize(hint)
        return needle in {
            _normalize(service.id),
            _normalize(service.name),
            *(_normalize(item) for item in service.aliases),
        }

    def infer_candidates(
        self,
        target: TargetSpec,
        context: DiagnosisContext | dict[str, Any] | None = None,
    ) -> list[EnvironmentTargetCandidate]:
        context_values = (
            context.model_dump(mode="python")
            if isinstance(context, DiagnosisContext)
            else dict(context or {})
        )
        environment_hint = target.environment_id or (
            context_values.get("environment")
            if isinstance(context_values.get("environment"), str)
            else None
        )
        service_hint = target.primary_service_id or (
            context_values.get("service")
            if isinstance(context_values.get("service"), str)
            else None
        )
        services_by_env: dict[str, list[ServiceConfig]] = {}
        for service in self._directory.services:
            if service.environment_id:
                services_by_env.setdefault(service.environment_id, []).append(service)
        scored: list[tuple[int, EnvironmentTargetCandidate]] = []
        for environment in self._directory.environments:
            if not environment.enabled:
                continue
            score = 0
            matched: list[str] = []
            if environment_hint:
                needle = _normalize(environment_hint)
                if needle == _normalize(environment.id):
                    score += 100
                    matched.append("environment_id")
                elif needle == _normalize(environment.display_name):
                    score += 90
                    matched.append("environment_name")
                elif needle in {_normalize(item) for item in environment.aliases}:
                    score += 90
                    matched.append("environment_alias")
                elif needle and needle in _normalize(environment.display_name):
                    score += 50
                    matched.append("environment_name_partial")
                else:
                    continue
            if service_hint:
                matching_services = [
                    service
                    for service in services_by_env.get(environment.id, [])
                    if self._service_matches(service, service_hint)
                ]
                if not matching_services:
                    # A service hint is meaningful only when it is represented
                    # in the selected environment; never silently cross it.
                    continue
                score += 40
                matched.append("service")
            if not environment_hint and not service_hint:
                matched.append("catalog")
            scored.append(
                (
                    score,
                    EnvironmentTargetCandidate(
                        environment_id=environment.id,
                        display_name=environment.display_name,
                        aliases=environment.aliases,
                        level=environment.level,
                        region=environment.region,
                        timezone=environment.timezone,
                        matched_by=matched,
                    ),
                )
            )
        scored.sort(key=lambda item: (-item[0], item[1].environment_id))
        return [item[1] for item in scored[:20]]

    def resolved_target(
        self,
        target: TargetSpec,
        *,
        candidate_ids: set[str] | None = None,
    ) -> ResolvedTarget:
        if not target.environment_id:
            raise EnvironmentConfigError("an environment_id is required")
        if candidate_ids is not None and target.environment_id not in candidate_ids:
            raise EnvironmentConfigError(
                "target environment is not a pending candidate"
            )
        environment = self.get_environment(target.environment_id)
        service_id = target.primary_service_id
        if service_id:
            service = next(
                (item for item in self._directory.services if item.id == service_id),
                None,
            )
            if service is None:
                raise EnvironmentConfigError(f"service is unknown: {service_id}")
            if service.environment_id and service.environment_id != environment.id:
                raise EnvironmentConfigError(
                    "primary service belongs to another environment"
                )
        return ResolvedTarget(
            mode="explicit",
            environment_id=environment.id,
            primary_service_id=service_id,
        )

    def snapshot(
        self,
        environment_id: str,
        primary_service_id: str | None = None,
    ) -> ResolvedEnvironmentSnapshot:
        environment = self.get_environment(environment_id)
        services = [
            service
            for service in self._directory.services
            if service.environment_id in {None, environment_id}
        ]
        nodes = [
            node
            for node in self._directory.nodes
            if node.environment_id in {None, environment_id}
        ]
        sources = [
            source
            for source in self._directory.sources
            if source.enabled and source.environment_id == environment_id
        ]
        instances = {item.id: item for item in self._directory.plugin_instances}
        snapshot_services = [
            SnapshotService.model_validate(
                item.model_dump(mode="python", exclude={"environment_id"})
            )
            for item in services
        ]
        snapshot_nodes = [
            SnapshotNode.model_validate(
                item.model_dump(mode="python", exclude={"environment_id"})
            )
            for item in nodes
        ]
        snapshot_sources = [
            SnapshotSource(
                id=source.id,
                kind=source.kind,
                plugin_instance_id=source.plugin_instance_id,
                plugin_id=instances[source.plugin_instance_id].plugin_id,
                service_ids=source.service_ids,
                node_ids=source.node_ids,
                config=_remove_secrets(copy.deepcopy(source.config)),
                limits=source.limits,
            )
            for source in sources
            if source.plugin_instance_id in instances
            and instances[source.plugin_instance_id].enabled
        ]
        canonical = {
            "config_revision": self.revision,
            "environment": environment.model_dump(mode="json"),
            "primary_service_id": primary_service_id,
            "services": [item.model_dump(mode="json") for item in snapshot_services],
            "nodes": [item.model_dump(mode="json") for item in snapshot_nodes],
            "sources": [item.model_dump(mode="json") for item in snapshot_sources],
        }
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        snapshot_id = (
            "envsnap_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
        )
        return ResolvedEnvironmentSnapshot(
            snapshot_id=snapshot_id,
            config_revision=self.revision,
            environment_id=environment.id,
            display_name=environment.display_name,
            level=environment.level,
            region=environment.region,
            timezone=environment.timezone,
            tags=environment.tags,
            primary_service_id=primary_service_id,
            services=snapshot_services,
            nodes=snapshot_nodes,
            sources=snapshot_sources,
        )

    def validate_raw(self, raw: dict[str, Any]) -> EnvironmentDirectory:
        if not isinstance(raw, dict):
            raise EnvironmentConfigError("environment config must be an object")
        return EnvironmentDirectory.model_validate(raw)

    def prepare(
        self,
        raw: dict[str, Any],
        expected_revision: str,
        *,
        secret_updates: dict[str, dict[str, dict[str, Any]]] | None = None,
    ) -> EnvironmentDirectory:
        """Validate a complete candidate, including explicit secret changes.

        The admin API exposes secret presence rather than secret values.  This
        merge happens before plugin validation so a failed plugin check cannot
        leave a newly written but unusable catalog behind.
        """
        if expected_revision != self.revision:
            raise EnvironmentRevisionConflictError(
                "environment config revision changed"
            )
        if self.path is None:
            raise EnvironmentConfigNotWritableError(
                "environment config is not writable without BUGLENS_ENVIRONMENTS_CONFIG"
            )
        incoming = self._merge_secret_updates(raw, secret_updates)
        return self.validate_raw(incoming)

    def _merge_secret_updates(
        self,
        raw: dict[str, Any],
        secret_updates: dict[str, dict[str, dict[str, Any]]] | None,
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise EnvironmentConfigError("environment config must be an object")
        incoming = copy.deepcopy(raw)
        current = self._directory.model_dump(mode="python")
        current_instances = {item["id"]: item for item in current["plugin_instances"]}
        incoming_instances = incoming.get("plugin_instances", [])
        if isinstance(incoming_instances, dict):
            incoming_instances = [
                {**dict(value), "id": dict(value).get("id", item_id)}
                for item_id, value in incoming_instances.items()
            ]
            incoming["plugin_instances"] = incoming_instances
        if not isinstance(incoming_instances, list):
            raise EnvironmentConfigError("plugin_instances must be a list or mapping")
        for item in incoming_instances:
            if not isinstance(item, dict):
                continue
            old = current_instances.get(str(item.get("id")), {})
            for secret_name in ("username", "password", "token"):
                submitted = item.get(secret_name)
                if isinstance(submitted, dict) and "is_set" in submitted:
                    if old.get(secret_name):
                        item[secret_name] = old[secret_name]
                    else:
                        item.pop(secret_name, None)
                elif secret_name not in item and old.get(secret_name):
                    item[secret_name] = old[secret_name]
            item.pop("secret_updates", None)
        for instance_id, updates in (secret_updates or {}).items():
            target = next(
                (item for item in incoming_instances if item.get("id") == instance_id),
                None,
            )
            if target is None:
                raise EnvironmentConfigError(
                    f"secret update references unknown plugin instance: {instance_id}"
                )
            if not isinstance(updates, dict):
                raise EnvironmentConfigError("secret updates must be an object")
            for secret_name, update in updates.items():
                if secret_name not in {"username", "password", "token"}:
                    raise EnvironmentConfigError(
                        f"unsupported secret field: {secret_name}"
                    )
                if not isinstance(update, dict):
                    raise EnvironmentConfigError("secret update must be an object")
                action = update.get("action")
                if action == "clear":
                    target.pop(secret_name, None)
                elif action == "set":
                    value = update.get("value")
                    if not isinstance(value, str) or not value:
                        raise EnvironmentConfigError(
                            f"{secret_name} secret value must be a non-empty string"
                        )
                    target[secret_name] = value
                else:
                    raise EnvironmentConfigError(
                        "secret update action must be set or clear"
                    )
        return incoming

    def apply(
        self,
        raw: dict[str, Any],
        expected_revision: str,
        *,
        secret_updates: dict[str, dict[str, dict[str, Any]]] | None = None,
    ) -> str:
        with self._write_lock:
            incoming = self._merge_secret_updates(raw, secret_updates)
            directory = self.prepare(incoming, expected_revision)
            payload = yaml.safe_dump(
                directory.model_dump(mode="python"), sort_keys=False, allow_unicode=True
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.tmp")
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self.path)
            self._directory = directory
            return self.revision


__all__ = [
    "EnvironmentConfig",
    "EnvironmentConfigError",
    "EnvironmentConfigNotWritableError",
    "EnvironmentDirectory",
    "EnvironmentRepository",
    "EnvironmentRevisionConflictError",
    "NodeConfig",
    "PluginInstanceConfig",
    "ResolvedEnvironmentSnapshot",
    "ServiceConfig",
    "SnapshotNode",
    "SnapshotService",
    "SnapshotSource",
    "SourceConfig",
    "ToolLimits",
]
