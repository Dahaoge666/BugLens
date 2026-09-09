"""OpenAI Agents SDK adapters and the native diagnosis orchestration loop."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrail,
    ModelRetryBackoffSettings,
    ModelRetrySettings,
    ModelSettings,
    OpenAIChatCompletionsModel,
    OutputGuardrail,
    RetryPolicy,
    RunConfig,
    Runner,
    RunState,
    SessionSettings,
    SQLiteSession,
    ToolExecutionConfig,
    handoff,
    retry_policies,
)
from agents.exceptions import (
    InputGuardrailTripwireTriggered,
    ModelBehaviorError,
    ModelTimeoutError,
    OutputGuardrailTripwireTriggered,
    ToolInputGuardrailTripwireTriggered,
    ToolOutputGuardrailTripwireTriggered,
    ToolTimeoutError,
)
from agents.items import ReasoningItem
from openai import APIConnectionError, AsyncOpenAI
from pydantic import BaseModel

from .config import ModelConfig
from .models import (
    AnalyzeInput,
    ClarificationInput,
    DiagnosisReport,
    DiagnosisTurnResult,
    EvaluationInput,
    EvaluationResult,
    InvestigationBrief,
    InvestigationInput,
    InvestigationResult,
    NativeDiagnosisInput,
    ProblemAnalysis,
    ProblemCategory,
    RetryInput,
    SummaryInput,
)
from .prompts import (
    ANALYSIS_PROMPT,
    EVALUATION_PROMPT,
    NATIVE_INVESTIGATION_SUFFIX,
    NATIVE_TRIAGE_PROMPT,
    SUMMARY_PROMPT,
    PromptRegistry,
)
from .security import sanitize_data
from .tools import (
    AuditingRunHooks,
    ToolExecutionError,
    ToolRegistry,
    bounded_tool_result,
)


@dataclass(frozen=True)
class NodeRuntimeContext:
    graph_run_id: str
    node_name: str
    config_version: str
    tenant_id: str | None = None
    capabilities: frozenset[str] = frozenset()
    config_snapshot_id: str = "pending"
    prompt_config_version: str = "tenant-prompts-v1"
    retry_index: int = 0
    max_turns: int = 6
    model: str | None = None
    model_config: ModelConfig | None = None
    # Direct callers from the pre-v2 API retain the old one-retry behavior;
    # production Graph plans always inject the profile's default of five.
    max_retries: int = 1
    retry_initial_delay: float = 1.0
    retry_max_delay: float = 16.0
    retry_multiplier: float = 2.0
    retry_jitter: bool = True
    node_timeout: float = 60.0
    session_history_limit: int = 100
    profile: str = "default"
    tool_result_bytes: int = 65_536
    tools_enabled: bool = False
    tool_allowed_nodes: frozenset[str] = frozenset({"investigate"})
    tool_allowed_profiles: frozenset[str] = frozenset()
    tool_max_results: int = 20
    tool_max_result_rows: int = 200
    tool_timeout_seconds: float = 30.0
    tool_max_calls: int = 20
    tool_max_concurrent: int = 2
    execution_id: str | None = None
    tool_registry: object | None = None
    execution_observer: object | None = None
    environment_snapshot_id: str | None = None
    environment_snapshot: object | None = None
    environment_tool_manager: object | None = None
    # Native orchestration fields.  ``resolved_config`` and ``native_tracker``
    # are live process objects and are deliberately excluded from RunState
    # serialization below.
    session_id: str | None = None
    active_agent: str | None = None
    resolved_config: object | None = None
    native_tracker: object | None = None


class NodeRunner(Protocol):
    async def run(
        self,
        node_name: str,
        payload: BaseModel,
        runtime: NodeRuntimeContext,
        category: ProblemCategory | None = None,
    ) -> BaseModel: ...

    async def resume_approval(
        self,
        node_name: str,
        payload: BaseModel,
        runtime: NodeRuntimeContext,
        *,
        state_string: str,
        decision: Literal["approved", "rejected"],
        expected_tool_call_ids: list[str] | None = None,
        category: ProblemCategory | None = None,
    ) -> BaseModel: ...


class NodeExecutionError(RuntimeError):
    """A controlled failure that does not expose provider details."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "node_execution_failed",
        retryable: bool = True,
        auto_retry: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.auto_retry = auto_retry


class NodeApprovalRequired(NodeExecutionError):
    """A typed interruption that must be resumed with an SDK ``RunState``."""

    def __init__(
        self,
        *,
        request_id: str,
        state_string: str,
        tool_name: str,
        tool_call_ids: list[str],
        arguments: dict[str, Any],
        explanation: str,
    ) -> None:
        super().__init__(
            "tool approval is required before the diagnosis can continue",
            code="tool_approval_required",
            retryable=False,
            auto_retry=False,
        )
        self.request_id = request_id
        self.state_string = state_string
        self.tool_name = tool_name
        self.tool_call_ids = tool_call_ids
        self.arguments = arguments
        self.explanation = explanation


def _schema_hint(output_type: type[BaseModel]) -> str:
    """Explicit JSON Schema contract for providers that ignore response_format.

    Some OpenAI-compatible gateways (e.g. litellm in streaming mode) do not enforce
    ``response_format`` on the model, so the SDK's structured-output parsing receives
    plain text instead of JSON. Stating the schema inline in the instructions makes
    structured output robust on such providers without changing OpenAI behaviour.
    """
    try:
        schema = output_type.model_json_schema()
    except Exception:  # pragma: no cover - defensive
        return ""
    return (
        "\n\n【输出契约】你必须只输出一个合法 JSON 对象，严格匹配下面的 JSON Schema。"
        "禁止输出解释文字、Markdown 代码块（如 ```json）或任何多余字符；"
        "信息不足的字段用空字符串或空数组填充，但整体必须是可被 json.loads 直接解析的 JSON。\n"
        + json.dumps(schema, ensure_ascii=False, indent=2)
    )


def _install_json_coercion() -> None:
    """Best-effort cleanup of model output before the SDK parses structured output.

    Strips Markdown fences and surrounding prose so that a model which wraps its
    JSON in ```json ... ``` or adds leading text still parses successfully.
    """
    import agents.util._json as _json_mod

    if getattr(_json_mod.validate_json, "_buglens_coerced", False):
        return
    real = _json_mod.validate_json

    def _coerce(text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if lines:
                first = lines[0].strip().lower().removeprefix("```")
                if first in {"json", "jsonc"}:
                    lines = lines[1:]
                else:
                    lines[0] = lines[0].removeprefix("```")
                stripped = "\n".join(lines)
            if stripped.endswith("```"):
                stripped = stripped[:-3].strip()
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end != -1 and end > start:
            return stripped[start : end + 1]
        return stripped

    def patched(json_str, type_adapter, partial, strict=None, **kwargs):
        return real(_coerce(json_str), type_adapter, partial, strict, **kwargs)

    patched._buglens_coerced = True  # type: ignore[attr-defined]
    _json_mod.validate_json = patched


_install_json_coercion()


def _serialize_runtime_context(value: Any) -> dict[str, Any]:
    """Serialize only safe runtime metadata into an SDK RunState checkpoint.

    The SDK can serialize dataclasses automatically, but ``NodeRuntimeContext``
    contains the resolved model configuration and live registry/observer objects.
    An explicit mapping keeps credentials, object references and mutable runtime
    services out of the durable state.  The real context is supplied again through
    ``context_override`` when an approval is resumed.
    """
    if not isinstance(value, NodeRuntimeContext):
        return {}
    set_fields = {
        "capabilities",
        "tool_allowed_nodes",
        "tool_allowed_profiles",
    }
    payload: dict[str, Any] = {}
    for field_name in NodeRuntimeContext.__dataclass_fields__:
        if field_name in {
            "tool_registry",
            "execution_observer",
            "resolved_config",
            "native_tracker",
            "environment_snapshot",
            "environment_tool_manager",
        }:
            continue
        field_value = getattr(value, field_name)
        if field_name in set_fields:
            field_value = sorted(field_value)
        elif field_name == "model_config":
            field_value = (
                field_value.model_dump(mode="json", exclude={"api_key"})
                if field_value is not None
                else None
            )
        payload[field_name] = field_value
    return sanitize_data(payload)


def _reasoning_summary(result: Any) -> list[str]:
    """Collect provider-released reasoning summaries without raw chain-of-thought."""
    summaries: list[str] = []
    for item in getattr(result, "new_items", []) or []:
        if not isinstance(item, ReasoningItem):
            continue
        raw_item = getattr(item, "raw_item", None)
        for summary in getattr(raw_item, "summary", []) or []:
            text = getattr(summary, "text", None)
            if isinstance(text, str) and text.strip():
                clean = str(sanitize_data(text)).strip()
                if clean:
                    summaries.append(clean[:2_000])
    return summaries[:20]


_NODE_INPUT_TYPES: dict[str, tuple[type[BaseModel], ...]] = {
    "analyze": (AnalyzeInput, ClarificationInput),
    "investigate": (InvestigationInput, RetryInput, ClarificationInput),
    "evaluate": (EvaluationInput,),
    "summarize": (SummaryInput,),
}
_MAX_NODE_INPUT_BYTES = 4_000_000
_MAX_NODE_OUTPUT_BYTES = 4_000_000


def _node_input_text_candidates(input_value: Any) -> list[str]:
    """Extract raw JSON and structured-tool JSON from SDK input items.

    ``Agent.as_tool(include_input_schema=True)`` wraps parameters in a human
    readable message with fenced JSON.  Native evaluator guardrails still need
    to validate the data block rather than the wrapper text.
    """

    def add_text(candidates: list[str], text: str) -> None:
        candidates.append(text)
        if "```" not in text:
            return
        for block in re.findall(r"```(?:jsonc?|JSONC?)?\s*(.*?)```", text, re.S):
            if block.strip():
                candidates.append(block.strip())

    if isinstance(input_value, str):
        candidates: list[str] = []
        add_text(candidates, input_value)
        return candidates
    if not isinstance(input_value, list):
        return []

    candidates: list[str] = []
    for item in reversed(input_value):
        content = (
            item.get("content")
            if isinstance(item, dict)
            else getattr(item, "content", None)
        )
        if isinstance(content, str):
            add_text(candidates, content)
            continue
        if not isinstance(content, list):
            continue
        text_parts: list[str] = []
        for part in content:
            text = (
                part.get("text")
                if isinstance(part, dict)
                else getattr(part, "text", None)
            )
            if isinstance(text, str):
                text_parts.append(text)
        if text_parts:
            add_text(candidates, "".join(text_parts))
    return candidates


def _node_input_guardrail(node_name: str) -> InputGuardrail[Any]:
    """Validate the serialized node input before the model is called."""
    accepted_types = _NODE_INPUT_TYPES[node_name]

    def validate(_context, _agent, input_value) -> GuardrailFunctionOutput:
        candidates = _node_input_text_candidates(input_value)
        if not candidates:
            return GuardrailFunctionOutput(
                output_info={"node": node_name, "reason": "input_not_json"},
                tripwire_triggered=True,
            )
        oversized = next(
            (
                value
                for value in candidates
                if len(value.encode("utf-8")) > _MAX_NODE_INPUT_BYTES
            ),
            None,
        )
        if oversized is not None:
            input_bytes = len(oversized.encode("utf-8"))
            return GuardrailFunctionOutput(
                output_info={
                    "node": node_name,
                    "reason": "input_too_large",
                    "bytes": input_bytes,
                },
                tripwire_triggered=True,
            )
        for candidate in candidates:
            input_bytes = len(candidate.encode("utf-8"))
            for input_type in accepted_types:
                try:
                    input_type.model_validate_json(candidate)
                except (TypeError, ValueError):
                    continue
                return GuardrailFunctionOutput(
                    output_info={
                        "node": node_name,
                        "input_type": input_type.__name__,
                        "bytes": input_bytes,
                    },
                    tripwire_triggered=False,
                )
        return GuardrailFunctionOutput(
            output_info={"node": node_name, "reason": "input_schema_invalid"},
            tripwire_triggered=True,
        )

    return InputGuardrail(
        validate,
        name=f"buglens_{node_name}_input_contract",
        run_in_parallel=False,
    )


def _node_output_guardrail(
    node_name: str, output_type: type[BaseModel]
) -> OutputGuardrail[Any]:
    """Validate the final typed node output and its bounded serialized size."""

    def validate(_context, _agent, output) -> GuardrailFunctionOutput:
        try:
            normalized = output_type.model_validate(output)
            serialized = json.dumps(
                normalized.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            return GuardrailFunctionOutput(
                output_info={"node": node_name, "reason": "output_schema_invalid"},
                tripwire_triggered=True,
            )
        output_bytes = len(serialized.encode("utf-8"))
        if output_bytes > _MAX_NODE_OUTPUT_BYTES:
            return GuardrailFunctionOutput(
                output_info={
                    "node": node_name,
                    "reason": "output_too_large",
                    "bytes": output_bytes,
                },
                tripwire_triggered=True,
            )
        return GuardrailFunctionOutput(
            output_info={
                "node": node_name,
                "output_type": output_type.__name__,
                "bytes": output_bytes,
            },
            tripwire_triggered=False,
        )

    return OutputGuardrail(
        validate,
        name=f"buglens_{node_name}_output_contract",
    )


class OpenAINodeRunner:
    """Run typed agents and let SDK SQLiteSession manage each node's history."""

    OUTPUT_TYPES: dict[str, type[BaseModel]] = {
        "analyze": ProblemAnalysis,
        "investigate": InvestigationResult,
        "evaluate": EvaluationResult,
        "summarize": DiagnosisReport,
    }

    def __init__(
        self,
        prompts: PromptRegistry,
        session_db: Path,
        *,
        streaming: bool = False,
        default_streaming: bool | None = None,
        tool_registry: ToolRegistry | None = None,
        default_base_url: str | None = None,
        default_api_key: str | None = None,
        default_timeout: float = 60.0,
    ) -> None:
        session_db.parent.mkdir(parents=True, exist_ok=True)
        self.prompts = prompts
        self.session_db = session_db
        # ``default_streaming`` is the per-model source of truth; ``streaming`` is
        # kept as a back-compat alias so existing callers/tests are unaffected.
        self.default_streaming = (
            default_streaming if default_streaming is not None else streaming
        )
        self.default_base_url = default_base_url
        self.default_api_key = default_api_key
        self.default_timeout = default_timeout
        self.tool_registry = tool_registry or ToolRegistry()
        self._sessions: dict[str, SQLiteSession] = {}
        self._agents: dict[str, Agent] = {}
        self._model_cache: dict[tuple, OpenAIChatCompletionsModel] = {}

    def _session(self, runtime: NodeRuntimeContext) -> SQLiteSession:
        session_id = runtime.session_id or f"{runtime.graph_run_id}:{runtime.node_name}"
        if session_id not in self._sessions:
            self._sessions[session_id] = SQLiteSession(
                session_id,
                self.session_db,
                session_settings=SessionSettings(limit=runtime.session_history_limit),
            )
        return self._sessions[session_id]

    def session_available(self, session_id: str) -> bool:
        """Check that a persisted SDK session exists without creating one."""
        if session_id in self._sessions:
            return True
        import sqlite3

        try:
            connection = sqlite3.connect(self.session_db)
            try:
                row = connection.execute(
                    "SELECT 1 FROM agent_sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.OperationalError:
            return False
        return row is not None

    def _model_instance(self, model_config: ModelConfig) -> OpenAIChatCompletionsModel:
        """Build (and cache) a chat-completions model bound to a specific client.

        Each named model can point at a different provider/credentials, so we
        cannot rely on the SDK's global default client. The client is cached by
        endpoint identity so repeated nodes on the same model reuse one pool.
        """
        base_url = model_config.base_url or self.default_base_url
        api_key = model_config.api_key or self.default_api_key
        timeout = model_config.timeout or self.default_timeout
        cache_key = (
            model_config.model,
            base_url,
            timeout,
            api_key,
        )
        if cache_key in self._model_cache:
            return self._model_cache[cache_key]
        # Transport retries belong to Agents SDK's runner-managed retry policy.
        # Keeping the OpenAI client at zero avoids multiplying retry budgets.
        client_kwargs: dict[str, Any] = {
            "timeout": timeout,
            "max_retries": 0,
        }
        if base_url:
            client_kwargs["base_url"] = base_url
        if api_key or base_url:
            # OpenAI-compatible gateways may authenticate out of band.  Passing
            # an explicit empty key prevents the SDK from requiring the local
            # OPENAI_API_KEY when a profile supplies only base_url.
            client_kwargs["api_key"] = api_key or ""
        client = AsyncOpenAI(**client_kwargs)
        instance = OpenAIChatCompletionsModel(
            model=model_config.model, openai_client=client
        )
        self._model_cache[cache_key] = instance
        return instance

    def _model_arg(
        self, model_name: str | None, model_config: ModelConfig | None
    ) -> str | OpenAIChatCompletionsModel:
        if model_config is not None:
            return self._model_instance(model_config)
        # Back-compat: a bare model name with no registered config uses the SDK
        # global default client (set by configure_openai_provider).
        return model_name or "gpt-4.1-mini"

    def _agent(
        self,
        node_name: str,
        category: ProblemCategory | None,
        tenant_id: str | None,
        model_name: str | None,
        model_config: ModelConfig | None = None,
        *,
        max_retries: int = 5,
        timeout: float = 60.0,
        retry_initial_delay: float = 1.0,
        retry_max_delay: float = 16.0,
        retry_multiplier: float = 2.0,
        retry_jitter: bool = True,
        tools: list[Any] | None = None,
    ) -> Agent:
        key = (
            f"{node_name}:{category}:{tenant_id}:{model_name}:"
            f"{model_config.base_url if model_config else ''}:"
            f"{model_config.api_key if model_config else ''}:"
            f"{model_config.timeout if model_config else ''}:"
            f"{max_retries}:{timeout}:{retry_initial_delay}:{retry_max_delay}:"
            f"{retry_multiplier}:{retry_jitter}:"
            f"{','.join(getattr(tool, 'name', '') for tool in tools or [])}"
        )
        if key in self._agents:
            return self._agents[key]
        instructions = {
            "analyze": ANALYSIS_PROMPT,
            "evaluate": EVALUATION_PROMPT,
            "summarize": SUMMARY_PROMPT,
        }
        if node_name == "investigate":
            if category is None:
                raise ValueError("Investigation requires a category")
            instructions[node_name] = self.prompts.build_investigation_instructions(
                category, tenant_id
            )
        agent = Agent(
            name=f"BugLens {node_name.title()}",
            instructions=instructions[node_name]
            + _schema_hint(self.OUTPUT_TYPES[node_name]),
            output_type=self.OUTPUT_TYPES[node_name],
            input_guardrails=[_node_input_guardrail(node_name)],
            output_guardrails=[
                _node_output_guardrail(node_name, self.OUTPUT_TYPES[node_name])
            ],
            tools=tools or [],
            model=self._model_arg(model_name, model_config),
            model_settings=self._model_settings(
                max_retries=max_retries,
                timeout=timeout,
                retry_initial_delay=retry_initial_delay,
                retry_max_delay=retry_max_delay,
                retry_multiplier=retry_multiplier,
                retry_jitter=retry_jitter,
            ),
        )
        self._agents[key] = agent
        return agent

    @staticmethod
    def _model_settings(
        *,
        max_retries: int,
        timeout: float,
        retry_initial_delay: float = 1.0,
        retry_max_delay: float = 16.0,
        retry_multiplier: float = 2.0,
        retry_jitter: bool = True,
    ) -> ModelSettings:
        policy: RetryPolicy = retry_policies.any(
            retry_policies.provider_suggested(),
            retry_policies.retry_after(),
            retry_policies.network_error(),
            retry_policies.http_status([408, 409, 429, 500, 502, 503, 504]),
        )
        return ModelSettings(
            timeout=timeout,
            retry=ModelRetrySettings(
                max_retries=max_retries,
                backoff=ModelRetryBackoffSettings(
                    initial_delay=retry_initial_delay,
                    max_delay=retry_max_delay,
                    multiplier=retry_multiplier,
                    jitter=retry_jitter,
                ),
                policy=policy,
            ),
        )

    @staticmethod
    def _session_input_callback(history_items, new_items, limit: int = 100):
        """Merge SDK session history while retaining only bounded prior turns."""
        return [*history_items[-max(1, limit) :], *new_items]

    @staticmethod
    def _retryable_exception(exc: Exception) -> bool:
        if isinstance(
            exc,
            (
                ModelTimeoutError,
                TimeoutError,
                ConnectionError,
                OSError,
                APIConnectionError,
            ),
        ):
            return True
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        return isinstance(status, int) and (
            status == 408 or status == 409 or status == 429 or status >= 500
        )

    def _tools_for(self, runtime: NodeRuntimeContext, node_name: str) -> list[Any]:
        registry = runtime.tool_registry or self.tool_registry
        if registry is None:
            return []
        method = getattr(registry, "tools_for", None)
        if method is None:
            return []
        try:
            tools = method(
                node_name=node_name,
                profile=runtime.profile,
                tenant_id=runtime.tenant_id,
                capabilities=set(runtime.capabilities),
                enabled=runtime.tools_enabled,
                allowed_nodes=set(runtime.tool_allowed_nodes),
                allowed_profiles=set(runtime.tool_allowed_profiles),
                max_results=runtime.tool_max_results,
                timeout_seconds=runtime.tool_timeout_seconds,
                environment_snapshot_id=runtime.environment_snapshot_id,
            )
        except TypeError:
            # Keep custom registries written against the small pre-v2 method
            # signature compatible with the runtime.
            tools = method(
                node_name=node_name,
                profile=runtime.profile,
                tenant_id=runtime.tenant_id,
                capabilities=set(runtime.capabilities),
            )
        return list(tools)

    def _streaming_for(self, runtime: NodeRuntimeContext) -> bool:
        if (
            runtime.model_config is not None
            and runtime.model_config.streaming is not None
        ):
            return runtime.model_config.streaming
        return self.default_streaming

    @staticmethod
    def _run_config(runtime: NodeRuntimeContext) -> RunConfig:
        return RunConfig(
            session_settings=SessionSettings(limit=runtime.session_history_limit),
            session_input_callback=lambda history, new_items: (
                OpenAINodeRunner._session_input_callback(
                    history, new_items, runtime.session_history_limit
                )
            ),
            tool_execution=ToolExecutionConfig(
                # Validate a proposed call before an approval interruption is
                # emitted, then let the SDK run the same guardrail again after
                # approval immediately before invoking the connector.
                pre_approval_tool_input_guardrails=True,
            ),
            trace_include_sensitive_data=False,
        )

    @staticmethod
    def _record_reasoning(result: Any, runtime: NodeRuntimeContext) -> None:
        observer = runtime.execution_observer
        callback = getattr(observer, "reasoning_captured", None)
        if callback is not None:
            callback(_reasoning_summary(result))

    def _approval_required(
        self, result: Any, runtime: NodeRuntimeContext
    ) -> NodeApprovalRequired:
        interruptions = list(getattr(result, "interruptions", []) or [])
        if not interruptions:
            raise NodeExecutionError(
                "tool approval state was not returned by the SDK",
                code="sdk_run_state_unavailable",
                retryable=False,
                auto_retry=False,
            )
        try:
            state = result.to_state()
            state_string = state.to_string(
                context_serializer=_serialize_runtime_context,
                strict_context=True,
                include_tracing_api_key=False,
            )
        except Exception as exc:
            raise NodeExecutionError(
                "tool approval state could not be checkpointed",
                code="sdk_run_state_unavailable",
                retryable=False,
                auto_retry=False,
            ) from exc

        names = [str(item.name or "unknown")[:128] for item in interruptions]
        tool_name = names[0] if len(set(names)) == 1 else "multiple_tools"
        call_ids = [
            str(item.call_id)[:128]
            for item in interruptions
            if item.call_id is not None
        ]
        arguments: dict[str, Any] = {}
        for index, item in enumerate(interruptions, start=1):
            raw_arguments = item.arguments
            if raw_arguments is None:
                continue
            try:
                parsed = json.loads(raw_arguments)
            except (TypeError, ValueError):
                parsed = raw_arguments
            bounded = bounded_tool_result(parsed, max_bytes=4_096)
            key = (
                names[index - 1]
                if len(interruptions) == 1
                else f"{names[index - 1]}_{index}"
            )
            if isinstance(bounded, dict):
                for argument_name, value in bounded.items():
                    if isinstance(value, list):
                        arguments[f"{key}.{argument_name}"] = [
                            str(item)[:512] for item in value[:20]
                        ]
                    elif isinstance(value, (str, int, float, bool)) or value is None:
                        arguments[f"{key}.{argument_name}"] = value
                    else:
                        arguments[f"{key}.{argument_name}"] = str(value)[:2_000]
            else:
                arguments[key] = str(bounded)[:2_000]
        explanation = ("只读工具调用需要你的审批：" + ", ".join(dict.fromkeys(names)))[
            :2_000
        ]
        return NodeApprovalRequired(
            request_id=f"approval_{uuid4().hex}",
            state_string=state_string,
            tool_name=tool_name,
            tool_call_ids=call_ids,
            arguments=arguments,
            explanation=explanation,
        )

    async def run(
        self,
        node_name: str,
        payload: BaseModel,
        runtime: NodeRuntimeContext,
        category: ProblemCategory | None = None,
    ) -> BaseModel:
        agent = self._agent(
            node_name,
            category,
            runtime.tenant_id,
            runtime.model,
            runtime.model_config,
            max_retries=runtime.max_retries,
            timeout=runtime.node_timeout,
            retry_initial_delay=runtime.retry_initial_delay,
            retry_max_delay=runtime.retry_max_delay,
            retry_multiplier=runtime.retry_multiplier,
            retry_jitter=runtime.retry_jitter,
            tools=self._tools_for(runtime, node_name),
        )
        output_type = self.OUTPUT_TYPES[node_name]
        streaming = self._streaming_for(runtime)
        session = self._session(runtime)
        run_config = self._run_config(runtime)
        hooks = AuditingRunHooks(runtime.execution_observer)
        for attempt in range(runtime.max_retries + 1):
            try:
                if streaming:
                    result = Runner.run_streamed(
                        agent,
                        input=payload.model_dump_json(),
                        context=runtime,
                        session=session,
                        max_turns=runtime.max_turns,
                        run_config=run_config,
                        hooks=hooks,
                    )
                    async for _event in result.stream_events():
                        pass
                    if getattr(result, "run_loop_exception", None) is not None:
                        raise result.run_loop_exception
                else:
                    result = await Runner.run(
                        agent,
                        input=payload.model_dump_json(),
                        context=runtime,
                        session=session,
                        max_turns=runtime.max_turns,
                        run_config=run_config,
                        hooks=hooks,
                    )
                self._record_reasoning(result, runtime)
                if getattr(result, "interruptions", None):
                    raise self._approval_required(result, runtime)
                output = result.final_output
                if not isinstance(output, output_type):
                    raise ModelBehaviorError(
                        f"{node_name} returned an unexpected output type"
                    )
                return output
            except ModelBehaviorError as exc:
                if runtime.execution_observer is not None:
                    raise NodeExecutionError(
                        f"{node_name}: invalid structured output",
                        code="invalid_structured_output",
                        retryable=True,
                        auto_retry=True,
                    ) from exc
                if attempt == runtime.max_retries:
                    raise NodeExecutionError(
                        f"{node_name}: invalid structured output after retry",
                        code="invalid_structured_output",
                        retryable=True,
                        auto_retry=False,
                    ) from exc
            except NodeApprovalRequired:
                raise
            except InputGuardrailTripwireTriggered as exc:
                raise NodeExecutionError(
                    "node input was blocked by an SDK guardrail",
                    code="input_guardrail_blocked",
                    retryable=False,
                    auto_retry=False,
                ) from exc
            except OutputGuardrailTripwireTriggered as exc:
                raise NodeExecutionError(
                    "node output was blocked by an SDK guardrail",
                    code="output_guardrail_blocked",
                    retryable=True,
                    auto_retry=True,
                ) from exc
            except ToolInputGuardrailTripwireTriggered as exc:
                raise NodeExecutionError(
                    "tool input was blocked by an SDK guardrail",
                    code="tool_input_guardrail_blocked",
                    retryable=False,
                    auto_retry=False,
                ) from exc
            except ToolOutputGuardrailTripwireTriggered as exc:
                raise NodeExecutionError(
                    "tool output was blocked by an SDK guardrail",
                    code="tool_output_guardrail_blocked",
                    retryable=False,
                    auto_retry=False,
                ) from exc
            except Exception as exc:
                # ModelSettings owns provider/network retries.  Once Runner has
                # exhausted that budget, surface a resumable failure to the
                # Runtime without asking it to replay the same transport retry
                # cycle and multiplying the request count.
                if isinstance(exc, ToolExecutionError):
                    raise NodeExecutionError(
                        "read-only tool execution failed",
                        code=exc.code,
                        retryable=exc.retryable,
                        auto_retry=True,
                    ) from exc
                if isinstance(exc, ToolTimeoutError):
                    raise NodeExecutionError(
                        "read-only tool execution timed out",
                        code="tool_timeout",
                        retryable=True,
                        auto_retry=True,
                    ) from exc
                retryable = self._retryable_exception(exc)
                raise NodeExecutionError(
                    f"{node_name}: execution failed",
                    code="sdk_retry_exhausted"
                    if retryable
                    else "node_execution_failed",
                    retryable=retryable,
                    auto_retry=False,
                ) from exc
        raise AssertionError("unreachable")

    async def resume_approval(
        self,
        node_name: str,
        payload: BaseModel,
        runtime: NodeRuntimeContext,
        *,
        state_string: str,
        decision: Literal["approved", "rejected"],
        expected_tool_call_ids: list[str] | None = None,
        category: ProblemCategory | None = None,
    ) -> BaseModel:
        """Restore and continue an SDK run after a persisted tool decision."""
        agent = self._agent(
            node_name,
            category,
            runtime.tenant_id,
            runtime.model,
            runtime.model_config,
            max_retries=runtime.max_retries,
            timeout=runtime.node_timeout,
            retry_initial_delay=runtime.retry_initial_delay,
            retry_max_delay=runtime.retry_max_delay,
            retry_multiplier=runtime.retry_multiplier,
            retry_jitter=runtime.retry_jitter,
            tools=self._tools_for(runtime, node_name),
        )
        output_type = self.OUTPUT_TYPES[node_name]
        try:
            state = await RunState.from_string(
                agent,
                state_string,
                context_override=runtime,
            )
        except Exception as exc:
            raise NodeExecutionError(
                "tool approval state could not be restored",
                code="sdk_run_state_unavailable",
                retryable=False,
                auto_retry=False,
            ) from exc

        interruptions = state.get_interruptions()
        if not interruptions:
            raise NodeExecutionError(
                "tool approval state has no pending interruption",
                code="sdk_run_state_unavailable",
                retryable=False,
                auto_retry=False,
            )
        actual_call_ids = {
            str(item.call_id) for item in interruptions if item.call_id is not None
        }
        expected_call_ids = {
            str(item) for item in (expected_tool_call_ids or []) if item
        }
        if expected_call_ids and actual_call_ids != expected_call_ids:
            raise NodeExecutionError(
                "tool approval state does not match the pending request",
                code="sdk_run_state_unavailable",
                retryable=False,
                auto_retry=False,
            )
        for item in interruptions:
            if decision == "approved":
                state.approve(item)
            else:
                state.reject(
                    item,
                    rejection_message="用户拒绝执行该只读工具调用",
                )

        session = self._session(runtime)
        run_config = self._run_config(runtime)
        hooks = AuditingRunHooks(runtime.execution_observer)
        try:
            if self._streaming_for(runtime):
                result = Runner.run_streamed(
                    agent,
                    input=state,
                    max_turns=runtime.max_turns,
                    run_config=run_config,
                    session=session,
                    hooks=hooks,
                )
                async for _event in result.stream_events():
                    pass
                if getattr(result, "run_loop_exception", None) is not None:
                    raise result.run_loop_exception
            else:
                result = await Runner.run(
                    agent,
                    input=state,
                    max_turns=runtime.max_turns,
                    run_config=run_config,
                    session=session,
                    hooks=hooks,
                )
            self._record_reasoning(result, runtime)
            if getattr(result, "interruptions", None):
                raise self._approval_required(result, runtime)
            output = result.final_output
            if not isinstance(output, output_type):
                raise ModelBehaviorError(
                    f"{node_name} returned an unexpected output type"
                )
            return output
        except NodeApprovalRequired:
            raise
        except InputGuardrailTripwireTriggered as exc:
            raise NodeExecutionError(
                "node input was blocked by an SDK guardrail",
                code="input_guardrail_blocked",
                retryable=False,
                auto_retry=False,
            ) from exc
        except OutputGuardrailTripwireTriggered as exc:
            raise NodeExecutionError(
                "node output was blocked by an SDK guardrail",
                code="output_guardrail_blocked",
                retryable=True,
                auto_retry=True,
            ) from exc
        except ToolInputGuardrailTripwireTriggered as exc:
            raise NodeExecutionError(
                "tool input was blocked by an SDK guardrail",
                code="tool_input_guardrail_blocked",
                retryable=False,
                auto_retry=False,
            ) from exc
        except ToolOutputGuardrailTripwireTriggered as exc:
            raise NodeExecutionError(
                "tool output was blocked by an SDK guardrail",
                code="tool_output_guardrail_blocked",
                retryable=False,
                auto_retry=False,
            ) from exc
        except ModelBehaviorError as exc:
            raise NodeExecutionError(
                f"{node_name}: invalid structured output",
                code="invalid_structured_output",
                retryable=True,
                auto_retry=True,
            ) from exc
        except ToolExecutionError as exc:
            raise NodeExecutionError(
                "read-only tool execution failed",
                code=exc.code,
                retryable=exc.retryable,
                auto_retry=True,
            ) from exc
        except ToolTimeoutError as exc:
            raise NodeExecutionError(
                "read-only tool execution timed out",
                code="tool_timeout",
                retryable=True,
                auto_retry=True,
            ) from exc
        except Exception as exc:
            retryable = self._retryable_exception(exc)
            raise NodeExecutionError(
                f"{node_name}: execution failed",
                code="sdk_retry_exhausted" if retryable else "node_execution_failed",
                retryable=retryable,
                auto_retry=False,
            ) from exc

    def close(self) -> None:
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()


def _native_input_guardrail() -> InputGuardrail[Any]:
    """Accept the application turn or the typed handoff brief."""

    accepted_types: tuple[type[BaseModel], ...] = (
        NativeDiagnosisInput,
        InvestigationBrief,
    )

    def validate(_context, _agent, input_value) -> GuardrailFunctionOutput:
        candidates = _node_input_text_candidates(input_value)
        if not candidates:
            return GuardrailFunctionOutput(
                output_info={"reason": "input_not_json"},
                tripwire_triggered=True,
            )
        for candidate in candidates:
            if len(candidate.encode("utf-8")) > _MAX_NODE_INPUT_BYTES:
                return GuardrailFunctionOutput(
                    output_info={"reason": "input_too_large"},
                    tripwire_triggered=True,
                )
            for input_type in accepted_types:
                try:
                    input_type.model_validate_json(candidate)
                except (TypeError, ValueError):
                    continue
                return GuardrailFunctionOutput(
                    output_info={"input_type": input_type.__name__},
                    tripwire_triggered=False,
                )
        return GuardrailFunctionOutput(
            output_info={"reason": "input_schema_invalid"},
            tripwire_triggered=True,
        )

    return InputGuardrail(
        validate,
        name="buglens_native_input_contract",
        run_in_parallel=False,
    )


def _native_output_guardrail() -> OutputGuardrail[Any]:
    """Validate the common final output of triage and investigators."""

    def validate(_context, _agent, output) -> GuardrailFunctionOutput:
        try:
            normalized = DiagnosisTurnResult.model_validate(output)
            serialized = json.dumps(
                normalized.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            return GuardrailFunctionOutput(
                output_info={"reason": "output_schema_invalid"},
                tripwire_triggered=True,
            )
        output_bytes = len(serialized.encode("utf-8"))
        if output_bytes > _MAX_NODE_OUTPUT_BYTES:
            return GuardrailFunctionOutput(
                output_info={"reason": "output_too_large", "bytes": output_bytes},
                tripwire_triggered=True,
            )
        return GuardrailFunctionOutput(
            output_info={
                "output_type": DiagnosisTurnResult.__name__,
                "bytes": output_bytes,
            },
            tripwire_triggered=False,
        )

    return OutputGuardrail(
        validate,
        name="buglens_native_output_contract",
    )


class NativeDiagnosisRunner(OpenAINodeRunner):
    """Run triage, category handoffs and independent review in one SDK loop.

    The application still calls this runner once per short Command execution.
    Inside that call the Agents SDK owns tool calls and handoffs.  A fresh
    hierarchy of Agent objects is built per turn so the evaluator tool can keep
    a per-turn review counter without leaking mutable state between runs.
    """

    NATIVE_VERSION = "native-agents-0.22"

    @staticmethod
    def _phase_config(runtime: NodeRuntimeContext, phase: str):
        resolved = runtime.resolved_config
        if resolved is not None:
            policy = resolved.policy.node(phase)
            return policy, resolved.policy.model(policy.model), resolved.policy.retry
        return (
            type(
                "Policy",
                (),
                {
                    "model": runtime.model or "gpt-4.1-mini",
                    "max_turns": runtime.max_turns,
                },
            )(),
            runtime.model_config,
            type(
                "Retry",
                (),
                {
                    "max_retries": runtime.max_retries,
                    "initial_delay_seconds": runtime.retry_initial_delay,
                    "max_delay_seconds": runtime.retry_max_delay,
                    "multiplier": runtime.retry_multiplier,
                    "jitter": runtime.retry_jitter,
                },
            )(),
        )

    def _agent_kwargs(self, runtime: NodeRuntimeContext, phase: str) -> dict[str, Any]:
        policy, model_config, retry = self._phase_config(runtime, phase)
        return {
            "model": self._model_arg(policy.model, model_config),
            "model_settings": self._model_settings(
                max_retries=retry.max_retries,
                timeout=(
                    model_config.timeout if model_config else runtime.node_timeout
                ),
                retry_initial_delay=retry.initial_delay_seconds,
                retry_max_delay=retry.max_delay_seconds,
                retry_multiplier=retry.multiplier,
                retry_jitter=retry.jitter,
            ),
        }

    @staticmethod
    async def _extract_evaluation(result) -> str:
        output = result.final_output
        if isinstance(output, EvaluationResult):
            return output.model_dump_json()
        return json.dumps(output, ensure_ascii=False)

    def _evaluator(self, runtime: NodeRuntimeContext) -> Agent:
        return Agent(
            name="BugLens Evaluator",
            instructions=(EVALUATION_PROMPT + _schema_hint(EvaluationResult)),
            output_type=EvaluationResult,
            input_guardrails=[_node_input_guardrail("evaluate")],
            output_guardrails=[_node_output_guardrail("evaluate", EvaluationResult)],
            **self._agent_kwargs(runtime, "evaluate"),
        )

    def _investigator(
        self,
        runtime: NodeRuntimeContext,
        category: ProblemCategory,
    ) -> Agent:
        evaluator = self._evaluator(runtime)
        tracker = runtime.native_tracker

        async def extract_with_count(result) -> str:
            if tracker is not None:
                tracker.review_calls = getattr(tracker, "review_calls", 0) + 1
            return await self._extract_evaluation(result)

        def review_enabled(_context, _agent) -> bool:
            return tracker is None or tracker.review_calls < getattr(
                tracker, "max_review_calls", 2
            )

        review_tool = evaluator.as_tool(
            tool_name="review_diagnosis",
            tool_description=(
                "独立评测当前候选定位。必须传入完整 analysis、investigation、"
                "证据目录和 rubric；不要重新定位或修改候选结果。"
            ),
            parameters=EvaluationInput,
            include_input_schema=True,
            custom_output_extractor=extract_with_count,
            is_enabled=review_enabled,
            max_turns=self._phase_config(runtime, "evaluate")[0].max_turns,
        )
        tools = [*self._tools_for(runtime, "investigate"), review_tool]
        instructions = (
            self.prompts.build_investigation_instructions(category, runtime.tenant_id)
            + NATIVE_INVESTIGATION_SUFFIX
            + _schema_hint(DiagnosisTurnResult)
        )
        return Agent(
            name=f"BugLens Investigator {category.value}",
            handoff_description=f"负责 {category.value} 类问题的证据约束定位。",
            instructions=instructions,
            output_type=DiagnosisTurnResult,
            input_guardrails=[_native_input_guardrail()],
            output_guardrails=[_native_output_guardrail()],
            tools=tools,
            **self._agent_kwargs(runtime, "investigate"),
        )

    def _triage(self, runtime: NodeRuntimeContext) -> Agent:
        handoffs = []
        for category in ProblemCategory:
            investigator = self._investigator(runtime, category)
            tracker = runtime.native_tracker

            def record_handoff(_context, brief):
                if tracker is not None:
                    tracker.handoff_brief = brief

            handoffs.append(
                handoff(
                    investigator,
                    tool_name_override=f"transfer_to_{category.value}_investigator",
                    tool_description_override=(
                        f"将已规范化的 {category.value} 问题交给对应定位专家。"
                    ),
                    on_handoff=record_handoff,
                    input_type=InvestigationBrief,
                )
            )
        return Agent(
            name="BugLens Triage",
            instructions=(NATIVE_TRIAGE_PROMPT + _schema_hint(DiagnosisTurnResult)),
            output_type=DiagnosisTurnResult,
            input_guardrails=[_native_input_guardrail()],
            output_guardrails=[_native_output_guardrail()],
            handoffs=handoffs,
            **self._agent_kwargs(runtime, "analyze"),
        )

    def _starting_agent(self, runtime: NodeRuntimeContext) -> Agent:
        active = runtime.active_agent or "triage"
        if active.startswith("investigator:"):
            raw_category = active.split(":", 1)[1]
            try:
                category = ProblemCategory(raw_category)
            except ValueError:
                category = ProblemCategory.UNKNOWN
            return self._investigator(runtime, category)
        return self._triage(runtime)

    @staticmethod
    def _active_agent_key(agent: Any, fallback: str = "triage") -> str:
        name = str(getattr(agent, "name", ""))
        prefix = "BugLens Investigator "
        if name.startswith(prefix):
            category = name.removeprefix(prefix).strip()
            try:
                return f"investigator:{ProblemCategory(category).value}"
            except ValueError:
                return "investigator:unknown"
        if name == "BugLens Triage":
            return "triage"
        return fallback

    async def _run_native_once(
        self,
        agent: Agent,
        payload: NativeDiagnosisInput,
        runtime: NodeRuntimeContext,
        *,
        sdk_input: Any | None = None,
    ) -> DiagnosisTurnResult:
        run_config = self._run_config(runtime)
        hooks = AuditingRunHooks(runtime.execution_observer)
        input_value = sdk_input if sdk_input is not None else payload.model_dump_json()
        if self._streaming_for(runtime):
            streamed = Runner.run_streamed(
                agent,
                input=input_value,
                context=runtime,
                session=self._session(runtime),
                max_turns=runtime.max_turns,
                run_config=run_config,
                hooks=hooks,
            )
            async for _event in streamed.stream_events():
                pass
            if getattr(streamed, "run_loop_exception", None) is not None:
                raise streamed.run_loop_exception
            result = streamed
        else:
            result = await Runner.run(
                agent,
                input=input_value,
                context=runtime,
                session=self._session(runtime),
                max_turns=runtime.max_turns,
                run_config=run_config,
                hooks=hooks,
            )
        self._record_reasoning(result, runtime)
        if getattr(result, "interruptions", None):
            raise self._approval_required(result, runtime)
        output = result.final_output
        if not isinstance(output, DiagnosisTurnResult):
            raise ModelBehaviorError(
                "native diagnosis returned an unexpected output type"
            )
        tracker = runtime.native_tracker
        brief = getattr(tracker, "handoff_brief", None) if tracker else None
        if output.analysis is None and isinstance(brief, InvestigationBrief):
            # A handoff brief is a bounded deterministic fallback, not model
            # memory.  It keeps the final application contract complete if a
            # specialist returns only the investigation fields.
            output = output.model_copy(
                update={
                    "analysis": ProblemAnalysis(
                        category=brief.category,
                        category_confidence=brief.category_confidence,
                        summary=brief.normalized_summary,
                        symptoms=brief.symptoms,
                        impact=brief.impact,
                        environment=brief.environment,
                        missing_information=brief.missing_information,
                    )
                }
            )
        active = self._active_agent_key(
            getattr(result, "last_agent", None), runtime.active_agent or "triage"
        )
        review_count = getattr(tracker, "review_calls", 0) if tracker else 0
        return output.model_copy(
            update={"active_agent": active, "review_count": review_count}
        )

    async def run(
        self,
        node_name: str,
        payload: BaseModel,
        runtime: NodeRuntimeContext,
        category: ProblemCategory | None = None,
    ) -> BaseModel:
        if not isinstance(payload, NativeDiagnosisInput):
            raise NodeExecutionError(
                "native runner received an invalid application input",
                code="invalid_native_input",
                retryable=False,
                auto_retry=False,
            )
        agent = self._starting_agent(runtime)
        for attempt in range(runtime.max_retries + 1):
            try:
                return await self._run_native_once(agent, payload, runtime)
            except NodeApprovalRequired:
                raise
            except InputGuardrailTripwireTriggered as exc:
                raise NodeExecutionError(
                    "native input was blocked by an SDK guardrail",
                    code="input_guardrail_blocked",
                    retryable=False,
                    auto_retry=False,
                ) from exc
            except OutputGuardrailTripwireTriggered as exc:
                if attempt == runtime.max_retries:
                    raise NodeExecutionError(
                        "native output was blocked by an SDK guardrail",
                        code="output_guardrail_blocked",
                        retryable=True,
                        auto_retry=False,
                    ) from exc
            except (
                ToolInputGuardrailTripwireTriggered,
                ToolOutputGuardrailTripwireTriggered,
            ) as exc:
                raise NodeExecutionError(
                    "native tool call was blocked by an SDK guardrail",
                    code="tool_guardrail_blocked",
                    retryable=False,
                    auto_retry=False,
                ) from exc
            except ModelBehaviorError as exc:
                if attempt == runtime.max_retries:
                    raise NodeExecutionError(
                        "native output did not match its structured schema",
                        code="invalid_structured_output",
                        retryable=True,
                        auto_retry=False,
                    ) from exc
            except ToolExecutionError as exc:
                raise NodeExecutionError(
                    "read-only tool execution failed",
                    code=exc.code,
                    retryable=exc.retryable,
                    auto_retry=True,
                ) from exc
            except ToolTimeoutError as exc:
                raise NodeExecutionError(
                    "read-only tool execution timed out",
                    code="tool_timeout",
                    retryable=True,
                    auto_retry=True,
                ) from exc
            except Exception as exc:
                retryable = self._retryable_exception(exc)
                if attempt == runtime.max_retries:
                    raise NodeExecutionError(
                        "native agent execution failed",
                        code="sdk_retry_exhausted"
                        if retryable
                        else "node_execution_failed",
                        retryable=retryable,
                        auto_retry=False,
                    ) from exc
        raise AssertionError("unreachable")

    async def resume_approval(
        self,
        node_name: str,
        payload: BaseModel,
        runtime: NodeRuntimeContext,
        *,
        state_string: str,
        decision: Literal["approved", "rejected"],
        expected_tool_call_ids: list[str] | None = None,
        category: ProblemCategory | None = None,
    ) -> BaseModel:
        if not isinstance(payload, NativeDiagnosisInput):
            raise NodeExecutionError(
                "native approval resume received an invalid input",
                code="invalid_native_input",
                retryable=False,
                auto_retry=False,
            )
        agent = self._starting_agent(runtime)
        try:
            state = await RunState.from_string(
                agent,
                state_string,
                context_override=runtime,
            )
        except Exception as exc:
            raise NodeExecutionError(
                "native approval state could not be restored",
                code="sdk_run_state_unavailable",
                retryable=False,
                auto_retry=False,
            ) from exc
        interruptions = state.get_interruptions()
        if not interruptions:
            raise NodeExecutionError(
                "native approval state has no pending interruption",
                code="sdk_run_state_unavailable",
                retryable=False,
                auto_retry=False,
            )
        expected = {str(item) for item in (expected_tool_call_ids or []) if item}
        actual = {str(item.call_id) for item in interruptions if item.call_id}
        if expected and expected != actual:
            raise NodeExecutionError(
                "native approval state does not match the pending request",
                code="sdk_run_state_unavailable",
                retryable=False,
                auto_retry=False,
            )
        for item in interruptions:
            if decision == "approved":
                state.approve(item)
            else:
                state.reject(item, rejection_message="用户拒绝执行该只读工具调用")
        return await self._run_native_once(
            agent,
            payload,
            runtime,
            sdk_input=state,
        )
