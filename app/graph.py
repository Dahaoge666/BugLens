from __future__ import annotations

from .agents import NodeRunner, NodeRuntimeContext
from .models import (
    AnalyzeInput,
    DiagnosisReport,
    DiagnosisState,
    EvaluationInput,
    EvaluationResult,
    GraphContractError,
    InvestigationInput,
    InvestigationResult,
    ProblemAnalysis,
    SummaryInput,
    UserAnswer,
    UserInteractionRequest,
)
from .prompts import PromptRegistry
from .security import sanitize_data
from .storage import JsonStateStore


class DiagnosisGraph:
    """Deterministic routing between independent, typed node runs."""

    def __init__(
        self, runner: NodeRunner, store: JsonStateStore, prompts: PromptRegistry
    ) -> None:
        self.runner = runner
        self.store = store
        self.prompts = prompts

    async def start(self, state: DiagnosisState) -> DiagnosisState:
        if state.status != "running" or state.analysis is not None:
            raise GraphContractError("Diagnosis has already started")
        state.status = "running"
        self.store.save(state)
        await self._analyze(state)
        if state.analysis and state.analysis.interaction_request:
            return await self._pause(state, state.analysis.interaction_request)
        return await self._investigate_until_done(state)

    async def resume(
        self, state: DiagnosisState, answers: list[UserAnswer]
    ) -> DiagnosisState:
        request = state.pending_interaction
        if state.status != "awaiting_user_input" or request is None:
            raise GraphContractError("This diagnosis is not awaiting clarification")
        request.validate_answers(answers)
        state.answers.extend(
            UserAnswer.model_validate(sanitize_data(answer.model_dump()))
            for answer in answers
        )
        state.pending_interaction = None
        state.status = "running"
        if request.resume_node == "analyze":
            await self._analyze(state)
            if state.analysis and state.analysis.interaction_request:
                return await self._pause(state, state.analysis.interaction_request)
        return await self._investigate_until_done(
            state, resuming=request.resume_node == "investigate"
        )

    async def cancel(self, state: DiagnosisState) -> DiagnosisState:
        if state.status != "awaiting_user_input":
            raise GraphContractError("Only a paused diagnosis can be cancelled")
        state.status = "inconclusive"
        state.pending_interaction = None
        return await self._summarize(state)

    async def _analyze(self, state: DiagnosisState) -> None:
        payload = AnalyzeInput(
            question=state.user_question,
            evidence=state.source_evidence,
            environment_hint=self._string_context(state, "environment"),
            clarification_answers=state.answers,
        )
        result = await self.runner.run(
            "analyze", payload, self._runtime(state, "analyze", "analyzer-v1")
        )
        result = ProblemAnalysis.model_validate(result)
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
        state.analysis = result
        self.store.save(state)

    async def _investigate_until_done(
        self, state: DiagnosisState, resuming: bool = False
    ) -> DiagnosisState:
        if state.analysis is None:
            raise GraphContractError("Investigation requires analysis")
        while resuming or state.attempt < state.max_attempts:
            if not resuming:
                state.attempt += 1
            resuming = False
            result = await self.runner.run(
                "investigate",
                self._investigation_input(state),
                self._runtime(state, "investigate", self.prompts.version),
                state.analysis.category,
            )
            state.investigation = InvestigationResult.model_validate(result)
            if state.investigation.interaction_request:
                state.investigation.interaction_request.validate_node("investigate")
            self.store.save(state)
            if state.investigation.interaction_request:
                return await self._pause(state, state.investigation.interaction_request)
            evaluation = await self.runner.run(
                "evaluate",
                EvaluationInput(
                    analysis=state.analysis,
                    investigation=state.investigation,
                    rubric_version="rubric-v1",
                ),
                self._runtime(state, "evaluate", "rubric-v1"),
            )
            state.evaluation = EvaluationResult.model_validate(
                evaluation
            ).enforce_rubric()
            self.store.save(state)
            if state.evaluation.passed:
                state.status = "completed"
                return await self._summarize(state)
        state.status = "inconclusive"
        return await self._summarize(state)

    async def _pause(
        self, state: DiagnosisState, request: UserInteractionRequest
    ) -> DiagnosisState:
        if state.clarification_round >= state.max_clarification_rounds:
            state.status = "inconclusive"
            return await self._summarize(state)
        state.pending_interaction = request
        state.clarification_round += 1
        state.status = "awaiting_user_input"
        self.store.save(state)
        return state

    async def _summarize(self, state: DiagnosisState) -> DiagnosisState:
        if state.analysis is None:
            raise GraphContractError("Summary requires analysis")
        result = await self.runner.run(
            "summarize",
            SummaryInput(
                analysis=state.analysis,
                investigation=state.investigation,
                evaluation=state.evaluation,
                status=state.status,
                attempts=state.attempt,
            ),
            self._runtime(state, "summarize", "summary-v1"),
        )
        state.report = DiagnosisReport.model_validate(result)
        if state.status == "inconclusive":
            state.report.primary_conclusion = None
            state.report.status_explanation = (
                "尚未确认根因。" + state.report.status_explanation
            )
        elif not state.investigation or not state.investigation.primary_conclusion:
            state.report.primary_conclusion = None
        self.store.save(state)
        return state

    def _investigation_input(self, state: DiagnosisState) -> InvestigationInput:
        assert state.analysis is not None
        # Explicit whitelist: raw question, tenant prompt, credentials and runtime objects never cross this boundary.
        evidence = [*state.analysis.extracted_evidence]
        for answer in state.answers:
            evidence.extend(answer.attachments)
        return InvestigationInput(
            analysis=state.analysis,
            evidence=evidence,
            previous_evaluation=state.evaluation,
            clarification_answers=state.answers,
            prompt_config_version=self.prompts.version,
        )

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
