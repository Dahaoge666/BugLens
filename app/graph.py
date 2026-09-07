"""Deterministic orchestration around isolated SDK agent sessions."""

from __future__ import annotations

from typing import Protocol, cast

from agents import trace

from .agents import NodeRunner, NodeRuntimeContext
from .models import (
    AnalyzeInput,
    ClarificationInput,
    DiagnosisReport,
    DiagnosisState,
    EvaluationInput,
    EvaluationResult,
    Evidence,
    GraphContractError,
    InvestigationInput,
    InvestigationResult,
    ProblemAnalysis,
    RetryInput,
    SummaryInput,
    UserAnswer,
    UserInteractionRequest,
)
from .prompts import PromptRegistry
from .security import sanitize_data


class Clarifier(Protocol):
    async def ask(self, request: UserInteractionRequest) -> list[UserAnswer]: ...


class DiagnosisGraph:
    """Route typed outputs while SDK sessions own node conversation history."""

    def __init__(
        self,
        runner: NodeRunner,
        prompts: PromptRegistry,
        *,
        tracing_enabled: bool = True,
    ) -> None:
        self.runner = runner
        self.prompts = prompts
        self.tracing_enabled = tracing_enabled

    async def run(
        self,
        question: str,
        context: dict[str, str | list[str]],
        evidence: list[Evidence],
        clarifier: Clarifier,
        *,
        run_id: str | None = None,
        max_attempts: int = 2,
        max_clarification_rounds: int = 2,
    ) -> DiagnosisState:
        state = DiagnosisState.create(
            question,
            context,
            evidence,
            max_attempts,
            max_clarification_rounds,
        )
        if run_id:
            state.run_id = run_id
        with trace(
            "BugLens diagnosis",
            group_id=state.run_id,
            metadata={"prompt_version": self.prompts.version},
            disabled=not self.tracing_enabled,
        ):
            if not await self._analyze(state, clarifier):
                return await self._summarize(state)
            return await self._investigate(state, clarifier)

    async def _analyze(self, state: DiagnosisState, clarifier: Clarifier) -> bool:
        payload = AnalyzeInput(
            question=state.user_question,
            evidence=state.source_evidence,
            environment_hint=self._string_context(state, "environment"),
        )
        while True:
            result = cast(
                ProblemAnalysis,
                await self.runner.run(
                    "analyze",
                    payload,
                    self._runtime(state, "analyze", "analyzer-v1"),
                ),
            )
            self._validate_analysis(result)
            state.analysis = result
            if not result.interaction_request:
                return True
            answers = await self._clarify(state, result.interaction_request, clarifier)
            if answers is None:
                state.status = "inconclusive"
                return False
            payload = ClarificationInput(answers=answers)

    async def _investigate(
        self, state: DiagnosisState, clarifier: Clarifier
    ) -> DiagnosisState:
        assert state.analysis is not None
        payload: InvestigationInput | ClarificationInput | RetryInput = (
            InvestigationInput(
                analysis=state.analysis,
                evidence=self._evidence(state),
                prompt_config_version=self.prompts.version,
            )
        )
        while state.attempt < state.max_attempts:
            state.attempt += 1
            while True:
                result = cast(
                    InvestigationResult,
                    await self.runner.run(
                        "investigate",
                        payload,
                        self._runtime(state, "investigate", self.prompts.version),
                        state.analysis.category,
                    ),
                )
                state.investigation = result
                if not result.interaction_request:
                    break
                answers = await self._clarify(
                    state, result.interaction_request, clarifier
                )
                if answers is None:
                    state.status = "inconclusive"
                    return await self._summarize(state)
                payload = ClarificationInput(answers=answers)

            evaluation = cast(
                EvaluationResult,
                await self.runner.run(
                    "evaluate",
                    EvaluationInput(
                        analysis=state.analysis,
                        investigation=result,
                        rubric_version="rubric-v1",
                    ),
                    self._runtime(state, "evaluate", "rubric-v1"),
                ),
            ).enforce_rubric()
            state.evaluation = evaluation
            if evaluation.passed:
                state.status = "completed"
                return await self._summarize(state)
            payload = RetryInput(evaluation=evaluation)

        state.status = "inconclusive"
        return await self._summarize(state)

    async def _clarify(
        self,
        state: DiagnosisState,
        request: UserInteractionRequest,
        clarifier: Clarifier,
    ) -> list[UserAnswer] | None:
        request.validate_node(request.source_node)
        if state.clarification_round >= state.max_clarification_rounds:
            return None
        answers = await clarifier.ask(request)
        request.validate_answers(answers)
        clean_answers = [
            UserAnswer.model_validate(sanitize_data(answer.model_dump()))
            for answer in answers
        ]
        state.answers.extend(clean_answers)
        state.clarification_round += 1
        return clean_answers

    async def _summarize(self, state: DiagnosisState) -> DiagnosisState:
        if state.analysis is None:
            raise GraphContractError("Summary requires analysis")
        state.report = cast(
            DiagnosisReport,
            await self.runner.run(
                "summarize",
                SummaryInput(
                    analysis=state.analysis,
                    investigation=state.investigation,
                    evaluation=state.evaluation,
                    status=state.status,
                    attempts=state.attempt,
                ),
                self._runtime(state, "summarize", "summary-v1"),
            ),
        )
        if state.status == "inconclusive":
            state.report.primary_conclusion = None
            state.report.status_explanation = (
                "尚未确认根因。" + state.report.status_explanation
            )
        elif not state.investigation or not state.investigation.primary_conclusion:
            state.report.primary_conclusion = None
        return state

    @staticmethod
    def _validate_analysis(result: ProblemAnalysis) -> None:
        if result.category_confidence < 0.55 and result.category.value != "unknown":
            raise GraphContractError(
                "Low-confidence analysis must use category unknown"
            )
        if result.category_confidence < 0.55 and result.interaction_request is None:
            raise GraphContractError(
                "Low-confidence analysis must request clarification"
            )
        if result.interaction_request:
            result.interaction_request.validate_node("analyze")

    @staticmethod
    def _evidence(state: DiagnosisState) -> list[Evidence]:
        assert state.analysis is not None
        evidence = [*state.analysis.extracted_evidence]
        for answer in state.answers:
            evidence.extend(answer.attachments)
        return evidence

    def _runtime(
        self, state: DiagnosisState, node_name: str, version: str
    ) -> NodeRuntimeContext:
        return NodeRuntimeContext(
            graph_run_id=state.run_id,
            node_name=node_name,
            config_version=version,
            tenant_id=self._string_context(state, "tenant_id"),
        )

    @staticmethod
    def _string_context(state: DiagnosisState, key: str) -> str | None:
        value = state.user_context.get(key)
        return value if isinstance(value, str) else None
