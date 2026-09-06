from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from pydantic import BaseModel, ValidationError

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
    """Private to one SDK run. It deliberately carries no state from another node."""

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
    """A controlled failure with no provider details or input secrets."""


class OpenAINodeRunner:
    """Lazy SDK adapter so API import and unit tests do not need an API key or SDK import."""

    def __init__(self, prompts: PromptRegistry) -> None:
        self.prompts = prompts

    async def run(
        self,
        node_name: str,
        payload: BaseModel,
        runtime: NodeRuntimeContext,
        category: ProblemCategory | None = None,
    ) -> BaseModel:
        from agents import Agent, Runner  # imported only for actual model execution
        from agents.exceptions import ModelBehaviorError

        output_types: dict[str, type[BaseModel]] = {
            "analyze": ProblemAnalysis,
            "investigate": InvestigationResult,
            "evaluate": EvaluationResult,
            "summarize": DiagnosisReport,
        }
        instructions: dict[str, str] = {
            "analyze": ANALYSIS_PROMPT,
            "evaluate": EVALUATION_PROMPT,
            "summarize": SUMMARY_PROMPT,
        }
        if node_name == "investigate":
            if category is None:
                raise ValueError("Investigation requires a category")
            # Tenant configuration is selected before the model call. It is not passed
            # through another node's input or retained as an SDK conversation.
            instructions[node_name] = self.prompts.build_investigation_instructions(
                category, runtime.tenant_id
            )
        agent = Agent(
            name=f"BugLens {node_name.title()}",
            instructions=instructions[node_name],
            output_type=output_types[node_name],
            tools=[],  # Phase 1 intentionally exposes no external or write-capable tools.
        )
        # A plain JSON string is the complete model-visible input for this node. No session,
        # conversation_id, previous_response_id, or prior result input is supplied.
        for attempt in range(2):
            try:
                result = await Runner.run(
                    agent,
                    input=payload.model_dump_json(),
                    context=replace(runtime),
                    max_turns=6,
                )
                output = result.final_output
                if isinstance(output, BaseModel):
                    output = output.model_dump()
                return output_types[node_name].model_validate(output)
            except (ValidationError, ModelBehaviorError) as exc:
                if attempt == 1:
                    raise NodeExecutionError(
                        f"{node_name}: invalid structured output after retry"
                    ) from exc
            except Exception as exc:
                raise NodeExecutionError(f"{node_name}: execution failed") from exc
        raise AssertionError("unreachable")
