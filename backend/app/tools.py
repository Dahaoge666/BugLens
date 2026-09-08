"""Read-only SDK function-tool registry and audit observer interfaces."""

from __future__ import annotations

import copy
import inspect
import json
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal, Protocol
from uuid import uuid4

from agents import (
    RunHooks,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrail,
    ToolInputGuardrailData,
    ToolOutputGuardrail,
    ToolOutputGuardrailData,
    function_tool,
)
from agents.exceptions import ToolTimeoutError

from .models import EvidenceRecord
from .security import sanitize_data


class ToolAuditObserver(Protocol):
    def tool_started(self, **record: Any) -> None: ...

    def tool_finished(self, tool_execution_id: str, **updates: Any) -> None: ...

    def evidence_registered(self, evidence: EvidenceRecord) -> None: ...

    def reasoning_captured(self, summaries: list[str]) -> None: ...


class ToolExecutionError(RuntimeError):
    """Stable error marker for a retryable read-only connector failure."""

    code = "tool_execution_failed"
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        code: str = "tool_execution_failed",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


_TOOL_ARGUMENT_MAX_BYTES = 16_384


def _tool_input_guardrail(
    data: ToolInputGuardrailData,
) -> ToolGuardrailFunctionOutput:
    """Fail closed before a model-supplied tool call reaches a connector.

    FunctionTool already performs strict schema validation.  This guardrail is a
    second boundary for the values that arrive at the actual tool invocation:
    it bounds the raw JSON and verifies that a tool exposed through the
    BugLens registry is still allowed for this run.
    """
    context = data.context
    raw_arguments = getattr(context, "tool_arguments", None)
    if not isinstance(raw_arguments, str):
        return ToolGuardrailFunctionOutput.raise_exception(
            {"code": "tool_arguments_invalid"}
        )
    try:
        arguments = json.loads(raw_arguments)
    except (TypeError, ValueError):
        return ToolGuardrailFunctionOutput.raise_exception(
            {"code": "tool_arguments_invalid"}
        )
    if not isinstance(arguments, dict):
        return ToolGuardrailFunctionOutput.raise_exception(
            {"code": "tool_arguments_invalid"}
        )
    encoded = json.dumps(
        sanitize_data(arguments),
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > _TOOL_ARGUMENT_MAX_BYTES:
        return ToolGuardrailFunctionOutput.raise_exception(
            {"code": "tool_arguments_too_large"}
        )

    runtime = getattr(context, "context", None)
    registry = getattr(runtime, "tool_registry", None)
    if isinstance(registry, ToolRegistry):
        registration = registry.registration(getattr(context, "tool_name", ""))
        if registration is None or not registry.is_enabled(
            registration,
            node_name=getattr(runtime, "node_name", ""),
            profile=getattr(runtime, "profile", "default"),
            tenant_id=getattr(runtime, "tenant_id", None),
            capabilities=set(getattr(runtime, "capabilities", ())),
            enabled=getattr(runtime, "tools_enabled", False),
            allowed_nodes=set(getattr(runtime, "tool_allowed_nodes", ())),
            allowed_profiles=set(getattr(runtime, "tool_allowed_profiles", ())),
        ):
            return ToolGuardrailFunctionOutput.raise_exception(
                {"code": "tool_not_allowed"}
            )

    return ToolGuardrailFunctionOutput.allow(
        {
            "tool_name": getattr(context, "tool_name", "unknown"),
            "arguments_bytes": len(encoded.encode("utf-8")),
        }
    )


def _tool_output_guardrail(
    data: ToolOutputGuardrailData,
) -> ToolGuardrailFunctionOutput:
    """Ensure only bounded, JSON-safe tool results continue to the model."""
    runtime = getattr(data.context, "context", None)
    max_bytes = int(getattr(runtime, "tool_result_bytes", 65_536))
    try:
        encoded = json.dumps(
            sanitize_data(_jsonable(data.output)),
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return ToolGuardrailFunctionOutput.raise_exception(
            {"code": "tool_output_invalid"}
        )
    output_bytes = len(encoded.encode("utf-8"))
    if output_bytes > max_bytes:
        return ToolGuardrailFunctionOutput.raise_exception(
            {"code": "tool_output_too_large", "max_bytes": max_bytes}
        )
    return ToolGuardrailFunctionOutput.allow(
        {
            "tool_name": getattr(data.context, "tool_name", "unknown"),
            "output_bytes": output_bytes,
        }
    )


def _sdk_tool_input_guardrails() -> list[ToolInputGuardrail[Any]]:
    return [
        ToolInputGuardrail(
            _tool_input_guardrail,
            name="buglens_tool_input_boundary",
        )
    ]


def _sdk_tool_output_guardrails() -> list[ToolOutputGuardrail[Any]]:
    return [
        ToolOutputGuardrail(
            _tool_output_guardrail,
            name="buglens_tool_output_boundary",
        )
    ]


@dataclass(frozen=True)
class ToolRegistration:
    tool: Any
    name: str
    version: str = "v1"
    nodes: frozenset[str] = frozenset({"analyze"})
    profiles: frozenset[str] = frozenset()
    tenants: frozenset[str] = frozenset()
    capabilities: frozenset[str] = frozenset()
    needs_approval: bool = False


class ToolRegistry:
    """Filter tools before they are exposed to a node's Agent."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        allowed_nodes: set[str] | None = None,
        allowed_profiles: set[str] | None = None,
        max_results: int = 20,
        max_result_bytes: int = 65_536,
        timeout_seconds: float = 30,
    ) -> None:
        self.enabled = enabled
        self.allowed_nodes = allowed_nodes or {"analyze"}
        self.allowed_profiles = allowed_profiles or set()
        self.max_results = max_results
        self.max_result_bytes = max_result_bytes
        self.timeout_seconds = timeout_seconds
        self._registrations: dict[str, ToolRegistration] = {}

    def register(
        self,
        tool: Any,
        *,
        name: str | None = None,
        version: str = "v1",
        nodes: set[str] | frozenset[str] | None = None,
        profiles: set[str] | frozenset[str] | None = None,
        tenants: set[str] | frozenset[str] | None = None,
        capabilities: set[str] | frozenset[str] | None = None,
        timeout_seconds: float | None = None,
        timeout_behavior: Literal[
            "error_as_result", "raise_exception"
        ] = "error_as_result",
        needs_approval: bool | None = None,
    ) -> Any:
        if not hasattr(tool, "name") or not hasattr(tool, "on_invoke_tool"):
            options: dict[str, Any] = {
                "name_override": name,
                "failure_error_function": None,
                "strict_mode": True,
                "tool_input_guardrails": _sdk_tool_input_guardrails(),
                "tool_output_guardrails": _sdk_tool_output_guardrails(),
            }
            # SDK 0.22 applies timeout_seconds to async handlers.  A sync fake
            # connector remains useful in unit tests, so it is wrapped without
            # an unsupported timeout option.
            if inspect.iscoroutinefunction(tool):
                options.update(
                    timeout=timeout_seconds or self.timeout_seconds,
                    timeout_behavior=timeout_behavior,
                )
            options["needs_approval"] = bool(needs_approval)
            tool = function_tool(tool, **options)
            tool._buglens_guardrails_installed = True
        else:
            # FunctionTool's default failure formatter turns exceptions into a
            # model-visible string. The runtime must observe connector failures
            # as failed executions so it can audit, retry, or offer Resume.
            tool = copy.copy(tool)
            if hasattr(tool, "_use_default_failure_error_function"):
                tool._use_default_failure_error_function = False
                tool._failure_error_function = None
            if needs_approval is not None and hasattr(tool, "needs_approval"):
                tool.needs_approval = needs_approval
            if not getattr(tool, "_buglens_guardrails_installed", False):
                tool.tool_input_guardrails = [
                    *(getattr(tool, "tool_input_guardrails", None) or []),
                    *_sdk_tool_input_guardrails(),
                ]
                tool.tool_output_guardrails = [
                    *(getattr(tool, "tool_output_guardrails", None) or []),
                    *_sdk_tool_output_guardrails(),
                ]
                tool._buglens_guardrails_installed = True
        tool_name = name or getattr(tool, "name", None)
        if not tool_name:
            raise ValueError("registered tool must have a name")
        tool = _instrument_tool(tool, version)
        registration = ToolRegistration(
            tool=tool,
            name=tool_name,
            version=version,
            nodes=frozenset(nodes or {"analyze"}),
            profiles=frozenset(profiles or set()),
            tenants=frozenset(tenants or set()),
            capabilities=frozenset(capabilities or set()),
            needs_approval=bool(getattr(tool, "needs_approval", False)),
        )
        self._registrations[tool_name] = registration
        return tool

    def is_enabled(
        self,
        registration: ToolRegistration,
        *,
        node_name: str,
        profile: str = "default",
        tenant_id: str | None = None,
        capabilities: set[str] | None = None,
        enabled: bool | None = None,
        allowed_nodes: set[str] | None = None,
        allowed_profiles: set[str] | None = None,
    ) -> bool:
        effective_enabled = self.enabled if enabled is None else enabled
        effective_nodes = self.allowed_nodes if allowed_nodes is None else allowed_nodes
        effective_profiles = (
            self.allowed_profiles if allowed_profiles is None else allowed_profiles
        )
        if not effective_enabled or node_name not in effective_nodes:
            return False
        if effective_profiles and profile not in effective_profiles:
            return False
        if node_name not in registration.nodes:
            return False
        if registration.profiles and profile not in registration.profiles:
            return False
        if registration.tenants and tenant_id not in registration.tenants:
            return False
        if registration.capabilities and not registration.capabilities.issubset(
            capabilities or set()
        ):
            return False
        return True

    def tools_for(
        self,
        *,
        node_name: str,
        profile: str = "default",
        tenant_id: str | None = None,
        capabilities: set[str] | None = None,
        enabled: bool | None = None,
        allowed_nodes: set[str] | None = None,
        allowed_profiles: set[str] | None = None,
        max_results: int | None = None,
        timeout_seconds: float | None = None,
    ) -> list[Any]:
        selected = [
            registration.tool
            for registration in self._registrations.values()
            if self.is_enabled(
                registration,
                node_name=node_name,
                profile=profile,
                tenant_id=tenant_id,
                capabilities=capabilities,
                enabled=enabled,
                allowed_nodes=allowed_nodes,
                allowed_profiles=allowed_profiles,
            )
        ][: self.max_results if max_results is None else max(0, max_results)]
        if timeout_seconds is None:
            return selected
        configured: list[Any] = []
        for tool in selected:
            if not hasattr(tool, "timeout_seconds"):
                configured.append(tool)
                continue
            if tool.timeout_seconds == timeout_seconds:
                configured.append(tool)
                continue
            copy_tool = copy.copy(tool)
            copy_tool.timeout_seconds = timeout_seconds
            configured.append(copy_tool)
        return configured

    def registration(self, name: str) -> ToolRegistration | None:
        return self._registrations.get(name)


def bounded_tool_result(value: Any, *, max_bytes: int = 65_536) -> Any:
    """Return a sanitized, bounded result suitable for audit/model context."""
    value = _jsonable(value)
    clean = sanitize_data(value)
    encoded = json.dumps(clean, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= max_bytes:
        return clean
    return {
        "truncated": True,
        "sha256": sha256(encoded.encode("utf-8")).hexdigest(),
        "bytes": len(encoded.encode("utf-8")),
    }


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def _instrument_tool(tool: Any, version: str) -> Any:
    """Decorate a FunctionTool at its ToolContext boundary for exact arguments."""
    if getattr(tool, "_buglens_instrumented", False):
        return tool
    original = tool.on_invoke_tool

    async def invoke(context, arguments_json: str):
        runtime = getattr(context, "context", None)
        observer = getattr(runtime, "execution_observer", None)
        run_id = getattr(runtime, "graph_run_id", "unknown")
        node_execution_id = getattr(runtime, "execution_id", None) or "unknown"
        sdk_tool_call_id = getattr(context, "tool_call_id", None) or uuid4().hex
        tool_execution_id = f"toolexec_{uuid4().hex}"
        try:
            raw_arguments = json.loads(arguments_json)
        except (TypeError, ValueError):
            raw_arguments = arguments_json
        clean_arguments = bounded_tool_result(raw_arguments, max_bytes=16_384)
        encoded_arguments = json.dumps(
            clean_arguments, ensure_ascii=False, default=str, separators=(",", ":")
        )
        started = time.monotonic()
        if observer is not None:
            observer.tool_started(
                tool_execution_id=tool_execution_id,
                node_execution_id=node_execution_id,
                run_id=run_id,
                sdk_tool_call_id=sdk_tool_call_id,
                tool_name=getattr(tool, "name", "unknown"),
                tool_version=version,
                retry_index=getattr(runtime, "retry_index", 0),
                arguments_json=encoded_arguments,
                arguments_hash=sha256(encoded_arguments.encode()).hexdigest(),
            )
        try:
            result = await original(context, arguments_json)
            bounded = bounded_tool_result(
                result,
                max_bytes=getattr(runtime, "tool_result_bytes", 65_536),
            )
            evidence_items: list[EvidenceRecord] = []
            if isinstance(result, EvidenceRecord):
                evidence_items = [result]
            elif isinstance(result, list):
                evidence_items = [
                    item for item in result if isinstance(item, EvidenceRecord)
                ][:50]
            register = getattr(observer, "evidence_registered", None)
            if register is not None:
                for index, item in enumerate(evidence_items):
                    if item.tool_execution_id is None:
                        item = item.model_copy(
                            update={"tool_execution_id": tool_execution_id}
                        )
                        evidence_items[index] = item
                    register(item)
            evidence_ids = [item.evidence_id for item in evidence_items]
            if observer is not None:
                observer.tool_finished(
                    tool_execution_id,
                    status="succeeded",
                    result_summary_json=json.dumps(
                        bounded, ensure_ascii=False, default=str, separators=(",", ":")
                    ),
                    evidence_ids_json=json.dumps(evidence_ids),
                    completed_at=datetime_now_iso(),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            return bounded
        except ToolTimeoutError:
            if observer is not None:
                observer.tool_finished(
                    tool_execution_id,
                    status="failed",
                    error_code="tool_timeout",
                    error_message="read-only tool execution timed out",
                    retryable=True,
                    completed_at=datetime_now_iso(),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            raise
        except ToolExecutionError:
            if observer is not None:
                observer.tool_finished(
                    tool_execution_id,
                    status="failed",
                    error_code="tool_execution_failed",
                    error_message="read-only tool execution failed",
                    retryable=True,
                    completed_at=datetime_now_iso(),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            raise
        except Exception as exc:
            error_code = (
                "tool_timeout"
                if isinstance(exc, TimeoutError)
                else "tool_execution_failed"
            )
            if observer is not None:
                observer.tool_finished(
                    tool_execution_id,
                    status="failed",
                    error_code=error_code,
                    error_message="read-only tool execution failed",
                    retryable=True,
                    completed_at=datetime_now_iso(),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            raise ToolExecutionError(
                "read-only tool execution failed",
                code=error_code,
            ) from exc

    wrapped = copy.copy(tool)
    wrapped.on_invoke_tool = invoke
    wrapped._buglens_instrumented = True
    return wrapped


class AuditingRunHooks(RunHooks):
    """Bridge SDK lifecycle hooks to a small store observer."""

    def __init__(self, observer: ToolAuditObserver | None = None) -> None:
        super().__init__()
        self.observer = observer
        self._active: dict[tuple[str, str], tuple[str, float]] = {}

    async def on_tool_start(self, context, agent, tool) -> None:
        if self.observer is None or getattr(tool, "_buglens_instrumented", False):
            return
        runtime = getattr(context, "context", None)
        run_id = getattr(runtime, "graph_run_id", "unknown")
        node_execution_id = getattr(runtime, "execution_id", None) or "unknown"
        sdk_tool_call_id = getattr(context, "tool_call_id", None) or uuid4().hex
        tool_name = getattr(tool, "name", "unknown")
        tool_execution_id = f"toolexec_{uuid4().hex}"
        self._active[(node_execution_id, tool_name)] = (
            tool_execution_id,
            time.monotonic(),
        )
        self.observer.tool_started(
            tool_execution_id=tool_execution_id,
            node_execution_id=node_execution_id,
            run_id=run_id,
            sdk_tool_call_id=sdk_tool_call_id,
            tool_name=tool_name,
            tool_version="unknown",
            retry_index=getattr(runtime, "retry_index", 0),
            arguments_json="{}",
            arguments_hash=sha256(b"{}").hexdigest(),
        )

    async def on_tool_end(self, context, agent, tool, result) -> None:
        if self.observer is None or getattr(tool, "_buglens_instrumented", False):
            return
        runtime = getattr(context, "context", None)
        node_execution_id = getattr(runtime, "execution_id", None) or "unknown"
        tool_name = getattr(tool, "name", "unknown")
        active = self._active.pop((node_execution_id, tool_name), None)
        if active is None:
            return
        tool_execution_id, started = active
        bounded = bounded_tool_result(
            result,
            max_bytes=getattr(runtime, "tool_result_bytes", 65_536),
        )
        evidence_ids: list[str] = []
        if isinstance(result, EvidenceRecord):
            if result.tool_execution_id is None:
                result = result.model_copy(
                    update={"tool_execution_id": tool_execution_id}
                )
            evidence_ids.append(result.evidence_id)
            register = getattr(self.observer, "evidence_registered", None)
            if register is not None:
                register(result)
        self.observer.tool_finished(
            tool_execution_id,
            status="succeeded",
            result_summary_json=json.dumps(
                bounded, ensure_ascii=False, default=str, separators=(",", ":")
            ),
            evidence_ids_json=json.dumps(evidence_ids),
            completed_at=datetime_now_iso(),
            duration_ms=int((time.monotonic() - started) * 1000),
        )


def datetime_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = [
    "AuditingRunHooks",
    "ToolAuditObserver",
    "ToolExecutionError",
    "ToolRegistration",
    "ToolRegistry",
    "bounded_tool_result",
]
