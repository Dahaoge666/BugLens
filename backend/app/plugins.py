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

from .capabilities import CapabilityRegistry
from .environment import (
    EnvironmentDirectory,
    EnvironmentRepository,
    PluginInstanceConfig,
    ResolvedEnvironmentSnapshot,
    SnapshotPluginInstance,
    SnapshotSource,
    ToolLimits,
    TransportConfig,
)
from .models import EvidenceRecord
from .security import sanitize_data
from .tools import ToolRegistry
from .transports import (
    TransportError,
    TransportTimeout,
    build_transport,
)


class PluginError(RuntimeError):
    code = "plugin_error"


class PluginDiscoveryError(PluginError):
    code = "plugin_discovery_failed"


class PluginCompatibilityError(PluginError):
    code = "plugin_api_incompatible"


class PluginConfigError(PluginError):
    code = "plugin_config_invalid"


_TRANSPORT_PLUGIN_IDS = frozenset({"mcp", "cli", "ssh"})


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
        inspect.signature(factory).bind(config)
    except TypeError:
        return factory()
    except (ValueError, AttributeError):
        # Some extension callables do not expose a signature.  Calling with
        # the documented config argument preserves their own TypeError rather
        # than mistaking it for an arity mismatch.
        pass
    return factory(config)


def _manifest_supports_source(
    source_kind: str, requested_capabilities: list[str], manifest_capabilities: set[str]
) -> bool:
    """Match legacy family capabilities and newer versioned capability IDs.

    Existing drivers advertise a family such as ``database`` while new
    connectors can advertise IDs such as ``database.query.v1``.  A source may
    explicitly list either form; a legacy family declaration is allowed to
    satisfy versioned IDs from that same family, but never an unrelated one.
    """

    if "*" in manifest_capabilities:
        return True
    required = set(requested_capabilities or [source_kind])
    if source_kind in manifest_capabilities:
        if "*" in required:
            return True
        return all(
            item in manifest_capabilities
            or item == source_kind
            or item.startswith(f"{source_kind}.")
            for item in required
        )
    return required.issubset(manifest_capabilities)


class PluginManager:
    """Discover, validate and lifecycle-manage deterministic plugin instances."""

    def __init__(self, plugins: list[Any] | None = None) -> None:
        self._registrations: dict[str, _PluginRegistration] = {}
        self._instances: dict[str, Any] = {}
        self._instance_fingerprints: dict[str, str] = {}
        self._instance_lock = threading.RLock()
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

    @staticmethod
    def _transport_manifest(plugin_id: str) -> PluginManifest:
        """Return an internal manifest for a declarative transport instance."""

        return PluginManifest(
            plugin_id=plugin_id,
            implementation_version="builtin-transport-v1",
            capabilities=["*"],
            health_check=True,
        )

    def manifest(self, plugin_id: str) -> PluginManifest:
        registration = self._registrations.get(plugin_id)
        if registration is None:
            if plugin_id in _TRANSPORT_PLUGIN_IDS:
                return self._transport_manifest(plugin_id)
            raise PluginConfigError(f"plugin is not installed: {plugin_id}")
        return registration.manifest

    def manifest_for_instance(self, instance: PluginInstanceConfig) -> PluginManifest:
        if instance.transport.type != "driver":
            return self._transport_manifest(instance.plugin_id)
        return self.manifest(instance.plugin_id)

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
        if instance.transport.type != "driver":
            # Declarative MCP/CLI/SSH instances do not require a Python wheel.
            # The transport config is already strictly validated by Pydantic;
            # its endpoint is checked lazily by health or the first call.
            try:
                build_transport(instance.transport).close()
            except (TypeError, ValueError) as exc:
                raise PluginConfigError(
                    f"plugin instance {instance.id} transport is invalid"
                ) from exc
            return
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
        instances = {item.id: item for item in directory.plugin_instances}
        for source in directory.sources:
            if not source.enabled:
                continue
            instance = instances[source.plugin_instance_id]
            if not instance.enabled:
                raise PluginConfigError(
                    f"enabled source {source.id} uses disabled plugin instance "
                    f"{instance.id}"
                )
            if instance.transport.type != "driver":
                continue
            capabilities = set(self.manifest(instance.plugin_id).capabilities)
            required = source.capabilities or [source.kind]
            if not _manifest_supports_source(source.kind, required, capabilities):
                detail = (
                    next(iter(required))
                    if len(required) == 1
                    else str(sorted(required))
                )
                raise PluginConfigError(
                    f"plugin {instance.plugin_id} does not support {detail} "
                    f"source {source.id}"
                )

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
        if instance.transport.type != "driver":
            with self._instance_lock:
                config = instance.transport.model_dump(mode="json")
                credentials = {
                    "username": instance.username,
                    "password": instance.password,
                    "token": instance.token,
                }
                fingerprint = hashlib.sha256(
                    json.dumps(
                        {
                            "plugin_id": instance.plugin_id,
                            "transport": config,
                            # The digest lets credential rotation replace a
                            # live client without persisting the secret.
                            "credentials": credentials,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                if self._instance_fingerprints.get(instance.id) != fingerprint:
                    old = self._instances.pop(instance.id, None)
                    self._instance_fingerprints.pop(instance.id, None)
                    if old is not None and callable(getattr(old, "close", None)):
                        old.close()
                    try:
                        transport = build_transport(
                            instance.transport, credentials=credentials
                        )
                    except (TypeError, ValueError) as exc:
                        raise PluginConfigError(
                            f"plugin instance {instance.id} transport is invalid"
                        ) from exc
                    self._instances[instance.id] = transport
                    self._instance_fingerprints[instance.id] = fingerprint
                return self._instances[instance.id]
        registration = self._registrations.get(instance.plugin_id)
        if registration is None:
            raise PluginConfigError(f"plugin is not installed: {instance.plugin_id}")
        with self._instance_lock:
            config = self._runtime_config(instance)
            fingerprint_payload = {
                "plugin_id": instance.plugin_id,
                "implementation_version": registration.manifest.implementation_version,
                "config": config,
            }
            fingerprint = hashlib.sha256(
                json.dumps(
                    fingerprint_payload,
                    sort_keys=True,
                    default=str,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            if self._instance_fingerprints.get(instance.id) != fingerprint:
                old = self._instances.pop(instance.id, None)
                self._instance_fingerprints.pop(instance.id, None)
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
            manifest = self.manifest_for_instance(instance)
            checker = getattr(plugin, "check_health", None)
            if not callable(checker):
                return PluginHealth(
                    status="degraded",
                    detail="plugin does not advertise a health check",
                    plugin_id=instance.plugin_id,
                    implementation_version=manifest.implementation_version,
                )
            if inspect.iscoroutinefunction(checker):
                result = checker()
            else:
                result = await asyncio.to_thread(checker)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, PluginHealth):
                health = result.model_copy(
                    update={
                        # Identity comes from the installed registration, not
                        # from connector-controlled health output.
                        "plugin_id": instance.plugin_id,
                        "implementation_version": manifest.implementation_version,
                    },
                )
            else:
                health = PluginHealth.model_validate(
                    {
                        **dict(result),
                        "plugin_id": instance.plugin_id,
                        "implementation_version": manifest.implementation_version,
                    }
                )
            detail = str(sanitize_data(health.detail)).strip()
            return health.model_copy(
                update={"detail": detail[:1_000] or "plugin health check completed"}
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
        if inspect.iscoroutinefunction(executor):
            result = executor(operation, source_config, request, context)
        else:
            result = await asyncio.to_thread(
                executor, operation, source_config, request, context
            )
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ToolResult):
            return result
        return ToolResult.model_validate(result)

    def close(self) -> None:
        with self._instance_lock:
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
        inspect.signature(validator).bind(instance_config, source_config)
    except TypeError:
        result = validator(instance_config)
    except (ValueError, AttributeError):
        result = validator(instance_config, source_config)
    else:
        result = validator(instance_config, source_config)
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
        self,
        repository: EnvironmentRepository,
        plugins: PluginManager,
        capabilities: CapabilityRegistry | None = None,
    ) -> None:
        self.repository = repository
        self.plugins = plugins
        self.capabilities = capabilities or CapabilityRegistry.default()
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
        rejection, reservation_id, budget = self._reserve_for_runtime(runtime)
        if rejection is not None:
            return _tool_result(
                ToolResult(
                    status=ToolResultStatus.REJECTED,
                    warnings=[rejection],
                )
            )
        try:
            result = ToolResult(
                status=ToolResultStatus.SUCCEEDED,
                structured_result=snapshot.public_view(),
            )
            return _tool_result(
                self._bound_plugin_result(
                    result,
                    min(
                        max(
                            1_024,
                            int(getattr(runtime, "tool_result_bytes", 65_536)),
                        ),
                        1_048_576,
                    ),
                )
            )
        finally:
            self._release_for_runtime(runtime, reservation_id, budget)

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
        rejection, reservation_id, budget = self._reserve_for_runtime(runtime)
        if rejection is not None:
            return _tool_result(
                ToolResult(
                    status=ToolResultStatus.REJECTED,
                    warnings=[rejection],
                )
            )
        started = time.monotonic()
        release_deferred = False
        execution_task: asyncio.Task[Any] | None = None
        try:
            spec = self.capabilities.for_operation(operation)
            if spec is None or operation == "describe_environment":
                return _tool_result(
                    ToolResult(
                        status=ToolResultStatus.REJECTED,
                        warnings=["unknown_tool_operation"],
                    )
                )
            source_id = request.get("source_id")
            source = snapshot.source(str(source_id)) if source_id else None
            if source is None or not spec.accepts_source_kind(source.kind):
                return _tool_result(
                    ToolResult(
                        status=ToolResultStatus.REJECTED,
                        warnings=["source_not_allowed"],
                    )
                )
            allowed_capabilities = set(source.capabilities)
            if source.capabilities and not (
                "*" in allowed_capabilities
                or spec.id in allowed_capabilities
                or operation in allowed_capabilities
                or source.kind in allowed_capabilities
            ):
                return _tool_result(
                    ToolResult(
                        status=ToolResultStatus.REJECTED,
                        warnings=["capability_not_allowed"],
                    )
                )
            if spec.effect != "read_only":
                return _tool_result(
                    ToolResult(
                        status=ToolResultStatus.REJECTED,
                        warnings=["capability_requires_approval"],
                    )
                )
            if operation == "search_logs":
                filter_error = self._validate_log_filters(snapshot, source, request)
                if filter_error is not None:
                    return _tool_result(
                        ToolResult(
                            status=ToolResultStatus.REJECTED,
                            warnings=[filter_error],
                        )
                    )
            elif operation == "search_knowledge":
                request_error = self._validate_knowledge_request(request)
                if request_error is not None:
                    return _tool_result(
                        ToolResult(
                            status=ToolResultStatus.REJECTED,
                            warnings=[request_error],
                        )
                    )
            elif operation == "search_traffic":
                filter_error = self._validate_log_filters(snapshot, source, request)
                request_error = filter_error or self._validate_traffic_request(request)
                if request_error is not None:
                    return _tool_result(
                        ToolResult(
                            status=ToolResultStatus.REJECTED,
                            warnings=[request_error],
                        )
                    )
            try:
                instance = self._current_instance(snapshot, source)
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
                execution_task = asyncio.create_task(
                    self.plugins.execute(
                        instance, operation, source.config, call_request, context
                    )
                )
                try:
                    # Shield the connector task so a core timeout does not
                    # cancel a sync plugin's worker thread.  Its budget slot
                    # stays occupied until that task really finishes.
                    result = await asyncio.wait_for(
                        asyncio.shield(execution_task),
                        timeout=max(0.1, context.remaining_seconds),
                    )
                except asyncio.TimeoutError:
                    release_deferred = True
                    execution_task.add_done_callback(
                        lambda task: self._release_after_task(
                            task, runtime, reservation_id, budget
                        )
                    )
                    result = ToolResult(
                        status=ToolResultStatus.UNAVAILABLE,
                        warnings=["tool_timeout"],
                    )
                except asyncio.CancelledError:
                    if not execution_task.done():
                        release_deferred = True
                        execution_task.add_done_callback(
                            lambda task: self._release_after_task(
                                task, runtime, reservation_id, budget
                            )
                        )
                    raise
            except TransportTimeout:
                result = ToolResult(
                    status=ToolResultStatus.UNAVAILABLE,
                    warnings=["tool_timeout"],
                )
            except (PluginError, TransportError):
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
            result = self._bound_plugin_result(result, max_bytes)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if result.elapsed_ms is None:
                result = result.model_copy(update={"elapsed_ms": elapsed_ms})
            return _tool_result(
                result,
                evidence=self._evidence_for(result, source, operation, elapsed_ms),
            )
        finally:
            if not release_deferred:
                self._release_for_runtime(runtime, reservation_id, budget)

    @staticmethod
    def _release_after_task(
        task: asyncio.Task[Any],
        runtime: Any,
        reservation_id: str | None,
        budget: _RunBudget | None,
    ) -> None:
        """Consume a late connector outcome and release its held slot."""
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
        try:
            EnvironmentToolService._release_for_runtime(runtime, reservation_id, budget)
        except Exception:
            # A process may close its store while a timed-out worker is winding
            # down. Durable reservations have an expiry and recovery also
            # releases them, so cleanup failure must not surface as a task error.
            pass

    @staticmethod
    def _bound_plugin_result(result: ToolResult, max_bytes: int) -> ToolResult:
        # Normalize every plugin-controlled field before the result is used for
        # Evidence, audit, or state persistence.  Sanitizing only the payload
        # would still allow a faulty connector to put a credential in a
        # warning, cursor, or source locator.
        try:
            result = ToolResult.model_validate(
                sanitize_data(result.model_dump(mode="json"))
            )
        except (TypeError, ValueError):
            return ToolResult(
                status=ToolResultStatus.UNAVAILABLE,
                warnings=["plugin_result_invalid"],
            )
        if result.structured_result is None:
            return result
        clean = sanitize_data(result.structured_result)
        try:
            encoded = json.dumps(
                clean,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            return ToolResult(
                status=ToolResultStatus.UNAVAILABLE,
                warnings=["plugin_result_invalid"],
            )
        if len(encoded) <= max_bytes:
            status = (
                ToolResultStatus.PARTIAL
                if result.truncated and result.status == ToolResultStatus.SUCCEEDED
                else result.status
            )
            return result.model_copy(
                update={"status": status, "structured_result": clean}
            )
        return result.model_copy(
            update={
                "status": (
                    ToolResultStatus.PARTIAL
                    if result.status
                    in {ToolResultStatus.SUCCEEDED, ToolResultStatus.PARTIAL}
                    else result.status
                ),
                "structured_result": {
                    "omitted": "plugin result exceeded the core byte limit",
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "bytes": len(encoded),
                },
                "truncated": True,
                "warnings": [*result.warnings[:49], "core_result_limit"],
            }
        )

    def _reserve_for_runtime(
        self, runtime: Any
    ) -> tuple[str | None, str | None, _RunBudget | None]:
        observer = getattr(runtime, "execution_observer", None)
        persistent_reserve = getattr(observer, "reserve_tool_budget", None)
        max_calls = max(0, int(getattr(runtime, "tool_max_calls", 20)))
        max_concurrent = max(1, int(getattr(runtime, "tool_max_concurrent", 2)))
        if callable(persistent_reserve):
            reservation_id, rejection = persistent_reserve(
                max_calls=max_calls,
                max_concurrent=max_concurrent,
                ttl_seconds=max(
                    5.0,
                    float(getattr(runtime, "tool_timeout_seconds", 10)) + 5.0,
                ),
            )
            return rejection, reservation_id, None
        budget = self._budget(runtime)
        return self._reserve(budget), None, budget

    @staticmethod
    def _release_for_runtime(
        runtime: Any,
        reservation_id: str | None,
        budget: _RunBudget | None,
    ) -> None:
        if reservation_id is not None:
            observer = getattr(runtime, "execution_observer", None)
            release = getattr(observer, "release_tool_budget", None)
            if callable(release):
                release(reservation_id)
                return
        if budget is not None:
            EnvironmentToolService._release(budget)

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

    @staticmethod
    def _validate_log_filters(
        snapshot: ResolvedEnvironmentSnapshot,
        source: SnapshotSource,
        request: dict[str, Any],
    ) -> str | None:
        requested_services, service_error = EnvironmentToolService._filter_ids(
            request.get("service_ids", [])
        )
        requested_nodes, node_error = EnvironmentToolService._filter_ids(
            request.get("node_ids", [])
        )
        if service_error:
            return "service_filter_invalid"
        if node_error:
            return "node_filter_invalid"
        snapshot_services = {item.id for item in snapshot.services}
        snapshot_nodes = {item.id for item in snapshot.nodes}
        allowed_services = set(source.service_ids) or snapshot_services
        allowed_nodes = set(source.node_ids) or snapshot_nodes
        if not requested_services.issubset(allowed_services & snapshot_services):
            return "service_not_allowed"
        if not requested_nodes.issubset(allowed_nodes & snapshot_nodes):
            return "node_not_allowed"
        return None

    @staticmethod
    def _validate_knowledge_request(request: dict[str, Any]) -> str | None:
        query = request.get("query")
        if not isinstance(query, str) or not query.strip():
            return "knowledge_query_required"
        if len(query) > 4_096:
            return "knowledge_query_too_large"
        top_k = request.get("top_k", 10)
        if (
            not isinstance(top_k, int)
            or isinstance(top_k, bool)
            or not 1 <= top_k <= 100
        ):
            return "knowledge_top_k_invalid"
        filters = request.get("filters")
        if filters is not None and not isinstance(filters, dict):
            return "knowledge_filters_invalid"
        if filters is not None:
            try:
                encoded = json.dumps(
                    filters, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                )
            except (TypeError, ValueError):
                return "knowledge_filters_invalid"
            if len(encoded.encode("utf-8")) > 16_384:
                return "knowledge_filters_too_large"
        return None

    @staticmethod
    def _validate_traffic_request(request: dict[str, Any]) -> str | None:
        start = request.get("start_time")
        end = request.get("end_time")
        if not isinstance(start, str) or not isinstance(end, str):
            return "absolute_time_required"
        try:
            start_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_at = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError:
            return "absolute_time_required"
        if start_at.tzinfo is None or end_at.tzinfo is None:
            return "absolute_time_required"
        if end_at <= start_at:
            return "invalid_time_window"
        if (end_at - start_at).total_seconds() > 24 * 60 * 60:
            return "time_window_too_large"
        text_query = request.get("text_query", "")
        if text_query is not None and (
            not isinstance(text_query, str) or len(text_query) > 4_096
        ):
            return "traffic_query_too_large"
        for field_name in ("service_ids", "node_ids", "correlation_ids"):
            value = request.get(field_name, [])
            if value is not None and (
                not isinstance(value, list)
                or len(value) > 100
                or any(not isinstance(item, str) or len(item) > 256 for item in value)
            ):
                return f"{field_name.removesuffix('_ids')}_filter_invalid"
        return None

    @staticmethod
    def _filter_ids(value: Any) -> tuple[set[str], bool]:
        if value is None:
            return set(), False
        if not isinstance(value, list) or len(value) > 100:
            return set(), True
        if any(not isinstance(item, str) for item in value):
            return set(), True
        return {item for item in value if item}, False

    def _current_instance(
        self,
        snapshot: ResolvedEnvironmentSnapshot,
        source: SnapshotSource,
    ) -> PluginInstanceConfig:
        pinned: SnapshotPluginInstance | None = snapshot.plugin_instance(
            source.plugin_instance_id
        )
        if pinned is None or pinned.plugin_id != source.plugin_id:
            # Snapshots created before plugin-instance pinning intentionally do
            # not gain access after an upgrade; that would silently change an
            # old run's connection and limits.
            raise PluginConfigError("plugin instance is absent from the snapshot")
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
        return PluginInstanceConfig(
            id=pinned.id,
            plugin_id=pinned.plugin_id,
            enabled=True,
            config=copy.deepcopy(pinned.config),
            transport=pinned.transport,
            username=current.username,
            password=current.password,
            token=current.token,
            default_limits=pinned.default_limits,
        )

    def audit_metadata(
        self, tool_name: str, runtime: Any, arguments: dict[str, Any] | Any
    ) -> dict[str, Any]:
        """Return safe, stable plugin fields for the tool audit ledger."""
        operation_by_tool = {
            "describe_database": "describe_database",
            "query_database": "query_database",
            "search_logs": "search_logs",
            "search_knowledge": "search_knowledge",
            "search_traffic": "search_traffic",
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
        instance = (
            snapshot.plugin_instance(source.plugin_instance_id) if snapshot else None
        )
        manifest = (
            self.plugins.manifest_for_instance(
                PluginInstanceConfig(
                    id=source.plugin_instance_id,
                    plugin_id=source.plugin_id,
                    transport=instance.transport if instance else TransportConfig(),
                )
            )
            if instance is not None
            else self.plugins.manifest(source.plugin_id)
        )
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
        elif operation in {"search_knowledge", "search_traffic"}:
            query_key = "query" if operation == "search_knowledge" else "text_query"
            query = arguments.get(query_key)
            if isinstance(query, str) and query:
                metadata["query_fingerprint"] = hashlib.sha256(
                    query.strip().encode("utf-8")
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
    """Expose stable read-only capability tools to InvestigateAgent."""

    def __init__(
        self,
        service: EnvironmentToolService,
        capabilities: CapabilityRegistry | None = None,
    ) -> None:
        super().__init__(
            enabled=True,
            allowed_nodes={"investigate"},
            max_results=6,
            max_result_bytes=65_536,
            timeout_seconds=30,
        )
        self.service = service
        self.capabilities = capabilities or service.capabilities
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
        self.register(
            self._search_knowledge,
            name="search_knowledge",
            version="environment-tools-v1",
            nodes={"investigate"},
            strict_mode=False,
        )
        self.register(
            self._search_traffic,
            name="search_traffic",
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

    async def _search_knowledge(
        self,
        context: RunContextWrapper[Any],
        source_id: str,
        query: str,
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return await self.service.call(
            "search_knowledge",
            context.context,
            {
                "source_id": source_id,
                "query": query,
                "top_k": top_k,
                "filters": filters or {},
                "cursor": cursor,
            },
        )

    async def _search_traffic(
        self,
        context: RunContextWrapper[Any],
        source_id: str,
        start_time: str,
        end_time: str,
        text_query: str = "",
        service_ids: list[str] | None = None,
        node_ids: list[str] | None = None,
        correlation_ids: list[str] | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return await self.service.call(
            "search_traffic",
            context.context,
            {
                "source_id": source_id,
                "start_time": start_time,
                "end_time": end_time,
                "text_query": text_query,
                "service_ids": service_ids or [],
                "node_ids": node_ids or [],
                "correlation_ids": correlation_ids or [],
                "cursor": cursor,
            },
        )


def build_plugin_runtime(
    repository: EnvironmentRepository,
    manager: PluginManager | None = None,
    capabilities: CapabilityRegistry | None = None,
) -> tuple[PluginManager, EnvironmentToolService, EnvironmentToolRegistry]:
    manager = manager or PluginManager.discover()
    if repository.configured:
        manager.validate_directory(repository.directory())
    registry = capabilities or CapabilityRegistry.default()
    service = EnvironmentToolService(repository, manager, registry)
    return manager, service, EnvironmentToolRegistry(service, registry)


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
