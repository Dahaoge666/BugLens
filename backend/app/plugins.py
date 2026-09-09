"""Plugin discovery and the core-to-SDK environment tool bridge."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.metadata
import inspect
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from agents import RunContextWrapper
from buglens_plugin_api import (
    PLUGIN_API_MAJOR,
    ExecutionContext,
    PluginHealth,
    PluginManifest,
    ToolResult,
    ToolResultStatus,
)

from .environment import (
    EnvironmentDirectory,
    EnvironmentRepository,
    PluginInstanceConfig,
    ResolvedEnvironmentSnapshot,
    SnapshotSource,
    ToolLimits,
)
from .models import EvidenceRecord
from .security import sanitize_data
from .tools import ToolRegistry


class PluginError(RuntimeError):
    code = "plugin_error"


class PluginDiscoveryError(PluginError):
    code = "plugin_discovery_failed"


class PluginCompatibilityError(PluginError):
    code = "plugin_api_incompatible"


class PluginConfigError(PluginError):
    code = "plugin_config_invalid"


@dataclass(frozen=True)
class _PluginRegistration:
    manifest: PluginManifest
    factory: Any
    entry_point: str | None = None


def _as_manifest(value: Any) -> PluginManifest:
    manifest = getattr(value, "manifest", value)
    try:
        return (
            manifest
            if isinstance(manifest, PluginManifest)
            else PluginManifest.model_validate(manifest)
        )
    except Exception as exc:
        raise PluginDiscoveryError("plugin manifest is invalid") from exc


def _call_factory(factory: Any, config: dict[str, Any]) -> Any:
    """Support both ``factory(config)`` and no-argument plugin factories."""
    if not callable(factory):
        return copy.copy(factory)
    try:
        return factory(config)
    except TypeError as first_error:
        try:
            return factory()
        except TypeError:
            raise first_error


class PluginManager:
    """Discover, validate and lifecycle-manage deterministic plugin instances."""

    def __init__(self, plugins: list[Any] | None = None) -> None:
        self._registrations: dict[str, _PluginRegistration] = {}
        self._instances: dict[str, Any] = {}
        self._instance_fingerprints: dict[str, str] = {}
        for plugin in plugins or []:
            self.register(plugin)

    @classmethod
    def discover(
        cls,
        entry_points: Any | None = None,
        *,
        group: str = "buglens.tool_plugins",
    ) -> PluginManager:
        manager = cls()
        if entry_points is None:
            entry_points = importlib.metadata.entry_points()
        if hasattr(entry_points, "select"):
            selected = list(entry_points.select(group=group))
        elif isinstance(entry_points, dict):
            selected = list(entry_points.get(group, ()))
        else:
            selected = [
                item for item in entry_points if getattr(item, "group", group) == group
            ]
        for entry_point in selected:
            try:
                loaded = entry_point.load()
                manager.register(loaded, entry_point=getattr(entry_point, "name", None))
            except PluginError:
                raise
            except Exception as exc:
                raise PluginDiscoveryError(
                    f"failed to load plugin entry point {getattr(entry_point, 'name', 'unknown')}"
                ) from exc
        return manager

    def register(
        self, plugin: Any, *, entry_point: str | None = None
    ) -> PluginManifest:
        """Register a plugin object, class or factory and reject duplicate IDs."""
        manifest_source = getattr(plugin, "manifest", None)
        if manifest_source is None and callable(plugin):
            manifest_source = getattr(plugin, "__buglens_manifest__", None)
        if manifest_source is None:
            try:
                manifest_source = _call_factory(plugin, {})
            except Exception as exc:
                raise PluginDiscoveryError(
                    "plugin factory cannot expose a manifest"
                ) from exc
        manifest = _as_manifest(manifest_source)
        if manifest.api_major != PLUGIN_API_MAJOR:
            raise PluginCompatibilityError(
                f"plugin {manifest.plugin_id} requires API {manifest.api_major}, "
                f"core supports {PLUGIN_API_MAJOR}"
            )
        if manifest.plugin_id in self._registrations:
            raise PluginDiscoveryError(f"duplicate plugin id: {manifest.plugin_id}")
        if callable(plugin) and not hasattr(plugin, "execute"):
            factory = plugin
        else:
            prototype = plugin

            def factory(config: dict[str, Any], prototype: Any = prototype) -> Any:
                if hasattr(prototype, "create") and callable(prototype.create):
                    return prototype.create(config)
                instance = copy.copy(prototype)
                if hasattr(instance, "configure") and callable(instance.configure):
                    instance.configure(config)
                elif hasattr(instance, "instance_config"):
                    instance.instance_config = config
                return instance

        self._registrations[manifest.plugin_id] = _PluginRegistration(
            manifest=manifest,
            factory=factory,
            entry_point=entry_point,
        )
        return manifest

    def manifests(self) -> list[PluginManifest]:
        return [registration.manifest for registration in self._registrations.values()]

    def manifest(self, plugin_id: str) -> PluginManifest:
        registration = self._registrations.get(plugin_id)
        if registration is None:
            raise PluginConfigError(f"plugin is not installed: {plugin_id}")
        return registration.manifest

    def _validate_schema(self, schema: dict[str, Any], value: Any, label: str) -> None:
        if not schema:
            return
        try:
            from jsonschema import Draft202012Validator

            Draft202012Validator.check_schema(schema)
            validator = Draft202012Validator(schema)
            errors = sorted(
                validator.iter_errors(value), key=lambda error: list(error.path)
            )
        except (
            ImportError
        ) as exc:  # pragma: no cover - dependency is declared by backend
            raise PluginConfigError("JSON Schema validation is unavailable") from exc
        except Exception as exc:
            raise PluginConfigError(f"{label} plugin schema is invalid") from exc
        if errors:
            detail = "; ".join(error.message for error in errors[:5])
            raise PluginConfigError(f"{label} does not match plugin schema: {detail}")

    def validate_instance(
        self,
        instance: PluginInstanceConfig,
        source_configs: list[dict[str, Any]] | None = None,
    ) -> None:
        registration = self._registrations.get(instance.plugin_id)
        if registration is None:
            if instance.enabled:
                raise PluginConfigError(
                    f"plugin is not installed: {instance.plugin_id}"
                )
            return
        self._validate_schema(
            registration.manifest.instance_config_schema,
            instance.config,
            f"plugin instance {instance.id}",
        )
        for source_config in source_configs or []:
            self._validate_schema(
                registration.manifest.source_config_schema,
                source_config or {},
                f"plugin instance {instance.id} source",
            )
        plugin = _call_factory(registration.factory, self._runtime_config(instance))
        validator = getattr(plugin, "validate_config", None)
        if callable(validator):
            for source_config in source_configs or [None]:
                try:
                    _invoke_validate(
                        validator, self._runtime_config(instance), source_config
                    )
                except PluginConfigError:
                    raise
                except Exception as exc:
                    raise PluginConfigError(
                        f"plugin instance {instance.id} rejected its configuration"
                    ) from exc
        close = getattr(plugin, "close", None)
        if callable(close):
            close()

    def validate_directory(self, directory: EnvironmentDirectory) -> None:
        sources_by_instance: dict[str, list[dict[str, Any]]] = {}
        for source in directory.sources:
            sources_by_instance.setdefault(source.plugin_instance_id, []).append(
                source.config
            )
        for instance in directory.plugin_instances:
            self.validate_instance(instance, sources_by_instance.get(instance.id))

    @staticmethod
    def _runtime_config(instance: PluginInstanceConfig) -> dict[str, Any]:
        payload = copy.deepcopy(instance.config)
        payload.update(
            {
                "id": instance.id,
                "username": instance.username,
                "password": instance.password,
                "token": instance.token,
                "default_limits": instance.default_limits.model_dump(mode="json"),
            }
        )
        return payload

    def instance(self, instance: PluginInstanceConfig) -> Any:
        if not instance.enabled:
            raise PluginConfigError(f"plugin instance is disabled: {instance.id}")
        registration = self._registrations.get(instance.plugin_id)
        if registration is None:
            raise PluginConfigError(f"plugin is not installed: {instance.plugin_id}")
        config = self._runtime_config(instance)
        fingerprint = hashlib.sha256(
            json.dumps(
                config, sort_keys=True, default=str, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if self._instance_fingerprints.get(instance.id) != fingerprint:
            old = self._instances.pop(instance.id, None)
            if old is not None and callable(getattr(old, "close", None)):
                old.close()
            plugin = _call_factory(registration.factory, config)
            validator = getattr(plugin, "validate_config", None)
            if callable(validator):
                try:
                    _invoke_validate(validator, config, None)
                except PluginConfigError:
                    raise
                except Exception as exc:
                    raise PluginConfigError(
                        f"plugin instance {instance.id} rejected its configuration"
                    ) from exc
            self._instances[instance.id] = plugin
            self._instance_fingerprints[instance.id] = fingerprint
        return self._instances[instance.id]

    async def check_health(self, instance: PluginInstanceConfig) -> PluginHealth:
        try:
            plugin = self.instance(instance)
            checker = getattr(plugin, "check_health", None)
            if not callable(checker):
                return PluginHealth(
                    status="degraded",
                    detail="plugin does not advertise a health check",
                    plugin_id=instance.plugin_id,
                )
            result = checker()
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, PluginHealth):
                return result.model_copy(
                    update={
                        "plugin_id": result.plugin_id or instance.plugin_id,
                        "implementation_version": (
                            result.implementation_version
                            or self.manifest(instance.plugin_id).implementation_version
                        ),
                    }
                )
            return PluginHealth.model_validate(
                {
                    **dict(result),
                    "plugin_id": instance.plugin_id,
                    "implementation_version": self.manifest(
                        instance.plugin_id
                    ).implementation_version,
                }
            )
        except Exception:
            return PluginHealth(
                status="error",
                detail="plugin health check failed",
                plugin_id=instance.plugin_id,
            )

    async def execute(
        self,
        instance: PluginInstanceConfig,
        operation: str,
        source_config: dict[str, Any],
        request: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        plugin = self.instance(instance)
        executor = getattr(plugin, "execute", None)
        if not callable(executor):
            raise PluginError("plugin does not implement execute")
        result = executor(operation, source_config, request, context)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ToolResult):
            return result
        return ToolResult.model_validate(result)

    def close(self) -> None:
        for plugin in list(self._instances.values()):
            close = getattr(plugin, "close", None)
            if callable(close):
                close()
        self._instances.clear()
        self._instance_fingerprints.clear()


def _invoke_validate(
    validator: Any, instance_config: dict[str, Any], source_config: Any
) -> None:
    try:
        result = validator(instance_config, source_config)
    except TypeError:
        result = validator(instance_config)
    if inspect.isawaitable(result):
        raise PluginConfigError("validate_config must be synchronous")
    if result is False:
        raise PluginConfigError("plugin rejected its configuration")


@dataclass
class _RunBudget:
    max_calls: int
    max_concurrent: int
    used: int = 0
    active: int = 0
    # The service can be used by CLI commands that create a fresh event loop
    # per invocation.  This lock therefore must not be bound to one asyncio
    # loop; the critical section contains no await and is thread-safe instead.
    lock: threading.Lock | None = None


class EnvironmentToolService:
    """Resolve catalog sources and turn plugin results into safe tool output."""

    def __init__(
        self, repository: EnvironmentRepository, plugins: PluginManager
    ) -> None:
        self.repository = repository
        self.plugins = plugins
        self._budgets: dict[str, _RunBudget] = {}

    def _budget(self, runtime: Any) -> _RunBudget:
        run_id = str(getattr(runtime, "graph_run_id", "unknown"))
        budget = self._budgets.get(run_id)
        if budget is None:
            budget = _RunBudget(
                max_calls=max(0, int(getattr(runtime, "tool_max_calls", 20))),
                max_concurrent=max(1, int(getattr(runtime, "tool_max_concurrent", 2))),
                lock=threading.Lock(),
            )
            self._budgets[run_id] = budget
        return budget

    @staticmethod
    def _snapshot(runtime: Any) -> ResolvedEnvironmentSnapshot | None:
        value = getattr(runtime, "environment_snapshot", None)
        return value if isinstance(value, ResolvedEnvironmentSnapshot) else None

    async def describe_environment(self, runtime: Any) -> dict[str, Any]:
        snapshot = self._snapshot(runtime)
        if snapshot is None:
            return _tool_result(
                ToolResult(
                    status=ToolResultStatus.REJECTED, warnings=["target_not_confirmed"]
                )
            )
        budget = self._budget(runtime)
        rejection = self._reserve(budget)
        if rejection is not None:
            return _tool_result(
                ToolResult(
                    status=ToolResultStatus.REJECTED,
                    warnings=[rejection],
                )
            )
        try:
            return _tool_result(
                ToolResult(
                    status=ToolResultStatus.SUCCEEDED,
                    structured_result=snapshot.model_dump(mode="json"),
                )
            )
        finally:
            self._release(budget)

    async def call(
        self, operation: str, runtime: Any, request: dict[str, Any]
    ) -> dict[str, Any]:
        snapshot = self._snapshot(runtime)
        if snapshot is None or not getattr(runtime, "environment_snapshot_id", None):
            return _tool_result(
                ToolResult(
                    status=ToolResultStatus.REJECTED, warnings=["target_not_confirmed"]
                )
            )
        budget = self._budget(runtime)
        rejection = self._reserve(budget)
        if rejection is not None:
            return _tool_result(
                ToolResult(
                    status=ToolResultStatus.REJECTED,
                    warnings=[rejection],
                )
            )
        started = time.monotonic()
        try:
            if operation == "describe_database":
                kind = "database"
            elif operation == "query_database":
                kind = "database"
            elif operation == "search_logs":
                kind = "logs"
            else:
                return _tool_result(
                    ToolResult(
                        status=ToolResultStatus.REJECTED,
                        warnings=["unknown_tool_operation"],
                    )
                )
            source_id = request.get("source_id")
            source = snapshot.source(str(source_id)) if source_id else None
            if source is None or source.kind != kind:
                return _tool_result(
                    ToolResult(
                        status=ToolResultStatus.REJECTED,
                        warnings=["source_not_allowed"],
                    )
                )
            try:
                instance = self._current_instance(source)
            except PluginError:
                return _tool_result(
                    ToolResult(
                        status=ToolResultStatus.UNAVAILABLE,
                        warnings=["plugin_unavailable"],
                    )
                )
            limits = _effective_limits(instance.default_limits, source.limits)
            profile_max_results = max(
                1,
                int(
                    getattr(
                        runtime,
                        "tool_max_result_rows",
                        getattr(runtime, "tool_max_results", 200),
                    )
                ),
            )
            profile_max_bytes = max(
                1_024, int(getattr(runtime, "tool_result_bytes", 65_536))
            )
            max_results = min(limits.max_results, profile_max_results, 1_000)
            max_bytes = min(limits.max_bytes, profile_max_bytes, 1_048_576)
            timeout = min(
                limits.timeout_seconds,
                max(1, int(getattr(runtime, "tool_timeout_seconds", 10))),
                30,
            )
            context = ExecutionContext(
                execution_id=str(getattr(runtime, "execution_id", None) or uuid4().hex),
                run_id=str(getattr(runtime, "graph_run_id", "unknown")),
                environment_snapshot_id=snapshot.snapshot_id,
                plugin_instance_id=source.plugin_instance_id,
                deadline=datetime.now(UTC) + timedelta(seconds=timeout),
                max_results=max_results,
                max_bytes=max_bytes,
                max_scan_files=limits.max_scan_files,
                max_scan_bytes=limits.max_scan_bytes,
            )
            call_request = dict(request)
            call_request.pop("source_id", None)
            call_request["max_results"] = max_results
            call_request["max_bytes"] = max_bytes
            try:
                result = await asyncio.wait_for(
                    self.plugins.execute(
                        instance, operation, source.config, call_request, context
                    ),
                    timeout=max(0.1, context.remaining_seconds),
                )
            except asyncio.TimeoutError:
                result = ToolResult(
                    status=ToolResultStatus.UNAVAILABLE,
                    warnings=["tool_timeout"],
                )
            except PluginError:
                result = ToolResult(
                    status=ToolResultStatus.UNAVAILABLE,
                    warnings=["plugin_unavailable"],
                )
            except Exception:
                # Plugin tracebacks are intentionally never returned to the model.
                result = ToolResult(
                    status=ToolResultStatus.UNAVAILABLE,
                    warnings=["plugin_execution_failed"],
                )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if result.elapsed_ms is None:
                result = result.model_copy(update={"elapsed_ms": elapsed_ms})
            return _tool_result(
                result,
                evidence=self._evidence_for(result, source, operation, elapsed_ms),
            )
        finally:
            self._release(budget)

    @staticmethod
    def _reserve(budget: _RunBudget) -> str | None:
        assert budget.lock is not None
        with budget.lock:
            if budget.used >= budget.max_calls:
                return "tool_budget_exceeded"
            if budget.active >= budget.max_concurrent:
                return "tool_concurrency_limit"
            budget.used += 1
            budget.active += 1
            return None

    @staticmethod
    def _release(budget: _RunBudget) -> None:
        assert budget.lock is not None
        with budget.lock:
            budget.active = max(0, budget.active - 1)

    def _current_instance(self, source: SnapshotSource) -> PluginInstanceConfig:
        current = next(
            (
                item
                for item in self.repository.directory().plugin_instances
                if item.id == source.plugin_instance_id
            ),
            None,
        )
        if (
            current is None
            or not current.enabled
            or current.plugin_id != source.plugin_id
        ):
            raise PluginConfigError("plugin instance is no longer enabled")
        return current

    def audit_metadata(
        self, tool_name: str, runtime: Any, arguments: dict[str, Any] | Any
    ) -> dict[str, Any]:
        """Return safe, stable plugin fields for the tool audit ledger."""
        operation_by_tool = {
            "describe_database": "describe_database",
            "query_database": "query_database",
            "search_logs": "search_logs",
        }
        operation = operation_by_tool.get(tool_name)
        if operation is None or not isinstance(arguments, dict):
            return {}
        snapshot = self._snapshot(runtime)
        source_id = arguments.get("source_id")
        source = snapshot.source(str(source_id)) if snapshot and source_id else None
        if source is None:
            return {
                "environment_snapshot_id": (
                    snapshot.snapshot_id if snapshot is not None else None
                ),
                "source_id": str(source_id) if source_id else None,
                "operation": operation,
            }
        manifest = self.plugins.manifest(source.plugin_id)
        metadata: dict[str, Any] = {
            "plugin_id": source.plugin_id,
            "plugin_implementation_version": manifest.implementation_version,
            "plugin_instance_id": source.plugin_instance_id,
            "environment_snapshot_id": snapshot.snapshot_id if snapshot else None,
            "source_id": source.id,
            "operation": operation,
        }
        if operation == "query_database" and isinstance(arguments.get("sql"), str):
            redacted = _redact_query(arguments["sql"])
            metadata["redacted_query"] = redacted
            metadata["query_fingerprint"] = hashlib.sha256(
                redacted.encode("utf-8")
            ).hexdigest()
        return metadata

    def _evidence_for(
        self,
        result: ToolResult,
        source: SnapshotSource,
        operation: str,
        elapsed_ms: int,
    ) -> list[EvidenceRecord]:
        if result.status not in {ToolResultStatus.SUCCEEDED, ToolResultStatus.PARTIAL}:
            return []
        if result.structured_result is None:
            return []
        clean = sanitize_data(result.structured_result)
        encoded = json.dumps(
            clean, ensure_ascii=False, default=str, separators=(",", ":")
        )
        max_content_bytes = 12_000
        if len(encoded.encode("utf-8")) > max_content_bytes:
            encoded = encoded.encode("utf-8")[:max_content_bytes].decode(
                "utf-8", "ignore"
            )
        locator = f"{source.id}:{result.cursor or 'page-1'}"
        return [
            EvidenceRecord(
                source="plugin",
                source_type=source.kind,
                source_reference=locator,
                reference=locator,
                content=encoded or "{}",
                observed_at=datetime.now(UTC).isoformat(),
                metadata={
                    "operation": operation,
                    "source_id": source.id,
                    "elapsed_ms": elapsed_ms,
                    "truncated": result.truncated,
                    "warnings": list(result.warnings)[:10],
                },
            )
        ]

    async def check_instance(self, instance_id: str) -> PluginHealth:
        instance = next(
            (
                item
                for item in self.repository.directory().plugin_instances
                if item.id == instance_id
            ),
            None,
        )
        if instance is None:
            return PluginHealth(status="error", detail="plugin instance is unknown")
        try:
            return await self.plugins.check_health(instance)
        except PluginError:
            return PluginHealth(
                status="error",
                detail="plugin health check is unavailable",
                plugin_id=instance.plugin_id,
            )

    def close(self) -> None:
        self.plugins.close()


def _tool_result(
    result: ToolResult, *, evidence: list[EvidenceRecord] | None = None
) -> dict[str, Any]:
    payload = dict(sanitize_data(result.model_dump(mode="json", by_alias=False)))
    payload["result"] = payload.pop("structured_result", None)
    if evidence:
        payload["evidence"] = [item.model_dump(mode="json") for item in evidence]
    return payload


def _effective_limits(instance_limits, source_limits):
    """Apply the strictest configured limit at instance and source scope."""
    return ToolLimits(
        max_results=min(instance_limits.max_results, source_limits.max_results),
        timeout_seconds=min(
            instance_limits.timeout_seconds, source_limits.timeout_seconds
        ),
        max_bytes=min(instance_limits.max_bytes, source_limits.max_bytes),
        max_scan_files=min(
            instance_limits.max_scan_files, source_limits.max_scan_files
        ),
        max_scan_bytes=min(
            instance_limits.max_scan_bytes, source_limits.max_scan_bytes
        ),
    )


def _redact_query(value: str) -> str:
    """Normalize SQL before it is persisted in the audit ledger."""
    without_comments = re.sub(r"--[^\r\n]*|/\*.*?\*/", " ", value, flags=re.S)
    redacted = re.sub(r"'(?:''|[^'])*'", "?", without_comments)
    redacted = re.sub(r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?(?![A-Za-z0-9_])", "?", redacted)
    return " ".join(redacted.split())[:16_384]


class EnvironmentToolRegistry(ToolRegistry):
    """Expose only the four stable environment tools to InvestigateAgent."""

    def __init__(self, service: EnvironmentToolService) -> None:
        super().__init__(
            enabled=True,
            allowed_nodes={"investigate"},
            max_results=4,
            max_result_bytes=65_536,
            timeout_seconds=30,
        )
        self.service = service
        self.register(
            self._describe_environment,
            name="describe_environment",
            version="environment-tools-v1",
            nodes={"investigate"},
            strict_mode=True,
        )
        self.register(
            self._describe_database,
            name="describe_database",
            version="environment-tools-v1",
            nodes={"investigate"},
            strict_mode=False,
        )
        self.register(
            self._query_database,
            name="query_database",
            version="environment-tools-v1",
            nodes={"investigate"},
            strict_mode=False,
        )
        self.register(
            self._search_logs,
            name="search_logs",
            version="environment-tools-v1",
            nodes={"investigate"},
            strict_mode=False,
        )

    def tools_for(
        self, *, environment_snapshot_id: str | None = None, **kwargs: Any
    ) -> list[Any]:
        if not environment_snapshot_id:
            return []
        return super().tools_for(**kwargs)

    def audit_metadata(
        self, tool_name: str, runtime: Any, arguments: dict[str, Any] | Any
    ) -> dict[str, Any]:
        return self.service.audit_metadata(tool_name, runtime, arguments)

    async def _describe_environment(
        self, context: RunContextWrapper[Any]
    ) -> dict[str, Any]:
        return await self.service.describe_environment(context.context)

    async def _describe_database(
        self,
        context: RunContextWrapper[Any],
        source_id: str,
        schema: str | None = None,
        table_pattern: str | None = None,
    ) -> dict[str, Any]:
        return await self.service.call(
            "describe_database",
            context.context,
            {"source_id": source_id, "schema": schema, "table_pattern": table_pattern},
        )

    async def _query_database(
        self,
        context: RunContextWrapper[Any],
        source_id: str,
        sql: str,
        parameters: dict[str, Any] | None = None,
        purpose: str = "diagnosis",
    ) -> dict[str, Any]:
        return await self.service.call(
            "query_database",
            context.context,
            {
                "source_id": source_id,
                "sql": sql,
                "parameters": parameters or {},
                "purpose": purpose,
            },
        )

    async def _search_logs(
        self,
        context: RunContextWrapper[Any],
        source_id: str,
        start_time: str,
        end_time: str,
        text_query: str = "",
        service_ids: list[str] | None = None,
        node_ids: list[str] | None = None,
        levels: list[str] | None = None,
        correlation_ids: list[str] | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return await self.service.call(
            "search_logs",
            context.context,
            {
                "source_id": source_id,
                "start_time": start_time,
                "end_time": end_time,
                "text_query": text_query,
                "service_ids": service_ids or [],
                "node_ids": node_ids or [],
                "levels": levels or [],
                "correlation_ids": correlation_ids or [],
                "cursor": cursor,
            },
        )


def build_plugin_runtime(
    repository: EnvironmentRepository,
    manager: PluginManager | None = None,
) -> tuple[PluginManager, EnvironmentToolService, EnvironmentToolRegistry]:
    manager = manager or PluginManager.discover()
    if repository.configured:
        manager.validate_directory(repository.directory())
    service = EnvironmentToolService(repository, manager)
    return manager, service, EnvironmentToolRegistry(service)


__all__ = [
    "EnvironmentToolRegistry",
    "EnvironmentToolService",
    "PluginCompatibilityError",
    "PluginConfigError",
    "PluginDiscoveryError",
    "PluginError",
    "PluginManager",
    "build_plugin_runtime",
]
