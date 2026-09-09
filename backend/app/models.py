from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import TypedDict

from .security import sanitize_data


class GraphContractError(ValueError):
    """Input or node output violates the diagnosis workflow contract."""


class StrictModel(BaseModel):
    """Base model for all data that crosses a BugLens boundary."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


ContextValue = str | int | float | bool | None | list[str]


class ProblemCategory(StrEnum):
    APPLICATION_ERROR = "application_error"
    PERFORMANCE = "performance"
    AVAILABILITY = "availability"
    DATA_CONSISTENCY = "data_consistency"
    CONFIGURATION = "configuration"
    INTEGRATION = "integration"
    SECURITY_ACCESS = "security_access"
    UNKNOWN = "unknown"


class LifecycleStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    WAITING_TOOL = "waiting_tool"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_TARGET_CONFIRMATION = "waiting_for_target_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class DiagnosisOutcome(StrEnum):
    CONFIRMED = "confirmed"
    INCONCLUSIVE = "inconclusive"


class TargetSpec(StrictModel):
    """User-supplied target selection or deterministic inference hint."""

    mode: Literal["explicit", "infer"] = "infer"
    environment_id: str | None = Field(default=None, max_length=128)
    primary_service_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_explicit_target(self) -> TargetSpec:
        if self.mode == "explicit" and not self.environment_id:
            raise ValueError("explicit target requires environment_id")
        return self


class ResolvedTarget(StrictModel):
    """A target accepted by the environment directory."""

    mode: Literal["explicit"] = "explicit"
    environment_id: str = Field(min_length=1, max_length=128)
    primary_service_id: str | None = Field(default=None, max_length=128)


class EnvironmentTargetCandidate(StrictModel):
    """A backend-generated, safe-to-display inference candidate."""

    environment_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=256)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    level: str = Field(default="unknown", max_length=64)
    region: str | None = Field(default=None, max_length=128)
    timezone: str = Field(default="UTC", max_length=128)
    matched_by: list[str] = Field(default_factory=list, max_length=10)


class PendingTargetConfirmation(StrictModel):
    """Durable target-confirmation checkpoint; it contains no credentials."""

    request_id: str = Field(min_length=1, max_length=128)
    requested_target: TargetSpec
    candidates: list[EnvironmentTargetCandidate] = Field(min_length=1, max_length=20)
    config_revision: str | None = Field(default=None, min_length=1, max_length=128)


class GraphNode(StrEnum):
    ANALYZE = "analyze"
    INVESTIGATE = "investigate"
    EVALUATE = "evaluate"
    SUMMARIZE = "summarize"
    DONE = "done"


NODE_NAMES = {node.value for node in GraphNode if node != GraphNode.DONE}
CLARIFICATION_NODES = ("analyze", "investigate", "evaluate")


class DiagnosisContext(StrictModel):
    """Bounded, extensible business context passed to every graph node."""

    environment: str | None = Field(default=None, max_length=256)
    system: str | None = Field(default=None, max_length=256)
    service: str | None = Field(default=None, max_length=256)
    component: str | None = Field(default=None, max_length=256)
    version: str | None = Field(default=None, max_length=256)
    deployment_id: str | None = Field(default=None, max_length=256)
    region: str | None = Field(default=None, max_length=256)
    host: str | None = Field(default=None, max_length=256)
    runtime: str | None = Field(default=None, max_length=256)
    incident_started_at: str | None = Field(default=None, max_length=128)
    timezone: str | None = Field(default=None, max_length=128)
    expected_behavior: str | None = Field(default=None, max_length=4_000)
    actual_behavior: str | None = Field(default=None, max_length=4_000)
    impact_scope: str | None = Field(default=None, max_length=2_000)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    # SDK tool schemas must be closed objects.  The runtime still validates
    # arbitrary bounded attributes in Python; native evaluator calls receive
    # the declared fields and do not invent new context keys.
    attributes: dict[str, ContextValue] = Field(
        default_factory=dict, json_schema_extra={"additionalProperties": False}
    )

    @model_validator(mode="after")
    def validate_bounds(self) -> DiagnosisContext:
        if len(self.attributes) > 50:
            raise ValueError("context attributes cannot exceed 50 entries")
        for key, value in self.attributes.items():
            if not key or len(key) > 128:
                raise ValueError("context attribute keys must be 1-128 characters")
            if isinstance(value, list) and len(value) > 20:
                raise ValueError("context attribute lists cannot exceed 20 items")
            if isinstance(value, str) and len(value) > 4_000:
                raise ValueError(
                    "context attribute strings cannot exceed 4000 characters"
                )
        return self

    @classmethod
    def from_legacy(cls, value: dict[str, ContextValue] | None) -> DiagnosisContext:
        raw = dict(value or {})
        known = {
            key: raw.pop(key)
            for key in (
                "environment",
                "system",
                "service",
                "component",
                "version",
                "deployment_id",
                "region",
                "host",
                "runtime",
                "incident_started_at",
                "timezone",
                "expected_behavior",
                "actual_behavior",
                "impact_scope",
                "constraints",
            )
            if key in raw
        }
        constraints = known.get("constraints", [])
        if isinstance(constraints, str):
            constraints = [constraints]
        known["constraints"] = constraints
        known["attributes"] = raw
        return cls.model_validate(known)

    def as_legacy(self) -> dict[str, ContextValue]:
        data = self.model_dump(exclude_none=True, mode="python")
        attributes = data.pop("attributes", {})
        if data.get("constraints") == []:
            data.pop("constraints", None)
        data.update(attributes)
        return data


class EvidenceRecord(StrictModel):
    """A stable, bounded and sanitized reference to an observed fact."""

    evidence_id: str = Field(
        default_factory=lambda: f"ev_{uuid4().hex}", min_length=1, max_length=128
    )
    source: str = Field(default="user", min_length=1, max_length=64)
    source_type: str = Field(default="", max_length=64)
    source_reference: str | None = Field(default=None, max_length=512)
    content: str = Field(min_length=1, max_length=12_000)
    reference: str | None = Field(default=None, max_length=512)
    content_hash: str = Field(default="", min_length=0, max_length=128)
    storage_reference: str | None = Field(default=None, max_length=512)
    observed_at: str | None = Field(default=None, max_length=128)
    collected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    tool_execution_id: str | None = Field(default=None, max_length=128)
    classification: str = Field(default="operational", max_length=64)
    retention_policy: str | None = Field(default=None, max_length=128)
    # The runtime still accepts a bounded metadata map.  Mark it closed in the
    # JSON Schema so Agents SDK strict structured-output validation does not
    # mistake this deliberately bounded extension point for an unbounded object.
    metadata: dict[str, ContextValue] = Field(
        default_factory=dict, json_schema_extra={"additionalProperties": False}
    )

    @model_validator(mode="after")
    def normalize_identity(self) -> EvidenceRecord:
        source_type = self.source_type or self.source
        source_reference = self.source_reference or self.reference
        content_hash = (
            self.content_hash
            or hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        )
        object.__setattr__(self, "source_type", source_type)
        object.__setattr__(self, "source_reference", source_reference)
        object.__setattr__(self, "content_hash", content_hash)
        return self


# The short name remains part of the original public API.
Evidence = EvidenceRecord


class FailureRecord(StrictModel):
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=1_000)
    node: GraphNode | None = None
    retryable: bool = False
    retry_cycle: int = Field(default=0, ge=0)
    retry_index: int = Field(default=0, ge=0)
    resume_available: bool = False
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PendingToolRequest(StrictModel):
    request_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class PendingApproval(StrictModel):
    request_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=128)
    explanation: str = Field(min_length=1, max_length=2_000)
    # The SDK RunState is the authoritative source for the exact interruption.
    # These bounded fields are only the public display/command correlation data.
    tool_call_ids: list[str] = Field(default_factory=list, max_length=20)
    arguments: dict[str, ContextValue] = Field(default_factory=dict)


class ClarificationQuestion(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=2_000)
    rationale: str = Field(min_length=1, max_length=2_000)
    required: bool = True
    answer_type: Literal["text", "single_select", "multi_select", "evidence_upload"] = (
        "text"
    )
    options: list[str] = Field(default_factory=list, max_length=20)


class UserAnswer(StrictModel):
    request_id: str = Field(min_length=1, max_length=128)
    question_id: str = Field(min_length=1, max_length=128)
    answer: str = Field(default="", max_length=12_000)
    attachments: list[EvidenceRecord] = Field(default_factory=list, max_length=10)
    source_node: Literal["analyze", "investigate", "evaluate"] | None = None
    information_unavailable: bool = False


class UserInteractionRequest(StrictModel):
    request_id: str = Field(min_length=1, max_length=128)
    source_node: Literal["analyze", "investigate", "evaluate"]
    resume_node: Literal["analyze", "investigate"]
    reason: Literal[
        "missing_problem_context",
        "insufficient_evidence",
        "tool_unavailable",
        "tool_failed",
        "user_explanation_required",
        "no_progress",
        "ambiguous_investigation_direction",
    ]
    explanation: str = Field(min_length=1, max_length=4_000)
    questions: list[ClarificationQuestion] = Field(min_length=1, max_length=3)

    def validate_node(self, node: str) -> None:
        if self.source_node != node:
            raise GraphContractError("Interaction request source node does not match")
        if self.source_node == "evaluate" and self.resume_node != "investigate":
            raise GraphContractError("Evaluate clarification must resume investigate")
        if self.source_node != "evaluate" and self.resume_node != self.source_node:
            raise GraphContractError(
                "Analyze and Investigate clarification must resume their source node"
            )
        if len({q.id for q in self.questions}) != len(self.questions):
            raise GraphContractError("Clarification question IDs must be unique")

    def validate_answers(self, answers: list[UserAnswer]) -> None:
        valid_ids = {question.id for question in self.questions}
        supplied_ids = {answer.question_id for answer in answers}
        if len(supplied_ids) != len(answers):
            raise GraphContractError("Duplicate question IDs are not allowed")
        questions = {question.id: question for question in self.questions}
        for answer in answers:
            if not answer.answer.strip() and not answer.attachments:
                raise GraphContractError("An answer or evidence attachment is required")
            question = questions.get(answer.question_id)
            if (
                question
                and question.answer_type == "single_select"
                and answer.answer not in question.options
            ):
                raise GraphContractError("Answer must match an offered option")
            if question and question.answer_type == "multi_select":
                try:
                    selected = json.loads(answer.answer)
                except (TypeError, ValueError) as exc:
                    raise GraphContractError(
                        "Multiple selections must be a JSON array"
                    ) from exc
                if (
                    not isinstance(selected, list)
                    or not selected
                    or any(item not in question.options for item in selected)
                ):
                    raise GraphContractError("Answer must match offered options")
        if any(answer.request_id != self.request_id for answer in answers):
            raise GraphContractError("Answers must use the pending request_id")
        if not supplied_ids.issubset(valid_ids) or not supplied_ids:
            raise GraphContractError(
                "Answers must use question IDs in the pending request"
            )
        required_ids = {question.id for question in self.questions if question.required}
        if not required_ids.issubset(supplied_ids):
            raise GraphContractError(
                "All required clarification questions need an answer"
            )


class SkippedInteraction(StrictModel):
    request_id: str = Field(min_length=1, max_length=128)
    source_node: Literal["analyze", "investigate", "evaluate"]
    question_ids: list[str] = Field(min_length=1, max_length=3)
    reason: str = Field(default="user_skipped", min_length=1, max_length=512)
    skipped_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ProblemAnalysis(StrictModel):
    category: ProblemCategory
    category_confidence: float = Field(ge=0, le=1)
    summary: str = Field(min_length=1, max_length=8_000)
    symptoms: list[str] = Field(default_factory=list, max_length=50)
    impact: str | None = Field(default=None, max_length=4_000)
    time_window: str | None = Field(default=None, max_length=512)
    environment: str | None = Field(default=None, max_length=256)
    extracted_evidence: list[EvidenceRecord] = Field(
        default_factory=list, max_length=100
    )
    missing_information: list[str] = Field(default_factory=list, max_length=50)
    interaction_request: UserInteractionRequest | None = None


class RootCauseHypothesis(StrictModel):
    rank: int = Field(ge=1, le=3)
    cause: str = Field(min_length=1, max_length=4_000)
    rationale: str = Field(min_length=1, max_length=8_000)
    supporting_evidence: list[str] = Field(min_length=1, max_length=20)
    contradicting_evidence: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0, le=1)
    verification_steps: list[str] = Field(min_length=1, max_length=20)
    remediation_direction: str | None = Field(default=None, max_length=4_000)


class ProgressDelta(StrictModel):
    """Machine-checkable progress produced by an investigation cycle."""

    new_evidence_ids: list[str] = Field(default_factory=list, max_length=50)
    resolved_gap_ids: list[str] = Field(default_factory=list, max_length=50)
    changed_hypothesis_ids: list[str] = Field(default_factory=list, max_length=50)
    discarded_hypothesis_ids: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_ids(self) -> ProgressDelta:
        for field_name in (
            "new_evidence_ids",
            "resolved_gap_ids",
            "changed_hypothesis_ids",
            "discarded_hypothesis_ids",
        ):
            values = getattr(self, field_name)
            if any(not value.strip() for value in values):
                raise ValueError(f"{field_name} cannot contain blank IDs")
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} cannot contain duplicate IDs")
        return self

    @property
    def has_ids(self) -> bool:
        return bool(
            self.new_evidence_ids
            or self.resolved_gap_ids
            or self.changed_hypothesis_ids
            or self.discarded_hypothesis_ids
        )


class InvestigationResult(StrictModel):
    investigation_summary: str = Field(min_length=1, max_length=8_000)
    hypotheses: list[RootCauseHypothesis] = Field(default_factory=list, max_length=3)
    primary_conclusion: str | None = Field(default=None, max_length=4_000)
    evidence_gaps: list[str] = Field(default_factory=list, max_length=50)
    next_data_to_collect: list[str] = Field(default_factory=list, max_length=50)
    limitations: list[str] = Field(default_factory=list, max_length=50)
    progress_delta: ProgressDelta = Field(default_factory=ProgressDelta)
    interaction_request: UserInteractionRequest | None = None


class CriteriaScores(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")
    problem_coverage: Annotated[int, Field(ge=0, le=20)]
    evidence_traceability: Annotated[int, Field(ge=0, le=25)]
    reasoning_consistency: Annotated[int, Field(ge=0, le=20)]
    verification_executability: Annotated[int, Field(ge=0, le=20)]
    uncertainty_expression: Annotated[int, Field(ge=0, le=15)]


class EvaluationResult(StrictModel):
    passed: bool
    score: int = Field(ge=0, le=100)
    criteria_scores: CriteriaScores
    strengths: list[str] = Field(default_factory=list, max_length=50)
    deficiencies: list[str] = Field(default_factory=list, max_length=50)
    retry_guidance: list[str] = Field(default_factory=list, max_length=3)
    interaction_request: UserInteractionRequest | None = None

    def enforce_rubric(
        self,
        passing_score: int = 75,
        min_evidence_traceability: int = 15,
        min_verification_executability: int = 15,
    ) -> EvaluationResult:
        """Recompute the total; an evaluator rejection always remains a rejection."""
        score = sum(self.criteria_scores.values())
        passed = (
            self.passed
            and score >= passing_score
            and self.criteria_scores["evidence_traceability"]
            >= min_evidence_traceability
            and self.criteria_scores["verification_executability"]
            >= min_verification_executability
        )
        guidance = self.retry_guidance or (
            [] if passed else ["补充可追溯证据与明确验证步骤，并完整填写五项评分。"]
        )
        return self.model_copy(
            update={"passed": passed, "score": score, "retry_guidance": guidance}
        )


class DiagnosisReport(StrictModel):
    executive_summary: str = Field(min_length=1, max_length=8_000)
    primary_conclusion: str | None = Field(default=None, max_length=4_000)
    status_explanation: str = Field(min_length=1, max_length=8_000)
    next_actions: list[str] = Field(default_factory=list, max_length=50)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    limitations: list[str] = Field(default_factory=list, max_length=50)


class DiagnosisState(StrictModel):
    run_id: str = Field(min_length=1, max_length=128)
    user_question: str = Field(min_length=1, max_length=12_000)
    context: DiagnosisContext = Field(default_factory=DiagnosisContext)
    # Compatibility field for v1 callers; context is canonical in new code.
    user_context: dict[str, ContextValue] = Field(default_factory=dict)
    source_evidence: list[EvidenceRecord] = Field(default_factory=list, max_length=100)
    analysis: ProblemAnalysis | None = None
    investigation: InvestigationResult | None = None
    evaluation: EvaluationResult | None = None
    report: DiagnosisReport | None = None
    answers: list[UserAnswer] = Field(default_factory=list, max_length=100)
    clarification_rounds: dict[str, int] = Field(default_factory=dict)
    clarification_round: int = Field(default=0, ge=0)
    max_clarification_rounds: int = Field(default=2, ge=0, le=10)
    attempt: int = Field(default=0, ge=0, le=10)
    max_attempts: int = Field(default=2, ge=1, le=10)
    retry_cycle: int = Field(default=0, ge=0)
    retry_index: int = Field(default=0, ge=0)
    lifecycle_status: LifecycleStatus = LifecycleStatus.CREATED
    outcome: DiagnosisOutcome | None = None
    current_node: GraphNode = GraphNode.ANALYZE
    pending_interaction: UserInteractionRequest | None = None
    pending_tool: PendingToolRequest | None = None
    pending_approval: PendingApproval | None = None
    target: TargetSpec = Field(default_factory=TargetSpec)
    pending_target_confirmation: PendingTargetConfirmation | None = None
    # This ID points to ``run_environment_snapshots``.  It is intentionally
    # nullable so legacy runs never gain access to external data sources.
    environment_snapshot_id: str | None = Field(default=None, max_length=128)
    revision: int = Field(default=0, ge=0)
    schema_version: int = Field(default=3, ge=1)
    config_snapshot_id: str = Field(default="pending", min_length=1, max_length=128)
    config_profile: str = Field(default="default", min_length=1, max_length=128)
    config_version: str = Field(default="default-v1", min_length=1, max_length=128)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_error: FailureRecord | None = None
    resume_available: bool = False
    active_execution_id: str | None = Field(default=None, max_length=128)
    skipped_interactions: list[SkippedInteraction] = Field(
        default_factory=list, max_length=100
    )
    progress_deltas: list[ProgressDelta] = Field(default_factory=list, max_length=20)
    no_progress_cycles: int = Field(default=0, ge=0, le=20)
    cancel_requested_at: datetime | None = None
    # Serialized typed input for resuming a deterministic graph step; never SDK messages.
    next_node_input: dict[str, Any] | None = None
    # Native Agents SDK orchestration state.  These fields identify the active
    # specialist between short application-level turns; they are not SDK
    # messages and remain safe to persist in the business checkpoint.
    active_agent: str | None = Field(default=None, max_length=128)
    review_count: int = Field(default=0, ge=0, le=10)
    reviewed_candidate_hash: str | None = Field(default=None, max_length=128)
    available_actions: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_lifecycle_invariants(self) -> DiagnosisState:
        context_values = self.context.model_dump(
            exclude_none=True, exclude_defaults=True
        )
        if not context_values and self.user_context:
            object.__setattr__(
                self, "context", DiagnosisContext.from_legacy(self.user_context)
            )
        elif not self.user_context and context_values:
            object.__setattr__(self, "user_context", self.context.as_legacy())

        rounds = {
            node: int(self.clarification_rounds.get(node, 0))
            for node in CLARIFICATION_NODES
        }
        if not any(rounds.values()) and self.clarification_round:
            node = self.current_node.value
            if node not in rounds:
                node = "analyze"
            rounds[node] = self.clarification_round
        if any(
            value < 0 or value > self.max_clarification_rounds
            for value in rounds.values()
        ):
            raise ValueError(
                "clarification rounds exceed the configured per-node budget"
            )
        object.__setattr__(self, "clarification_rounds", rounds)
        object.__setattr__(self, "clarification_round", sum(rounds.values()))

        pending = [
            self.pending_interaction,
            self.pending_tool,
            self.pending_approval,
            self.pending_target_confirmation,
        ]
        if sum(item is not None for item in pending) > 1:
            raise ValueError("at most one pending interaction/tool/approval is allowed")
        waiting_map = {
            LifecycleStatus.WAITING_USER: self.pending_interaction,
            LifecycleStatus.WAITING_TOOL: self.pending_tool,
            LifecycleStatus.WAITING_APPROVAL: self.pending_approval,
            LifecycleStatus.WAITING_TARGET_CONFIRMATION: self.pending_target_confirmation,
        }
        if (
            self.lifecycle_status in waiting_map
            and waiting_map[self.lifecycle_status] is None
        ):
            raise ValueError("waiting status requires its pending request")
        if self.lifecycle_status in {
            LifecycleStatus.RUNNING,
            LifecycleStatus.COMPLETED,
            LifecycleStatus.FAILED,
            LifecycleStatus.CANCELED,
        } and any(item is not None for item in pending):
            raise ValueError("running and terminal states cannot have pending work")
        if self.lifecycle_status == LifecycleStatus.COMPLETED and self.outcome is None:
            raise ValueError("completed state requires a diagnosis outcome")
        if self.lifecycle_status == LifecycleStatus.FAILED:
            if self.last_error is None:
                raise ValueError("failed state requires a failure record")
            if self.resume_available and not self.last_error.retryable:
                raise ValueError("only retryable failures can offer Resume")
        if (
            self.lifecycle_status == LifecycleStatus.CANCELED
            and self.outcome is not None
        ):
            raise ValueError("canceled state cannot have a diagnosis outcome")
        if self.current_node == GraphNode.DONE and self.lifecycle_status not in {
            LifecycleStatus.COMPLETED,
            LifecycleStatus.FAILED,
            LifecycleStatus.CANCELED,
        }:
            raise ValueError("done cursor is only valid for a terminal state")

        if self.lifecycle_status == LifecycleStatus.WAITING_USER:
            actions = ["submit_answers", "skip_input", "cancel"]
        elif self.lifecycle_status == LifecycleStatus.WAITING_APPROVAL:
            actions = ["approve", "reject", "cancel"]
        elif self.lifecycle_status == LifecycleStatus.WAITING_TARGET_CONFIRMATION:
            actions = ["confirm_target", "cancel"]
        elif self.lifecycle_status == LifecycleStatus.FAILED and self.resume_available:
            actions = ["resume"]
        elif self.lifecycle_status == LifecycleStatus.RUNNING:
            actions = ["cancel"]
        else:
            actions = []
        object.__setattr__(self, "available_actions", actions)
        return self

    @property
    def status(self) -> str:
        """Compatibility view for the original CLI/API shape."""
        if self.lifecycle_status == LifecycleStatus.COMPLETED:
            return (
                "inconclusive"
                if self.outcome == DiagnosisOutcome.INCONCLUSIVE
                else "completed"
            )
        return self.lifecycle_status.value

    @property
    def evidence(self) -> list[EvidenceRecord]:
        return self.source_evidence

    def clarification_count(self, node: str) -> int:
        return self.clarification_rounds.get(node, 0)

    @classmethod
    def create(
        cls,
        question: str,
        context: dict[str, ContextValue] | DiagnosisContext,
        evidence: list[EvidenceRecord],
        max_attempts: int = 2,
        max_clarification_rounds: int = 2,
        *,
        config_snapshot_id: str = "pending",
        profile: str = "default",
        config_version: str = "default-v1",
        target: TargetSpec | None = None,
    ) -> DiagnosisState:
        """Validate and redact all user data before it enters graph state."""
        raw_context = (
            context.model_dump(mode="python")
            if isinstance(context, DiagnosisContext)
            else context
        )
        clean_context = sanitize_data(raw_context)
        if isinstance(context, DiagnosisContext):
            canonical_context = DiagnosisContext.model_validate(clean_context)
            clean_context = canonical_context.as_legacy()
        else:
            canonical_context = DiagnosisContext.from_legacy(clean_context)
        clean_evidence: list[dict[str, Any]] = []
        for item in evidence:
            record = EvidenceRecord.model_validate(item)
            record_data = dict(sanitize_data(record.model_dump(mode="python")))
            # The hash must describe the stored, redacted content rather than
            # an unsanitized value supplied by a caller.
            record_data["content_hash"] = ""
            clean_evidence.append(
                EvidenceRecord.model_validate(record_data).model_dump(mode="python")
            )
        clean = sanitize_data(
            {
                "run_id": f"diag_{uuid4().hex}",
                "user_question": question,
                "context": canonical_context.model_dump(mode="python"),
                "user_context": clean_context,
                "source_evidence": clean_evidence,
                "max_attempts": max_attempts,
                "max_clarification_rounds": max_clarification_rounds,
                "config_snapshot_id": config_snapshot_id,
                "config_profile": profile,
                "config_version": config_version,
                "schema_version": 3,
                "target": target or TargetSpec(),
            }
        )
        return cls.model_validate(clean)


class AnalyzeInput(StrictModel):
    question: str = Field(min_length=1, max_length=12_000)
    context: DiagnosisContext = Field(default_factory=DiagnosisContext)
    evidence: list[EvidenceRecord] = Field(default_factory=list, max_length=100)
    target: ResolvedTarget | None = None
    environment_hint: str | None = Field(default=None, max_length=256)
    clarification_answers: list[UserAnswer] = Field(default_factory=list, max_length=20)


class InvestigationInput(StrictModel):
    analysis: ProblemAnalysis
    context: DiagnosisContext = Field(default_factory=DiagnosisContext)
    evidence: list[EvidenceRecord] = Field(default_factory=list, max_length=100)
    target: ResolvedTarget | None = None
    previous_evaluation: EvaluationResult | None = None
    previous_investigation: InvestigationResult | None = None
    clarification_answers: list[UserAnswer] = Field(default_factory=list, max_length=20)
    prompt_config_version: str = Field(default="default-v1", max_length=128)
    investigation_attempt: int = Field(default=1, ge=1)


class EvaluationInput(StrictModel):
    analysis: ProblemAnalysis
    investigation: InvestigationResult
    context: DiagnosisContext = Field(default_factory=DiagnosisContext)
    evidence: list[EvidenceRecord] = Field(default_factory=list, max_length=100)
    target: ResolvedTarget | None = None
    previous_evaluation: EvaluationResult | None = None
    progress_history: list[ProgressDelta] = Field(default_factory=list, max_length=20)
    rubric_version: str = Field(default="rubric-v1", max_length=128)


class SummaryInput(StrictModel):
    analysis: ProblemAnalysis
    investigation: InvestigationResult | None = None
    evaluation: EvaluationResult | None = None
    context: DiagnosisContext = Field(default_factory=DiagnosisContext)
    evidence: list[EvidenceRecord] = Field(default_factory=list, max_length=100)
    target: ResolvedTarget | None = None
    skipped_interactions: list[SkippedInteraction] = Field(
        default_factory=list, max_length=100
    )
    status: Literal["completed", "inconclusive"]
    attempts: int = Field(ge=0)
    limitations: list[str] = Field(default_factory=list, max_length=50)


class ClarificationInput(StrictModel):
    answers: list[UserAnswer] = Field(default_factory=list, max_length=3)
    information_unavailable: bool = False
    skipped_question_ids: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def require_answer_or_skip(self) -> ClarificationInput:
        if not self.answers and not self.information_unavailable:
            raise ValueError(
                "clarification input requires answers or information_unavailable"
            )
        return self


class RetryInput(StrictModel):
    evaluation: EvaluationResult
    previous_investigation: InvestigationResult | None = None


class InvestigationBrief(StrictModel):
    """Bounded handoff payload from triage to a category specialist."""

    category: ProblemCategory
    category_confidence: float = Field(ge=0, le=1)
    normalized_summary: str = Field(min_length=1, max_length=8_000)
    symptoms: list[str] = Field(default_factory=list, max_length=50)
    impact: str | None = Field(default=None, max_length=4_000)
    environment: str | None = Field(default=None, max_length=256)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    missing_information: list[str] = Field(default_factory=list, max_length=50)


class NativeDiagnosisInput(StrictModel):
    """The application-owned input for one native SDK turn.

    The SDK Session stores the conversational transcript.  This model carries
    the current business snapshot explicitly so a new process can resume a
    turn without relying on Graph messages or an implicit model memory.
    """

    question: str = Field(min_length=1, max_length=12_000)
    context: DiagnosisContext = Field(default_factory=DiagnosisContext)
    evidence: list[EvidenceRecord] = Field(default_factory=list, max_length=100)
    target: ResolvedTarget | None = None
    answers: list[UserAnswer] = Field(default_factory=list, max_length=100)
    analysis: ProblemAnalysis | None = None
    investigation: InvestigationResult | None = None
    evaluation: EvaluationResult | None = None
    clarification: ClarificationInput | None = None
    active_agent: str | None = Field(default=None, max_length=128)
    review_count: int = Field(default=0, ge=0, le=10)
    max_review_count: int = Field(default=2, ge=1, le=10)
    rubric_version: str = Field(default="rubric-v1", max_length=128)
    passing_score: int = Field(default=75, ge=0, le=100)
    min_evidence_traceability: int = Field(default=15, ge=0, le=25)
    min_verification_executability: int = Field(default=15, ge=0, le=20)


class DiagnosisTurnResult(StrictModel):
    """Common structured output shared by triage and investigators."""

    kind: Literal["completed", "needs_input"]
    analysis: ProblemAnalysis | None = None
    investigation: InvestigationResult | None = None
    evaluation: EvaluationResult | None = None
    report: DiagnosisReport | None = None
    interaction_request: UserInteractionRequest | None = None
    active_agent: str | None = Field(default=None, max_length=128)
    review_count: int = Field(default=0, ge=0, le=10)

    @model_validator(mode="after")
    def validate_kind(self) -> DiagnosisTurnResult:
        if self.kind == "needs_input":
            if self.interaction_request is None:
                raise ValueError("needs_input result requires interaction_request")
            if self.report is not None:
                raise ValueError("needs_input result cannot include a report")
        elif self.report is None:
            raise ValueError("completed result requires report")
        return self


class NodeExecutionPlan(StrictModel):
    """Pure, serialization-safe description of one external node call."""

    execution_id: str = Field(default_factory=lambda: f"nexec_{uuid4().hex}")
    node: Literal["analyze", "investigate", "evaluate", "summarize"]
    input_model: Any
    session_id: str = Field(min_length=1, max_length=256)
    investigation_attempt: int = Field(default=0, ge=0)
    clarification_round: int = Field(default=0, ge=0)
    retry_cycle: int = Field(default=0, ge=0)
    retry_index: int = Field(default=0, ge=0)
    config_snapshot_id: str = Field(min_length=1, max_length=128)
    config_version: str = Field(min_length=1, max_length=128)
    prompt_config_version: str = Field(min_length=1, max_length=128)
    agent_definition_version: str = Field(default="unknown", max_length=128)


class ExecutionFailure(StrictModel):
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=1_000)
    retryable: bool = False
    # False means an inner SDK/runner retry budget was already consumed. The
    # outer Runtime may still expose Resume when retryable is true.
    auto_retry: bool = True


class GraphTransition(StrictModel):
    node: Literal["analyze", "investigate", "evaluate", "summarize"]
    next_node: GraphNode
    lifecycle_status: LifecycleStatus
    outcome: DiagnosisOutcome | None = None
    interaction_request: UserInteractionRequest | None = None
    error: ExecutionFailure | None = None


def replace_state(state: DiagnosisState, **updates: Any) -> DiagnosisState:
    """Create and fully validate an immutable-style state transition."""
    data = state.model_dump(mode="python")
    # RunView adds a UI-only projection that is deliberately not part of the
    # persisted DiagnosisState.  Keep state transitions compatible with a
    # RunView returned by the runtime.
    data.pop("environment_snapshot", None)
    data.update(updates)
    return DiagnosisState.model_validate(data)
