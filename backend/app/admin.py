"""Read-only administration views and controlled configuration mutations.

The admin service is intentionally a thin control-plane layer.  It reads persisted
``DiagnosisState`` and SDK session metadata, while all diagnosis execution continues
through :class:`ApplicationService` and :class:`DiagnosisRuntime`.
"""

from __future__ import annotations

import copy
import importlib.metadata
import os
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field

from .application import ApplicationService
from .config import ConfigNotWritableError, ConfigRevisionConflictError
from .environment import (
    EnvironmentConfigError,
    EnvironmentConfigNotWritableError,
    EnvironmentRepository,
    EnvironmentRevisionConflictError,
)
from .models import StrictModel
from .security import sanitize_data

AdminStatus = Literal["ok", "degraded", "error"]


class HealthComponent(StrictModel):
    name: str
    status: AdminStatus
    detail: str
    checked_at: datetime


class HealthView(StrictModel):
    status: AdminStatus
    checked_at: datetime
    components: list[HealthComponent]


class VersionView(StrictModel):
    version: str
    protocol_version: str = "2"
    api_prefix: str = "/v1"


class CapabilitiesView(StrictModel):
    protocol_version: str = "2"
    features: dict[str, bool]


class ConfigView(StrictModel):
    revision: str
    writable: bool
    active_profile: str
    profiles: dict[str, dict[str, Any]]


class ConfigMutation(StrictModel):
    profile: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    config: dict[str, Any]
    expected_revision: str = Field(min_length=8, max_length=128)


class ConfigValidationView(StrictModel):
    valid: bool
    profile: str
    revision: str
    config_version: str | None = None
    snapshot_id: str | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class EnvironmentSummaryView(StrictModel):
    environment_id: str
    display_name: str
    aliases: list[str]
    level: str
    region: str | None = None
    timezone: str
    tags: dict[str, str]


class EnvironmentListView(StrictModel):
    revision: str
    items: list[EnvironmentSummaryView]


class PluginView(StrictModel):
    plugin_id: str
    implementation_version: str
    api_major: int
    capabilities: list[str]
    health_check: bool
    instance_ids: list[str] = Field(default_factory=list)
    instance_status: dict[str, str] = Field(default_factory=dict)
    instance_config_schema: dict[str, Any] = Field(default_factory=dict)
    source_config_schema: dict[str, Any] = Field(default_factory=dict)


class PluginListView(StrictModel):
    items: list[PluginView]


class EnvironmentConfigView(StrictModel):
    revision: str
    writable: bool
    config: dict[str, Any]


class EnvironmentConfigMutation(StrictModel):
    config: dict[str, Any]
    expected_revision: str = Field(min_length=8, max_length=128)
    secret_updates: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)


class PluginHealthView(StrictModel):
    instance_id: str
    plugin_id: str | None = None
    status: AdminStatus
    detail: str
    checked_at: datetime


class RunListItem(StrictModel):
    run_id: str
    question: str
    lifecycle_status: str
    status: str
    outcome: str | None = None
    current_node: str
    profile: str
    config_version: str
    config_snapshot_id: str
    attempt: int
    clarification_round: int
    clarification_rounds: dict[str, int]
    retry_cycle: int
    retry_index: int
    resume_available: bool
    active_execution_id: str | None = None
    available_actions: list[str]
    cancel_requested: bool = False
    pending_input: bool
    pending_approval: bool = False
    environment_id: str | None = None
    environment_snapshot_id: str | None = None
    pending_target_confirmation: bool = False
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None


class RunListView(StrictModel):
    items: list[RunListItem]
    total: int
    offset: int
    limit: int
    degraded_count: int = 0


class SessionView(StrictModel):
    session_id: str
    run_id: str
    node: str | None = None
    status: str
    created_at: str
    updated_at: str
    message_count: int
    config_snapshot_id: str | None = None


class SessionListView(StrictModel):
    items: list[SessionView]
    total: int
    offset: int
    limit: int
    degraded_count: int = 0


class NodeExecutionView(StrictModel):
    execution_id: str
    run_id: str
    node: str
    session_id: str
    investigation_attempt: int
    clarification_round: int
    retry_cycle: int
    retry_index: int
    status: str
    input_context_json: str | None = None
    input_hash: str | None = None
    output_json: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool
    reasoning_summary_json: str | None = None
    trace_id: str | None = None
    config_snapshot_id: str
    agent_definition_version: str
    started_at: str
    completed_at: str | None = None


class ToolExecutionView(StrictModel):
    tool_execution_id: str
    node_execution_id: str
    run_id: str
    sdk_tool_call_id: str
    tool_name: str
    tool_version: str
    retry_index: int
    arguments_json: str | None = None
    arguments_hash: str | None = None
    status: str
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool
    result_summary_json: str | None = None
    evidence_ids_json: str
    plugin_id: str | None = None
    plugin_implementation_version: str | None = None
    plugin_instance_id: str | None = None
    environment_snapshot_id: str | None = None
    source_id: str | None = None
    operation: str | None = None
    redacted_query: str | None = None
    query_fingerprint: str | None = None
    truncated: bool = False
    started_at: str
    completed_at: str | None = None
    duration_ms: int | None = None


class BootstrapStatus(StrictModel):
    initialized: bool
    setup_required: bool
    version: VersionView
    health: HealthView


def _mask_api_key(key: str | None) -> str | None:
    """Mask an api_key for safe display: keep the prefix and last 4 chars."""
    if not key:
        return key
    if len(key) <= 8:
        return "****"
    return f"{key[:3]}****{key[-4:]}"


class AdminApplicationService:
    """Control-plane operations used by the HTTP adapter and admin UI."""

    def __init__(self, service: ApplicationService) -> None:
        self.service = service

    def version(self) -> VersionView:
        try:
            value = importlib.metadata.version("buglens")
        except importlib.metadata.PackageNotFoundError:
            value = "0.1.0"
        return VersionView(version=value)

    def capabilities(self) -> CapabilitiesView:
        return CapabilitiesView(
            features={
                "diagnosis_runs": True,
                "server_sent_events": True,
                "admin_health": True,
                "admin_config": True,
                "environment_catalog": self._environments() is not None,
                "plugin_discovery": self._environment_tools() is not None,
                "target_confirmation": True,
                "admin_sessions": True,
                "skip_input": True,
                "resume": True,
                "tool_approval": True,
                "collaborative_cancel": True,
                "execution_audit": True,
                "one_click_update": False,
            }
        )

    def health(self) -> HealthView:
        checked_at = datetime.now(UTC)
        components: list[HealthComponent] = []
        store = self.service.runtime.store
        try:
            store.db.execute("SELECT 1").fetchone()
            components.append(
                HealthComponent(
                    name="checkpoint_store",
                    status="ok",
                    detail="SQLite checkpoint store is reachable",
                    checked_at=checked_at,
                )
            )
        except Exception:
            components.append(
                HealthComponent(
                    name="checkpoint_store",
                    status="error",
                    detail="SQLite checkpoint store is unavailable",
                    checked_at=checked_at,
                )
            )
        try:
            self.service.configs.resolve("default")
            components.append(
                HealthComponent(
                    name="configuration",
                    status="ok",
                    detail="default profile is valid",
                    checked_at=checked_at,
                )
            )
        except Exception:
            components.append(
                HealthComponent(
                    name="configuration",
                    status="error",
                    detail="default profile is invalid",
                    checked_at=checked_at,
                )
            )
        key_configured = bool(os.getenv("OPENAI_API_KEY")) or bool(
            os.getenv("OPENAI_BASE_URL")
        )
        if not key_configured:
            try:
                resolved = self.service.configs.resolve("default")
                key_configured = any(
                    mc.api_key or mc.base_url for mc in resolved.policy.models.values()
                )
            except Exception:
                pass
        components.append(
            HealthComponent(
                name="model_credentials",
                status="ok" if key_configured else "degraded",
                detail=(
                    "model credentials are configured"
                    if key_configured
                    else "no model credentials; set OPENAI_API_KEY/OPENAI_BASE_URL "
                    "or define api_key/base_url on a model in the config profile"
                ),
                checked_at=checked_at,
            )
        )
        statuses = {component.status for component in components}
        overall: AdminStatus = (
            "error"
            if "error" in statuses
            else ("degraded" if "degraded" in statuses else "ok")
        )
        return HealthView(status=overall, checked_at=checked_at, components=components)

    def bootstrap_status(self) -> BootstrapStatus:
        health = self.health()
        initialized = bool(self.service.configs.profiles())
        setup_required = health.status == "error" or any(
            c.name == "model_credentials" and c.status != "ok"
            for c in health.components
        )
        return BootstrapStatus(
            initialized=initialized,
            setup_required=setup_required,
            version=self.version(),
            health=health,
        )

    def config(self) -> ConfigView:
        profiles = self.service.configs.profiles()
        # Mask api_key in the models registry before exposing via the admin API.
        for profile_cfg in profiles.values():
            models = (
                profile_cfg.get("models") if isinstance(profile_cfg, dict) else None
            )
            if isinstance(models, dict):
                for model_cfg in models.values():
                    if isinstance(model_cfg, dict) and model_cfg.get("api_key"):
                        model_cfg["api_key"] = _mask_api_key(model_cfg["api_key"])
        return ConfigView(
            revision=self.service.configs.revision,
            writable=self.service.configs.writable,
            active_profile="default",
            profiles=profiles,
        )

    def validate_config(self, mutation: ConfigMutation) -> ConfigValidationView:
        try:
            resolved = self.service.configs.validate_profile(
                mutation.profile, mutation.config
            )
        except Exception as exc:
            return ConfigValidationView(
                valid=False,
                profile=mutation.profile,
                revision=self.service.configs.revision,
                errors=[str(sanitize_data(str(exc)))],
            )
        warnings: list[str] = []
        if not self.service.configs.writable:
            warnings.append("当前配置来自内置默认值，保存前需要设置 BUGLENS_CONFIG")
        return ConfigValidationView(
            valid=True,
            profile=mutation.profile,
            revision=self.service.configs.revision,
            config_version=resolved.config_version,
            snapshot_id=resolved.snapshot_id,
            warnings=warnings,
        )

    def _environments(self) -> EnvironmentRepository | None:
        return getattr(self.service, "environments", None)

    def _environment_tools(self):
        return getattr(self.service.runtime, "environment_tools", None)

    def environment_list(self) -> EnvironmentListView:
        repository = self._environments()
        if repository is None:
            return EnvironmentListView(revision="", items=[])
        return EnvironmentListView(
            revision=repository.revision,
            items=[
                EnvironmentSummaryView(
                    environment_id=item.id,
                    display_name=item.display_name,
                    aliases=item.aliases,
                    level=item.level,
                    region=item.region,
                    timezone=item.timezone,
                    tags=item.tags,
                )
                for item in repository.environments()
            ],
        )

    def environment_config(self) -> EnvironmentConfigView:
        repository = self._environments()
        if repository is None:
            return EnvironmentConfigView(revision="", writable=False, config={})
        return EnvironmentConfigView(
            revision=repository.revision,
            writable=repository.writable,
            config=repository.public_config(),
        )

    def plugins(self) -> PluginListView:
        repository = self._environments()
        runtime_tools = self._environment_tools()
        manager = getattr(runtime_tools, "plugins", None)
        if manager is None:
            return PluginListView(items=[])
        instances = repository.directory().plugin_instances if repository else []
        by_plugin: dict[str, list[str]] = {}
        statuses: dict[str, dict[str, str]] = {}
        for instance in instances:
            by_plugin.setdefault(instance.plugin_id, []).append(instance.id)
            statuses.setdefault(instance.plugin_id, {})[instance.id] = (
                "enabled" if instance.enabled else "disabled"
            )
        manifests = list(manager.manifests())
        known_plugin_ids = {manifest.plugin_id for manifest in manifests}
        # Declarative MCP/CLI/SSH instances have no entry point to discover,
        # but should still appear in the admin directory and support the same
        # connection check UX as installed driver plugins.
        manifest_for_instance = getattr(manager, "manifest_for_instance", None)
        if callable(manifest_for_instance):
            for instance in instances:
                if (
                    instance.transport.type == "driver"
                    or instance.plugin_id in known_plugin_ids
                ):
                    continue
                try:
                    manifest = manifest_for_instance(instance)
                except Exception:
                    continue
                manifests.append(manifest)
                known_plugin_ids.add(manifest.plugin_id)
        result: list[PluginView] = []
        for manifest in manifests:
            result.append(
                PluginView(
                    plugin_id=manifest.plugin_id,
                    implementation_version=manifest.implementation_version,
                    api_major=manifest.api_major,
                    capabilities=manifest.capabilities,
                    health_check=manifest.health_check,
                    instance_ids=by_plugin.get(manifest.plugin_id, []),
                    instance_status=statuses.get(manifest.plugin_id, {}),
                    instance_config_schema=manifest.instance_config_schema,
                    source_config_schema=manifest.source_config_schema,
                )
            )
        return PluginListView(items=result)

    def validate_environment_config(
        self, mutation: EnvironmentConfigMutation
    ) -> ConfigValidationView:
        repository = self._environments()
        runtime_tools = self._environment_tools()
        if repository is None:
            return ConfigValidationView(
                valid=False,
                profile="environments",
                revision="",
                errors=["environment repository is unavailable"],
            )
        if mutation.expected_revision != repository.revision:
            return ConfigValidationView(
                valid=False,
                profile="environments",
                revision=repository.revision,
                errors=["environment config revision changed"],
            )
        try:
            directory = repository.prepare(
                mutation.config,
                mutation.expected_revision,
                secret_updates=mutation.secret_updates,
            )
            validator = getattr(
                getattr(runtime_tools, "plugins", None), "validate_directory", None
            )
            if callable(validator):
                validator(directory)
        except Exception as exc:
            return ConfigValidationView(
                valid=False,
                profile="environments",
                revision=repository.revision,
                errors=[str(sanitize_data(str(exc)))],
            )
        return ConfigValidationView(
            valid=True,
            profile="environments",
            revision=repository.revision,
            config_version=directory.config_version,
        )

    def apply_environment_config(
        self, mutation: EnvironmentConfigMutation
    ) -> EnvironmentConfigView:
        repository = self._environments()
        if repository is None:
            raise EnvironmentConfigNotWritableError(
                "environment repository is unavailable"
            )
        validation = self.validate_environment_config(mutation)
        if not validation.valid:
            if mutation.expected_revision != repository.revision:
                raise EnvironmentRevisionConflictError(
                    "environment config revision changed"
                )
            raise EnvironmentConfigError(
                validation.errors[0]
                if validation.errors
                else "invalid environment config"
            )
        repository.apply(
            mutation.config,
            mutation.expected_revision,
            secret_updates=mutation.secret_updates,
        )
        return self.environment_config()

    async def check_plugin_instance(self, instance_id: str) -> PluginHealthView:
        repository = self._environments()
        runtime_tools = self._environment_tools()
        if repository is None or runtime_tools is None:
            return PluginHealthView(
                instance_id=instance_id,
                status="error",
                detail="environment plugin runtime is unavailable",
                checked_at=datetime.now(UTC),
            )
        checker = getattr(runtime_tools, "check_instance", None)
        if not callable(checker):
            return PluginHealthView(
                instance_id=instance_id,
                status="error",
                detail="plugin health checks are unavailable",
                checked_at=datetime.now(UTC),
            )
        health = await checker(instance_id)
        instance = next(
            (
                item
                for item in repository.directory().plugin_instances
                if item.id == instance_id
            ),
            None,
        )
        return PluginHealthView(
            instance_id=instance_id,
            plugin_id=instance.plugin_id if instance else health.plugin_id,
            status=health.status,
            detail=health.detail,
            checked_at=health.checked_at,
        )

    def apply_config(self, mutation: ConfigMutation) -> ConfigView:
        config = copy.deepcopy(mutation.config)
        # A masked or empty api_key means "keep the existing value"; only an
        # explicitly typed new key is persisted. This lets the admin UI show keys
        # masked without forcing re-entry on every save.
        existing = self.service.configs.profiles().get(mutation.profile, {})
        existing_models = existing.get("models") if isinstance(existing, dict) else {}
        existing_models = existing_models if isinstance(existing_models, dict) else {}
        models = config.get("models") if isinstance(config.get("models"), dict) else {}
        for name, model_cfg in models.items():
            if not isinstance(model_cfg, dict):
                continue
            submitted = model_cfg.get("api_key")
            if not submitted or "****" in str(submitted):
                orig = existing_models.get(name)
                if isinstance(orig, dict):
                    model_cfg["api_key"] = orig.get("api_key")
        self.service.configs.apply_profile(
            mutation.profile, config, mutation.expected_revision
        )
        return self.config()

    def runs(
        self,
        *,
        lifecycle_status: str | None = None,
        outcome: str | None = None,
        current_node: str | None = None,
        profile: str | None = None,
        query: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> RunListView:
        states, total = self.service.runtime.store.list_states(
            lifecycle_status=lifecycle_status,
            outcome=outcome,
            current_node=current_node,
            profile=profile,
            query=query,
            offset=offset,
            limit=limit,
        )
        return RunListView(
            items=[
                RunListItem(
                    run_id=state.run_id,
                    question=state.user_question,
                    lifecycle_status=state.lifecycle_status.value,
                    status=state.status,
                    outcome=state.outcome.value if state.outcome else None,
                    current_node=state.current_node.value,
                    profile=state.config_profile,
                    config_version=state.config_version,
                    config_snapshot_id=state.config_snapshot_id,
                    attempt=state.attempt,
                    clarification_round=state.clarification_round,
                    clarification_rounds=state.clarification_rounds,
                    retry_cycle=state.retry_cycle,
                    retry_index=state.retry_index,
                    resume_available=state.resume_available,
                    active_execution_id=state.active_execution_id,
                    available_actions=state.available_actions,
                    cancel_requested=state.cancel_requested_at is not None,
                    pending_input=state.pending_interaction is not None,
                    pending_approval=state.pending_approval is not None,
                    environment_id=state.target.environment_id,
                    environment_snapshot_id=state.environment_snapshot_id,
                    pending_target_confirmation=(
                        state.pending_target_confirmation is not None
                    ),
                    created_at=state.created_at,
                    updated_at=state.updated_at,
                    last_error=state.last_error.message if state.last_error else None,
                )
                for state in states
            ],
            total=total,
            offset=offset,
            limit=limit,
            degraded_count=self.service.runtime.store.last_list_degraded_count,
        )

    def sessions(
        self,
        *,
        run_id: str | None = None,
        node: str | None = None,
        status: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> SessionListView:
        records, total = self.service.runtime.store.list_sessions(
            run_id=run_id,
            node=node,
            status=status,
            offset=offset,
            limit=limit,
        )
        return SessionListView(
            items=[SessionView.model_validate(record) for record in records],
            total=total,
            offset=offset,
            limit=limit,
            degraded_count=self.service.runtime.store.last_list_degraded_count,
        )

    def node_executions(
        self,
        *,
        run_id: str | None = None,
        node: str | None = None,
        execution_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[NodeExecutionView]:
        return [
            NodeExecutionView.model_validate(record)
            for record in self.service.runtime.store.list_node_executions(
                run_id=run_id,
                node=node,
                execution_id=execution_id,
                status=status,
                limit=limit,
            )
        ]

    def tool_executions(
        self,
        *,
        run_id: str | None = None,
        node_execution_id: str | None = None,
        sdk_tool_call_id: str | None = None,
        limit: int = 100,
    ) -> list[ToolExecutionView]:
        return [
            ToolExecutionView.model_validate(record)
            for record in self.service.runtime.store.list_tool_executions(
                run_id=run_id,
                node_execution_id=node_execution_id,
                sdk_tool_call_id=sdk_tool_call_id,
                limit=limit,
            )
        ]


__all__ = [
    "AdminApplicationService",
    "BootstrapStatus",
    "CapabilitiesView",
    "ConfigMutation",
    "ConfigValidationView",
    "ConfigView",
    "ConfigNotWritableError",
    "ConfigRevisionConflictError",
    "EnvironmentConfigMutation",
    "EnvironmentConfigView",
    "EnvironmentListView",
    "EnvironmentSummaryView",
    "HealthView",
    "NodeExecutionView",
    "RunListView",
    "SessionListView",
    "ToolExecutionView",
    "PluginHealthView",
    "PluginListView",
    "PluginView",
    "VersionView",
]
