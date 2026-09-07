"""OpenAI Agents SDK adapter with isolated persistent node sessions."""

from __future__ import annotations

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


class OpenAINodeRunner:
    """Run typed agents and let SDK SQLiteSession manage each node's history."""

    OUTPUT_TYPES: dict[str, type[BaseModel]] = {
        "analyze": ProblemAnalysis,
        "investigate": InvestigationResult,
        "evaluate": EvaluationResult,
        "summarize": DiagnosisReport,
    }

    def __init__(self, prompts: PromptRegistry, session_db: Path) -> None:
        session_db.parent.mkdir(parents=True, exist_ok=True)
        self.prompts = prompts
        self.session_db = session_db
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
    ) -> Agent:
        key = f"{node_name}:{category}:{tenant_id}"
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
            instructions=instructions[node_name],
            output_type=self.OUTPUT_TYPES[node_name],
            tools=[],
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
        agent = self._agent(node_name, category, runtime.tenant_id)
        output_type = self.OUTPUT_TYPES[node_name]
        for attempt in range(2):
            try:
                result = await Runner.run(
                    agent,
                    input=payload.model_dump_json(),
                    context=runtime,
                    session=self._session(runtime),
                    max_turns=6,
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
