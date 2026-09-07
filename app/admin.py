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
from .models import StrictModel

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
    protocol_version: str = "1"
    api_prefix: str = "/v1"


class CapabilitiesView(StrictModel):
    protocol_version: str = "1"
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
    pending_input: bool
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None


class RunListView(StrictModel):
    items: list[RunListItem]
    total: int
    offset: int
    limit: int


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
                "admin_sessions": True,
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
                errors=[str(exc)],
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
                    pending_input=state.pending_interaction is not None,
                    created_at=state.created_at,
                    updated_at=state.updated_at,
                    last_error=state.last_error.message if state.last_error else None,
                )
                for state in states
            ],
            total=total,
            offset=offset,
            limit=limit,
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
        )


__all__ = [
    "AdminApplicationService",
    "BootstrapStatus",
    "CapabilitiesView",
    "ConfigMutation",
    "ConfigValidationView",
    "ConfigView",
    "ConfigNotWritableError",
    "ConfigRevisionConflictError",
    "HealthView",
    "RunListView",
    "SessionListView",
    "VersionView",
]
