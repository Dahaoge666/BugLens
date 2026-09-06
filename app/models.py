from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict

from .security import sanitize_data


class GraphContractError(ValueError):
    """Input or node output violates the diagnosis workflow contract."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProblemCategory(StrEnum):
    APPLICATION_ERROR = "application_error"
    PERFORMANCE = "performance"
    AVAILABILITY = "availability"
    DATA_CONSISTENCY = "data_consistency"
    CONFIGURATION = "configuration"
    INTEGRATION = "integration"
    SECURITY_ACCESS = "security_access"
    UNKNOWN = "unknown"


class Evidence(StrictModel):
    source: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=12_000)
    reference: str | None = Field(default=None, max_length=512)
    observed_at: str | None = Field(default=None, max_length=128)


class ClarificationQuestion(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=2_000)
    rationale: str = Field(min_length=1, max_length=2_000)
    required: bool = True
    answer_type: Literal["text", "single_select", "multi_select", "evidence_upload"] = (
        "text"
    )
    options: list[str] = Field(default_factory=list, max_length=20)


class UserInteractionRequest(StrictModel):
    request_id: str = Field(min_length=1, max_length=128)
    source_node: Literal["analyze", "investigate"]
    resume_node: Literal["analyze", "investigate"]
    reason: Literal[
        "missing_problem_context",
        "insufficient_evidence",
        "tool_unavailable",
        "tool_failed",
        "ambiguous_investigation_direction",
    ]
    explanation: str = Field(min_length=1, max_length=4_000)
    questions: list[ClarificationQuestion] = Field(min_length=1, max_length=3)

    def validate_node(self, node: str) -> None:
        if self.source_node != node or self.resume_node != node:
            raise GraphContractError(
                "Interaction request must be resumed by its source node"
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
                except ValueError as exc:
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


class UserAnswer(StrictModel):
    request_id: str = Field(min_length=1, max_length=128)
    question_id: str = Field(min_length=1, max_length=128)
    answer: str = Field(default="", max_length=12_000)
    attachments: list[Evidence] = Field(default_factory=list, max_length=10)


class ProblemAnalysis(StrictModel):
    category: ProblemCategory
    category_confidence: float = Field(ge=0, le=1)
    summary: str
    symptoms: list[str] = Field(default_factory=list)
    impact: str | None = None
    time_window: str | None = None
    environment: str | None = None
    extracted_evidence: list[Evidence] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    interaction_request: UserInteractionRequest | None = None


class RootCauseHypothesis(StrictModel):
    rank: int = Field(ge=1, le=3)
    cause: str
    rationale: str
    supporting_evidence: list[str] = Field(min_length=1)
    contradicting_evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    verification_steps: list[str] = Field(min_length=1)
    remediation_direction: str | None = None


class InvestigationResult(StrictModel):
    investigation_summary: str
    hypotheses: list[RootCauseHypothesis] = Field(default_factory=list, max_length=3)
    primary_conclusion: str | None = None
    evidence_gaps: list[str] = Field(default_factory=list)
    next_data_to_collect: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
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
    strengths: list[str] = Field(default_factory=list)
    deficiencies: list[str] = Field(default_factory=list)
    retry_guidance: list[str] = Field(default_factory=list, max_length=3)

    def enforce_rubric(self) -> EvaluationResult:
        """Recompute the total; an evaluator rejection always remains a rejection."""
        score = sum(self.criteria_scores.values())
        passed = (
            self.passed
            and score >= 75
            and self.criteria_scores["evidence_traceability"] >= 15
            and self.criteria_scores["verification_executability"] >= 15
        )
        guidance = self.retry_guidance or (
            [] if passed else ["补充可追溯证据与明确验证步骤，并完整填写五项评分。"]
        )
        return self.model_copy(
            update={"passed": passed, "score": score, "retry_guidance": guidance}
        )


class DiagnosisReport(StrictModel):
    executive_summary: str
    primary_conclusion: str | None = None
    status_explanation: str
    next_actions: list[str] = Field(default_factory=list)


class DiagnosisState(StrictModel):
    run_id: str
    user_question: str
    user_context: dict[str, str | list[str]] = Field(default_factory=dict)
    source_evidence: list[Evidence] = Field(default_factory=list)
    analysis: ProblemAnalysis | None = None
    investigation: InvestigationResult | None = None
    evaluation: EvaluationResult | None = None
    report: DiagnosisReport | None = None
    pending_interaction: UserInteractionRequest | None = None
    answers: list[UserAnswer] = Field(default_factory=list)
    clarification_round: int = 0
    max_clarification_rounds: int = Field(default=2, ge=0, le=10)
    attempt: int = 0
    max_attempts: int = Field(default=2, ge=1, le=2)
    status: Literal["running", "awaiting_user_input", "completed", "inconclusive"] = (
        "running"
    )

    @classmethod
    def create(
        cls,
        question: str,
        context: dict[str, str | list[str]],
        evidence: list[Evidence],
        max_attempts: int = 2,
        max_clarification_rounds: int = 2,
    ) -> DiagnosisState:
        """Validate and redact all user data before it enters graph state."""
        return cls.model_validate(
            sanitize_data(
                {
                    "run_id": f"diag_{uuid4().hex}",
                    "user_question": question,
                    "user_context": context,
                    "source_evidence": [item.model_dump() for item in evidence],
                    "max_attempts": max_attempts,
                    "max_clarification_rounds": max_clarification_rounds,
                }
            )
        )


class AnalyzeInput(StrictModel):
    question: str
    evidence: list[Evidence]
    environment_hint: str | None = None
    clarification_answers: list[UserAnswer] = Field(default_factory=list)


class InvestigationInput(StrictModel):
    analysis: ProblemAnalysis
    evidence: list[Evidence]
    previous_evaluation: EvaluationResult | None = None
    clarification_answers: list[UserAnswer] = Field(default_factory=list)
    prompt_config_version: str


class EvaluationInput(StrictModel):
    analysis: ProblemAnalysis
    investigation: InvestigationResult
    rubric_version: str


class SummaryInput(StrictModel):
    analysis: ProblemAnalysis
    investigation: InvestigationResult | None = None
    evaluation: EvaluationResult | None = None
    status: Literal["completed", "inconclusive"]
    attempts: int


class CreateDiagnosisRequest(StrictModel):
    question: str = Field(min_length=1, max_length=12_000)
    context: dict[str, str | list[str]] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list, max_length=30)
    max_attempts: int = Field(default=2, ge=1, le=2)
    max_clarification_rounds: int = Field(default=2, ge=0, le=10)


class SubmitAnswer(StrictModel):
    question_id: str = Field(min_length=1, max_length=128)
    answer: str = Field(default="", max_length=12_000)
    attachments: list[Evidence] = Field(default_factory=list, max_length=10)


class SubmitAnswersRequest(StrictModel):
    request_id: str
    answers: list[SubmitAnswer] = Field(min_length=1, max_length=3)
