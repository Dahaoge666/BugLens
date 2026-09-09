"""Pydantic protocol types shared by the core and external plugins."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

PLUGIN_API_MAJOR = 1

JSONScalar: TypeAlias = str | int | float | bool | None
# Pydantic 2.13 on Python 3.14 eagerly expands recursive aliases while it
# builds a model schema.  The wire protocol is JSON-only, but schemas and
# plugin payloads are intentionally open-ended, so ``Any`` is the portable
# representation here; callers still validate the JSON boundary.
JSONValue: TypeAlias = Any


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )


class ToolResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class SourceReference(_StrictModel):
    """A stable locator that lets a report point back to plugin evidence."""

    source_id: str = Field(min_length=1, max_length=256)
    locator: str = Field(min_length=1, max_length=1_024)
    title: str | None = Field(default=None, max_length=512)
    observed_at: str | None = Field(default=None, max_length=128)


class PluginManifest(_StrictModel):
    """Capabilities and configuration schemas advertised by one plugin."""

    plugin_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.-]{0,127}$",
    )
    implementation_version: str = Field(min_length=1, max_length=128)
    api_major: int = Field(default=PLUGIN_API_MAJOR, ge=1, le=10_000)
    # ``api_version`` is kept as a human-readable wire field in addition to the
    # numeric compatibility gate.  Both forms are accepted by external plugins.
    api_version: str = Field(default=str(PLUGIN_API_MAJOR), min_length=1, max_length=32)
    capabilities: list[str] = Field(default_factory=list, max_length=50)
    instance_config_schema: dict[str, JSONValue] = Field(
        default_factory=dict,
        json_schema_extra={"additionalProperties": False},
    )
    source_config_schema: dict[str, JSONValue] = Field(
        default_factory=dict,
        json_schema_extra={"additionalProperties": False},
    )
    health_check: bool = True

    @model_validator(mode="after")
    def validate_api_version(self) -> PluginManifest:
        try:
            major = int(self.api_version.split(".", 1)[0])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "api_version must start with a numeric major version"
            ) from exc
        if major != self.api_major:
            raise ValueError("api_major and api_version do not match")
        return self


class ExecutionContext(_StrictModel):
    """Bounded, credential-free context supplied for one plugin call."""

    execution_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    environment_snapshot_id: str = Field(min_length=1, max_length=128)
    plugin_instance_id: str = Field(min_length=1, max_length=128)
    deadline: datetime
    max_results: int = Field(default=200, ge=1, le=1_000)
    max_bytes: int = Field(default=65_536, ge=1_024, le=1_048_576)
    max_scan_files: int = Field(default=100, ge=1, le=10_000)
    max_scan_bytes: int = Field(default=16_777_216, ge=1_024, le=67_108_864)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, (self.deadline - datetime.now(UTC)).total_seconds())


class PluginHealth(_StrictModel):
    status: Literal["ok", "degraded", "error"]
    detail: str = Field(min_length=1, max_length=1_000)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    plugin_id: str | None = Field(default=None, max_length=128)
    implementation_version: str | None = Field(default=None, max_length=128)


class ToolResult(_StrictModel):
    """A bounded result returned by a deterministic read-only plugin."""

    status: ToolResultStatus
    structured_result: JSONValue | None = Field(default=None, alias="result")
    source_references: list[SourceReference] = Field(
        default_factory=list, max_length=100
    )
    cursor: str | None = Field(default=None, max_length=512)
    truncated: bool = False
    elapsed_ms: int | None = Field(default=None, ge=0)
    warnings: list[str] = Field(default_factory=list, max_length=50)


@runtime_checkable
class ToolPlugin(Protocol):
    """Runtime protocol implemented by an independently packaged plugin."""

    manifest: PluginManifest

    def validate_config(
        self,
        instance_config: dict[str, JSONValue],
        source_config: dict[str, JSONValue] | None = None,
    ) -> None: ...

    async def check_health(self) -> PluginHealth | dict[str, JSONValue]: ...

    async def execute(
        self,
        operation: str,
        source_config: dict[str, JSONValue],
        request: dict[str, JSONValue],
        context: ExecutionContext,
    ) -> ToolResult | dict[str, JSONValue]: ...

    def close(self) -> None: ...


__all__ = [
    "ExecutionContext",
    "JSONScalar",
    "JSONValue",
    "PLUGIN_API_MAJOR",
    "PluginHealth",
    "PluginManifest",
    "SourceReference",
    "ToolPlugin",
    "ToolResult",
    "ToolResultStatus",
]
