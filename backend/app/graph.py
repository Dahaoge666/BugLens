"""Pure deterministic routing around isolated SDK node sessions."""

from __future__ import annotations

from typing import Protocol

from agents import trace

from .agents import NodeRunner, NodeRuntimeContext
from .config import ResolvedRunConfig
from .context import ContextAssembler
from .models import (
    ClarificationInput,
    DiagnosisOutcome,
    DiagnosisReport,
    DiagnosisState,
    EvaluationResult,
    ExecutionFailure,
    GraphContractError,
    GraphNode,
    GraphTransition,
    InvestigationResult,
    LifecycleStatus,
    NodeExecutionPlan,
    ProblemAnalysis,
    ProgressDelta,
    RetryInput,
    SkippedInteraction,
    UserAnswer,
    UserInteractionRequest,
    replace_state,
)
from .prompts import PromptRegistry
from .security import sanitize_data


class Clarifier(Protocol):
    async def ask(self, request: UserInteractionRequest) -> list[UserAnswer]: ...


class DiagnosisGraph:
    """Compute one deterministic transition at a time.

    ``prepare_step`` and ``apply_result`` deliberately do not perform I/O.  The
    Runtime can therefore commit an attempt-start record before calling a model,
    while graph tests exercise the exact production routing path with a fake
    runner.
    """

    def __init__(
        self,
        runner: NodeRunner,
        prompts: PromptRegistry,
        *,
        tracing_enabled: bool = True,
        context_assembler: ContextAssembler | None = None,
    ) -> None:
        self.runner = runner
        self.prompts = prompts
        self.tracing_enabled = tracing_enabled
        self.context_assembler = context_assembler or ContextAssembler()

    def prepare_step(
        self, state: DiagnosisState, config: ResolvedRunConfig
    ) -> NodeExecutionPlan:
        """Create a serialization-safe external-call plan without side effects."""
        if state.lifecycle_status != LifecycleStatus.RUNNING:
            raise GraphContractError("graph can only prepare a running state")
        node = GraphNode(state.current_node)
        if node == GraphNode.DONE:
            raise GraphContractError("cannot prepare a terminal graph")

        payload = self._payload_for_state(node, state, config)
        node_name = node.value
        node_policy = config.policy.node(node_name)
        attempt = state.attempt
        if node == GraphNode.INVESTIGATE and not isinstance(
            payload, ClarificationInput
        ):
            # A retry or Resume repeats the same investigation attempt. Only
            # Evaluate feedback (represented by RetryInput), or the first
            # visit to Investigate, consumes a new attempt budget.
            attempt = (
                state.attempt + 1
                if isinstance(payload, RetryInput) or state.attempt == 0
                else state.attempt
            )
        clarification_round = state.clarification_count(node_name)
        return NodeExecutionPlan(
            node=node_name,
            input_model=payload,
            session_id=f"{state.run_id}:{node_name}",
            investigation_attempt=attempt,
            clarification_round=clarification_round,
            retry_cycle=state.retry_cycle,
            retry_index=state.retry_index,
            config_snapshot_id=config.snapshot_id,
            config_version=config.config_version,
            prompt_config_version=(
                node_policy.prompt_version or config.prompt_config_version
            ),
            agent_definition_version=f"{node_policy.prompt_version}:agents-0.22",
        )

    def apply_result(
        self,
        state: DiagnosisState,
        plan: NodeExecutionPlan,
        node_result=None,
        *,
        error: ExecutionFailure | None = None,
        registered_evidence_ids: set[str] | None = None,
    ) -> tuple[DiagnosisState, GraphTransition]:
        """Apply a validated node result, or a classified execution failure."""
        if state.current_node.value != plan.node:
            raise GraphContractError(
                "execution plan does not match current graph cursor"
            )
        if error is not None:
            failure = state.last_error
            if failure is None or failure.code != error.code:
                from .models import FailureRecord

                failure = FailureRecord(
                    code=error.code,
                    message=error.message,
                    node=GraphNode(plan.node),
                    retryable=error.retryable,
                    retry_cycle=state.retry_cycle,
                    retry_index=state.retry_index,
                    resume_available=error.retryable,
                )
            new_state = replace_state(
                state,
                lifecycle_status=LifecycleStatus.FAILED,
                last_error=failure,
                resume_available=error.retryable,
                active_execution_id=None,
            )
            return new_state, GraphTransition(
                node=plan.node,
                next_node=new_state.current_node,
                lifecycle_status=new_state.lifecycle_status,
                error=error,
            )

        expected_type = {
            "analyze": ProblemAnalysis,
            "investigate": InvestigationResult,
            "evaluate": EvaluationResult,
            "summarize": DiagnosisReport,
        }[plan.node]
        if not isinstance(node_result, expected_type):
            raise GraphContractError(f"{plan.node} returned an invalid output type")
        clean = expected_type.model_validate(
            sanitize_data(node_result.model_dump(mode="python"))
        )
        current = replace_state(
            state,
            active_execution_id=None,
            last_error=None,
            resume_available=False,
        )

        if plan.node == "analyze":
            result = clean
            self._validate_analysis(result)
            if result.interaction_request:
                request = result.interaction_request
                if (
                    current.clarification_count("analyze")
                    >= current.max_clarification_rounds
                ):
                    result = result.model_copy(
                        update={
                            "interaction_request": None,
                            "missing_information": [
                                *result.missing_information,
                                "clarification budget exhausted; requested information unavailable",
                            ][:50],
                        }
                    )
                    current = replace_state(
                        current,
                        analysis=result,
                        current_node=GraphNode.SUMMARIZE,
                        outcome=DiagnosisOutcome.INCONCLUSIVE,
                    )
                else:
                    current = replace_state(
                        current,
                        analysis=result,
                        pending_interaction=request,
                        lifecycle_status=LifecycleStatus.WAITING_USER,
                        next_node_input=None,
                    )
            else:
                current = replace_state(
                    current,
                    analysis=result,
                    current_node=GraphNode.INVESTIGATE,
                    next_node_input=None,
                )

        elif plan.node == "investigate":
            result = clean
            self._validate_investigation(
                result, current, registered_evidence_ids=registered_evidence_ids
            )
            attempt = plan.investigation_attempt
            valid_progress = self._has_new_progress(
                result.progress_delta,
                current,
                registered_evidence_ids=registered_evidence_ids,
            )
            updates = {
                "investigation": result,
                "attempt": max(current.attempt, attempt),
                "next_node_input": None,
                "progress_deltas": [
                    *current.progress_deltas,
                    result.progress_delta,
                ][-20:],
                "no_progress_cycles": 0
                if valid_progress
                else current.no_progress_cycles + 1,
            }
            if result.interaction_request:
                request = result.interaction_request
                if (
                    current.clarification_count("investigate")
                    < current.max_clarification_rounds
                ):
                    updates.update(
                        pending_interaction=request,
                        lifecycle_status=LifecycleStatus.WAITING_USER,
                    )
                else:
                    # The result is still useful, but the unavailable answer is
                    # explicit in the report context rather than silently invented.
                    result = result.model_copy(
                        update={
                            "interaction_request": None,
                            "limitations": [
                                *result.limitations,
                                "clarification budget exhausted; requested information unavailable",
                            ][:50],
                        }
                    )
                    updates.update(
                        investigation=result,
                        current_node=GraphNode.EVALUATE,
                        lifecycle_status=LifecycleStatus.RUNNING,
                    )
            else:
                updates.update(
                    current_node=GraphNode.EVALUATE,
                    lifecycle_status=LifecycleStatus.RUNNING,
                )
            current = replace_state(current, **updates)

        elif plan.node == "evaluate":
            result = clean
            if result.interaction_request:
                request = result.interaction_request
                self._validate_interaction(request, "evaluate", current)
                if current.clarification_count(
                    "evaluate"
                ) < current.max_clarification_rounds and (
                    request.reason != "no_progress" or current.no_progress_cycles >= 2
                ):
                    current = replace_state(
                        current,
                        evaluation=result,
                        pending_interaction=request,
                        lifecycle_status=LifecycleStatus.WAITING_USER,
                        next_node_input=None,
                    )
                else:
                    result = result.model_copy(update={"interaction_request": None})
            if current.lifecycle_status == LifecycleStatus.RUNNING:
                if result.passed:
                    current = replace_state(
                        current,
                        evaluation=result,
                        outcome=DiagnosisOutcome.CONFIRMED,
                        current_node=GraphNode.SUMMARIZE,
                        next_node_input=None,
                    )
                elif current.attempt < current.max_attempts:
                    current = replace_state(
                        current,
                        evaluation=result,
                        current_node=GraphNode.INVESTIGATE,
                        next_node_input=RetryInput(
                            evaluation=result,
                            previous_investigation=current.investigation,
                        ).model_dump(mode="python"),
                    )
                else:
                    current = replace_state(
                        current,
                        evaluation=result,
                        outcome=DiagnosisOutcome.INCONCLUSIVE,
                        current_node=GraphNode.SUMMARIZE,
                        next_node_input=None,
                    )

        elif plan.node == "summarize":
            result = clean
            self._validate_report(result, current)
            if current.outcome is None:
                current = replace_state(current, outcome=DiagnosisOutcome.INCONCLUSIVE)
            if current.outcome == DiagnosisOutcome.INCONCLUSIVE:
                result = result.model_copy(
                    update={
                        "primary_conclusion": None,
                        "status_explanation": "尚未确认根因。"
                        + result.status_explanation,
                    }
                )
            elif (
                not current.investigation
                or not current.investigation.primary_conclusion
            ):
                result = result.model_copy(update={"primary_conclusion": None})
            current = replace_state(
                current,
                report=result,
                current_node=GraphNode.DONE,
                lifecycle_status=LifecycleStatus.COMPLETED,
                outcome=current.outcome or DiagnosisOutcome.INCONCLUSIVE,
            )

        transition = GraphTransition(
            node=plan.node,
            next_node=current.current_node,
            lifecycle_status=current.lifecycle_status,
            outcome=current.outcome,
            interaction_request=current.pending_interaction,
        )
        return current, transition

    @staticmethod
    def _validate_report(result: DiagnosisReport, state: DiagnosisState) -> None:
        known_ids = {record.evidence_id for record in state.source_evidence}
        if state.analysis is not None:
            known_ids.update(
                record.evidence_id for record in state.analysis.extracted_evidence
            )
        known_ids.update(
            record.evidence_id
            for answer in state.answers
            for record in answer.attachments
        )
        if known_ids:
            unknown = set(result.evidence_ids) - known_ids
            if unknown:
                raise GraphContractError(
                    "Report evidence_ids must reference registered evidence IDs"
                )

    def apply_evaluation_policy(
        self,
        state: DiagnosisState,
        plan: NodeExecutionPlan,
        result: EvaluationResult,
        config: ResolvedRunConfig,
        registered_evidence_ids: set[str] | None = None,
    ) -> tuple[DiagnosisState, GraphTransition]:
        """Apply the configured rubric before the normal deterministic route."""
        enforced = result.enforce_rubric(
            config.policy.evaluation.passing_score,
            config.policy.evaluation.min_evidence_traceability,
            config.policy.evaluation.min_verification_executability,
        )
        return self.apply_result(
            state,
            plan,
            enforced,
            registered_evidence_ids=registered_evidence_ids,
        )

    def _payload_for_state(
        self, node: GraphNode, state: DiagnosisState, config: ResolvedRunConfig
    ):
        if state.next_node_input is not None:
            raw = state.next_node_input
            if node == GraphNode.ANALYZE:
                return ClarificationInput.model_validate(raw)
            if node == GraphNode.INVESTIGATE:
                if "evaluation" in raw:
                    return RetryInput.model_validate(raw)
                return ClarificationInput.model_validate(raw)
        payload = self.context_assembler.build(node, state)
        if node == GraphNode.EVALUATE:
            payload = payload.model_copy(
                update={"rubric_version": config.policy.evaluation.rubric_version}
            )
        if node == GraphNode.INVESTIGATE:
            payload = payload.model_copy(
                update={"prompt_config_version": config.prompt_config_version}
            )
        return payload

    @staticmethod
    def _validate_interaction(
        request: UserInteractionRequest, node: str, state: DiagnosisState
    ) -> None:
        request.validate_node(node)
        if request.reason == "no_progress" and state.no_progress_cycles < 2:
            raise GraphContractError(
                "no_progress clarification requires two no-progress cycles"
            )

    @classmethod
    def _validate_analysis(cls, result: ProblemAnalysis) -> None:
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

    @classmethod
    def _validate_investigation(
        cls,
        result: InvestigationResult,
        state: DiagnosisState,
        *,
        registered_evidence_ids: set[str] | None = None,
    ) -> None:
        if result.interaction_request:
            cls._validate_interaction(result.interaction_request, "investigate", state)
        known_ids = {record.evidence_id for record in state.source_evidence}
        if state.analysis is not None:
            known_ids.update(
                record.evidence_id for record in state.analysis.extracted_evidence
            )
        known_ids.update(registered_evidence_ids or set())
        if known_ids:
            references = {
                *[
                    item
                    for hypothesis in result.hypotheses
                    for item in hypothesis.supporting_evidence
                ],
                *[
                    item
                    for hypothesis in result.hypotheses
                    for item in hypothesis.contradicting_evidence
                ],
            }
            unknown = references - known_ids
            if unknown:
                raise GraphContractError(
                    "Hypothesis evidence must reference registered evidence IDs"
                )

    @staticmethod
    def _has_new_progress(
        delta: ProgressDelta,
        state: DiagnosisState,
        *,
        registered_evidence_ids: set[str] | None = None,
    ) -> bool:
        previous_ids = {
            identifier
            for previous in state.progress_deltas
            for identifier in (
                *previous.new_evidence_ids,
                *previous.resolved_gap_ids,
                *previous.changed_hypothesis_ids,
                *previous.discarded_hypothesis_ids,
            )
        }
        evidence_ids = {record.evidence_id for record in state.source_evidence}
        if state.analysis is not None:
            evidence_ids.update(
                record.evidence_id for record in state.analysis.extracted_evidence
            )
        evidence_ids.update(
            record.evidence_id
            for answer in state.answers
            for record in answer.attachments
        )
        evidence_ids.update(registered_evidence_ids or set())
        previous_gaps = (
            set(state.investigation.evidence_gaps)
            if state.investigation is not None
            else set()
        )
        valid_evidence = {
            identifier
            for identifier in delta.new_evidence_ids
            if identifier in evidence_ids and identifier not in previous_ids
        }
        valid_gaps = {
            identifier
            for identifier in delta.resolved_gap_ids
            if identifier in previous_gaps and identifier not in previous_ids
        }
        valid_hypotheses = {
            identifier
            for identifier in (
                *delta.changed_hypothesis_ids,
                *delta.discarded_hypothesis_ids,
            )
            if identifier.strip() and identifier not in previous_ids
        }
        return bool(valid_evidence or valid_gaps or valid_hypotheses)

    @staticmethod
    def _string_context(state: DiagnosisState, key: str) -> str | None:
        value = state.user_context.get(key)
        return value if isinstance(value, str) else None

    def _runtime_for_config(
        self, state: DiagnosisState, node_name: str, config: ResolvedRunConfig
    ) -> NodeRuntimeContext:
        node_policy = config.policy.node(node_name)
        raw_capabilities = state.user_context.get("capabilities", [])
        capabilities = (
            frozenset(raw_capabilities)
            if isinstance(raw_capabilities, list)
            and all(isinstance(item, str) for item in raw_capabilities)
            else frozenset()
        )
        return NodeRuntimeContext(
            graph_run_id=state.run_id,
            node_name=node_name,
            config_version=config.config_version,
            tenant_id=self._string_context(state, "tenant_id"),
            capabilities=capabilities,
            config_snapshot_id=config.snapshot_id,
            prompt_config_version=node_policy.prompt_version,
            retry_index=state.retry_index,
            max_turns=node_policy.max_turns,
            model=node_policy.model,
            model_config=config.policy.model(node_policy.model),
            max_retries=config.policy.retry.max_retries,
            retry_initial_delay=config.policy.retry.initial_delay_seconds,
            retry_max_delay=config.policy.retry.max_delay_seconds,
            retry_multiplier=config.policy.retry.multiplier,
            retry_jitter=config.policy.retry.jitter,
            node_timeout=config.policy.model(node_policy.model).timeout,
            session_history_limit=config.policy.sessions.history_item_limit,
            profile=config.profile,
            tool_result_bytes=config.policy.tools.max_result_bytes,
            tools_enabled=config.policy.tools.enabled,
            tool_allowed_nodes=frozenset(config.policy.tools.allowed_nodes),
            tool_allowed_profiles=frozenset(config.policy.tools.allowed_profiles),
            tool_max_results=config.policy.tools.max_results,
            tool_timeout_seconds=config.policy.tools.timeout_seconds,
        )

    async def step(self, state: DiagnosisState, config: ResolvedRunConfig) -> str:
        """Compatibility helper; production Runtime uses prepare/apply directly."""
        plan = self.prepare_step(state, config)
        runtime = self._runtime_for_config(state, plan.node, config)
        result = await self.runner.run(plan.node, plan.input_model, runtime)
        new_state, _ = self.apply_result(state, plan, result)
        for field in new_state.model_fields:
            object.__setattr__(state, field, getattr(new_state, field))
        return plan.node

    async def run(
        self,
        question: str,
        context: dict[str, str | list[str]],
        evidence,
        clarifier: Clarifier,
        *,
        run_id: str | None = None,
        max_attempts: int = 2,
        max_clarification_rounds: int = 2,
    ) -> DiagnosisState:
        """Legacy in-memory facade retained for callers during the migration."""
        from .config import ConfigRepository

        config = ConfigRepository(None).resolve("default")
        state = DiagnosisState.create(
            question,
            context,
            evidence,
            max_attempts,
            max_clarification_rounds,
            config_snapshot_id=config.snapshot_id,
            profile="default",
            config_version=config.config_version,
        )
        if run_id:
            state = replace_state(state, run_id=run_id)
        state = replace_state(state, lifecycle_status=LifecycleStatus.RUNNING)
        with trace(
            "BugLens diagnosis",
            group_id=state.run_id,
            metadata={"prompt_version": self.prompts.version},
            disabled=not self.tracing_enabled,
        ):
            while state.lifecycle_status == LifecycleStatus.RUNNING:
                plan = self.prepare_step(state, config)
                runtime = self._runtime_for_config(state, plan.node, config)
                result = await self.runner.run(
                    plan.node,
                    plan.input_model,
                    runtime,
                    state.analysis.category
                    if state.analysis and plan.node == "investigate"
                    else None,
                )
                if plan.node == "evaluate":
                    state, _ = self.apply_evaluation_policy(state, plan, result, config)
                else:
                    state, _ = self.apply_result(state, plan, result)
                if state.lifecycle_status == LifecycleStatus.WAITING_USER:
                    request = state.pending_interaction
                    assert request is not None
                    if (
                        state.clarification_count(request.source_node)
                        >= state.max_clarification_rounds
                    ):
                        state = replace_state(
                            state,
                            pending_interaction=None,
                            lifecycle_status=LifecycleStatus.RUNNING,
                            current_node=GraphNode.SUMMARIZE,
                            outcome=DiagnosisOutcome.INCONCLUSIVE,
                        )
                        continue
                    answers = await clarifier.ask(request)
                    request.validate_answers(answers)
                    state = self.resume_with_answers(state, answers)
            return state

    @staticmethod
    def resume_with_answers(
        state: DiagnosisState, answers: list[UserAnswer]
    ) -> DiagnosisState:
        request = state.pending_interaction
        if state.lifecycle_status != LifecycleStatus.WAITING_USER or request is None:
            raise GraphContractError("state is not waiting for user input")
        request.validate_answers(answers)
        clean = [
            UserAnswer.model_validate(
                sanitize_data(answer.model_dump(mode="python"))
            ).model_copy(update={"source_node": request.source_node})
            for answer in answers
        ]
        rounds = dict(state.clarification_rounds)
        rounds[request.source_node] = rounds.get(request.source_node, 0) + 1
        return replace_state(
            state,
            answers=[*state.answers, *clean],
            clarification_rounds=rounds,
            clarification_round=sum(rounds.values()),
            next_node_input=ClarificationInput(answers=clean).model_dump(mode="python"),
            current_node=GraphNode(request.resume_node),
            pending_interaction=None,
            lifecycle_status=LifecycleStatus.RUNNING,
        )

    @staticmethod
    def skip_interaction(
        state: DiagnosisState, request_id: str, reason: str
    ) -> DiagnosisState:
        request = state.pending_interaction
        if state.lifecycle_status != LifecycleStatus.WAITING_USER or request is None:
            raise GraphContractError("state is not waiting for user input")
        if request.request_id != request_id:
            raise GraphContractError("pending request_id does not match")
        rounds = dict(state.clarification_rounds)
        rounds[request.source_node] = rounds.get(request.source_node, 0) + 1
        skipped = SkippedInteraction(
            request_id=request.request_id,
            source_node=request.source_node,
            question_ids=[question.id for question in request.questions],
            reason=str(sanitize_data(reason)),
        )
        return replace_state(
            state,
            clarification_rounds=rounds,
            clarification_round=sum(rounds.values()),
            skipped_interactions=[*state.skipped_interactions, skipped],
            next_node_input=ClarificationInput(
                information_unavailable=True,
                skipped_question_ids=skipped.question_ids,
            ).model_dump(mode="python"),
            current_node=GraphNode(request.resume_node),
            pending_interaction=None,
            lifecycle_status=LifecycleStatus.RUNNING,
        )
