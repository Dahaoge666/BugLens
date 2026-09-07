"""OpenAI Agents SDK adapter with isolated persistent node sessions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agents import Agent, Runner, SQLiteSession
from agents.exceptions import ModelBehaviorError
from pydantic import BaseModel

from .models import (
    DiagnosisReport,
    EvaluationResult,
    InvestigationResult,
    ProblemAnalysis,
    ProblemCategory,
)
from .prompts import ANALYSIS_PROMPT, EVALUATION_PROMPT, SUMMARY_PROMPT, PromptRegistry


@dataclass(frozen=True)
class NodeRuntimeContext:
    graph_run_id: str
    node_name: str
    config_version: str
    tenant_id: str | None = None
    config_snapshot_id: str = "pending"
    prompt_config_version: str = "tenant-prompts-v1"
    max_turns: int = 6
    model: str | None = None


class NodeRunner(Protocol):
    async def run(
        self,
        node_name: str,
        payload: BaseModel,
        runtime: NodeRuntimeContext,
        category: ProblemCategory | None = None,
    ) -> BaseModel: ...


class NodeExecutionError(RuntimeError):
    """A controlled failure that does not expose provider details."""


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


class OpenAINodeRunner:
    """Run typed agents and let SDK SQLiteSession manage each node's history."""

    OUTPUT_TYPES: dict[str, type[BaseModel]] = {
        "analyze": ProblemAnalysis,
        "investigate": InvestigationResult,
        "evaluate": EvaluationResult,
        "summarize": DiagnosisReport,
    }

    def __init__(
        self, prompts: PromptRegistry, session_db: Path, *, streaming: bool = False
    ) -> None:
        session_db.parent.mkdir(parents=True, exist_ok=True)
        self.prompts = prompts
        self.session_db = session_db
        self.streaming = streaming
        self._sessions: dict[str, SQLiteSession] = {}
        self._agents: dict[str, Agent] = {}

    def _session(self, runtime: NodeRuntimeContext) -> SQLiteSession:
        session_id = f"{runtime.graph_run_id}:{runtime.node_name}"
        if session_id not in self._sessions:
            self._sessions[session_id] = SQLiteSession(session_id, self.session_db)
        return self._sessions[session_id]

    def _agent(
        self,
        node_name: str,
        category: ProblemCategory | None,
        tenant_id: str | None,
        model: str | None,
    ) -> Agent:
        key = f"{node_name}:{category}:{tenant_id}:{model}"
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
            tools=[],
            model=model,
        )
        self._agents[key] = agent
        return agent

    async def run(
        self,
        node_name: str,
        payload: BaseModel,
        runtime: NodeRuntimeContext,
        category: ProblemCategory | None = None,
    ) -> BaseModel:
        agent = self._agent(node_name, category, runtime.tenant_id, runtime.model)
        output_type = self.OUTPUT_TYPES[node_name]
        for attempt in range(2):
            try:
                if self.streaming:
                    result = Runner.run_streamed(
                        agent,
                        input=payload.model_dump_json(),
                        context=runtime,
                        session=self._session(runtime),
                        max_turns=runtime.max_turns,
                    )
                    async for _event in result.stream_events():
                        pass
                else:
                    result = await Runner.run(
                        agent,
                        input=payload.model_dump_json(),
                        context=runtime,
                        session=self._session(runtime),
                        max_turns=runtime.max_turns,
                    )
                output = result.final_output
                if not isinstance(output, output_type):
                    raise ModelBehaviorError(
                        f"{node_name} returned an unexpected output type"
                    )
                return output
            except ModelBehaviorError as exc:
                if attempt == 1:
                    raise NodeExecutionError(
                        f"{node_name}: invalid structured output after retry"
                    ) from exc
            except Exception as exc:
                raise NodeExecutionError(f"{node_name}: execution failed") from exc
        raise AssertionError("unreachable")

    def close(self) -> None:
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()
