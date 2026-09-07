"""Deterministic orchestration around isolated SDK agent sessions."""

from __future__ import annotations

from typing import Protocol, cast

from agents import trace

from .agents import NodeRunner, NodeRuntimeContext
from .config import ResolvedRunConfig
from .models import (
    AnalyzeInput,
    ClarificationInput,
    DiagnosisOutcome,
    DiagnosisReport,
    DiagnosisState,
    EvaluationInput,
    EvaluationResult,
    Evidence,
    GraphContractError,
    GraphNode,
    InvestigationInput,
    InvestigationResult,
    LifecycleStatus,
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
        state.lifecycle_status = LifecycleStatus.RUNNING
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
                state.lifecycle_status = LifecycleStatus.COMPLETED
                state.outcome = DiagnosisOutcome.INCONCLUSIVE
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
                    state.lifecycle_status = LifecycleStatus.COMPLETED
                    state.outcome = DiagnosisOutcome.INCONCLUSIVE
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
                state.lifecycle_status = LifecycleStatus.COMPLETED
                state.outcome = DiagnosisOutcome.CONFIRMED
                return await self._summarize(state)
            payload = RetryInput(evaluation=evaluation)

        state.lifecycle_status = LifecycleStatus.COMPLETED
        state.outcome = DiagnosisOutcome.INCONCLUSIVE
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
                    status=(
                        "inconclusive"
                        if state.outcome == DiagnosisOutcome.INCONCLUSIVE
                        else "completed"
                    ),
                    attempts=state.attempt,
                ),
                self._runtime(state, "summarize", "summary-v1"),
            ),
        )
        if state.outcome == DiagnosisOutcome.INCONCLUSIVE:
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

    async def step(self, state: DiagnosisState, config: ResolvedRunConfig) -> str:
        """Execute exactly one graph node and return its node name.

        Runtime owns persistence and lifecycle events; this method only applies
        deterministic business transitions around a typed node call.
        """
        node = GraphNode(state.current_node)
        if node == GraphNode.ANALYZE:
            payload = (
                ClarificationInput.model_validate(state.next_node_input)
                if state.next_node_input
                else AnalyzeInput(
                    question=state.user_question,
                    evidence=state.source_evidence,
                    environment_hint=self._string_context(state, "environment"),
                )
            )
            result = cast(
                ProblemAnalysis,
                await self.runner.run(
                    "analyze",
                    payload,
                    self._runtime_for_config(state, "analyze", config),
                ),
            )
            self._validate_analysis(result)
            state.analysis = result
            state.next_node_input = None
            if result.interaction_request:
                self._set_user_wait(state, result.interaction_request)
            else:
                state.current_node = GraphNode.INVESTIGATE
            return node.value

        if node == GraphNode.INVESTIGATE:
            assert state.analysis is not None
            input_data = state.next_node_input
            if input_data and input_data.get("answers") is not None:
                payload = ClarificationInput.model_validate(input_data)
            elif input_data and input_data.get("evaluation") is not None:
                payload = RetryInput.model_validate(input_data)
                state.attempt += 1
            else:
                payload = InvestigationInput(
                    analysis=state.analysis,
                    evidence=self._evidence(state),
                    prompt_config_version=config.prompt_config_version,
                )
                state.attempt += 1
            result = cast(
                InvestigationResult,
                await self.runner.run(
                    "investigate",
                    payload,
                    self._runtime_for_config(state, "investigate", config),
                    state.analysis.category,
                ),
            )
            state.investigation = result
            state.next_node_input = None
            if result.interaction_request:
                self._set_user_wait(state, result.interaction_request)
            else:
                state.current_node = GraphNode.EVALUATE
            return node.value

        if node == GraphNode.EVALUATE:
            assert state.analysis is not None and state.investigation is not None
            evaluation = cast(
                EvaluationResult,
                await self.runner.run(
                    "evaluate",
                    EvaluationInput(
                        analysis=state.analysis,
                        investigation=state.investigation,
                        rubric_version=config.policy.evaluation.rubric_version,
                    ),
                    self._runtime_for_config(state, "evaluate", config),
                ),
            ).enforce_rubric(
                config.policy.evaluation.passing_score,
                config.policy.evaluation.min_evidence_traceability,
                config.policy.evaluation.min_verification_executability,
            )
            state.evaluation = evaluation
            if evaluation.passed:
                state.outcome = DiagnosisOutcome.CONFIRMED
                state.current_node = GraphNode.SUMMARIZE
            elif state.attempt < config.policy.graph.max_investigation_attempts:
                state.current_node = GraphNode.INVESTIGATE
                state.next_node_input = RetryInput(evaluation=evaluation).model_dump()
            else:
                state.outcome = DiagnosisOutcome.INCONCLUSIVE
                state.current_node = GraphNode.SUMMARIZE
            return node.value

        if node == GraphNode.SUMMARIZE:
            if state.analysis is None:
                raise GraphContractError("Summary requires analysis")
            status = (
                "inconclusive"
                if state.outcome == DiagnosisOutcome.INCONCLUSIVE
                else "completed"
            )
            state.report = cast(
                DiagnosisReport,
                await self.runner.run(
                    "summarize",
                    SummaryInput(
                        analysis=state.analysis,
                        investigation=state.investigation,
                        evaluation=state.evaluation,
                        status=status,
                        attempts=state.attempt,
                    ),
                    self._runtime_for_config(state, "summarize", config),
                ),
            )
            if state.outcome == DiagnosisOutcome.INCONCLUSIVE:
                state.report.primary_conclusion = None
                state.report.status_explanation = (
                    "尚未确认根因。" + state.report.status_explanation
                )
            elif not state.investigation or not state.investigation.primary_conclusion:
                state.report.primary_conclusion = None
            state.current_node = GraphNode.DONE
            state.lifecycle_status = LifecycleStatus.COMPLETED
            return node.value
        raise GraphContractError("Cannot advance a terminal graph")

    @staticmethod
    def _set_user_wait(state: DiagnosisState, request: UserInteractionRequest) -> None:
        request.validate_node(request.source_node)
        if state.clarification_round >= state.max_clarification_rounds:
            state.outcome = DiagnosisOutcome.INCONCLUSIVE
            state.current_node = GraphNode.SUMMARIZE
            state.next_node_input = None
            return
        state.pending_interaction = request
        state.lifecycle_status = LifecycleStatus.WAITING_USER

    def _runtime_for_config(
        self, state: DiagnosisState, node_name: str, config: ResolvedRunConfig
    ) -> NodeRuntimeContext:
        node_policy = config.policy.node(node_name)
        return NodeRuntimeContext(
            graph_run_id=state.run_id,
            node_name=node_name,
            config_version=config.config_version,
            tenant_id=self._string_context(state, "tenant_id"),
            config_snapshot_id=config.snapshot_id,
            prompt_config_version=config.prompt_config_version,
            max_turns=node_policy.max_turns,
        )

    @staticmethod
    def _string_context(state: DiagnosisState, key: str) -> str | None:
        value = state.user_context.get(key)
        return value if isinstance(value, str) else None
