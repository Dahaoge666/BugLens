"""Committed, replayable domain events.

Events use a CloudEvents-shaped envelope in protocol v2 while retaining the
original BugLens field names as compatibility aliases.  No SDK objects, hidden
reasoning or unbounded model messages are ever placed in an event.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from ..models import (
    DiagnosisOutcome,
    EnvironmentTargetCandidate,
    ExecutionFailure,
    FailureRecord,
    PendingApproval,
    StrictModel,
    TargetSpec,
    UserInteractionRequest,
)


class EventEnvelope(StrictModel):
    protocol_version: Literal["1", "2"] = "2"
    event_id: str = Field(
        default_factory=lambda: uuid4().hex, min_length=1, max_length=128
    )
    run_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    revision: int = Field(ge=0)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # CloudEvents 1.0 names.  The validator mirrors these from the stable
    # BugLens aliases so constructors used by older adapters remain valid.
    specversion: Literal["1.0"] = "1.0"
    id: str | None = None
    source: str = "buglens"
    subject: str | None = None
    type: str | None = None
    time: datetime | None = None
    datacontenttype: Literal["application/json"] = "application/json"
    data: dict[str, Any] = Field(default_factory=dict)
    runid: str | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_names(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        data.setdefault("id", data.get("event_id"))
        data.setdefault("runid", data.get("run_id"))
        data.setdefault("time", data.get("occurred_at"))
        event_type = data.get("event_type")
        data.setdefault("type", f"com.buglens.{event_type}" if event_type else None)
        data.setdefault("subject", f"runs/{data.get('run_id', data.get('runid', ''))}")
        return data

    def model_post_init(self, __context: Any) -> None:
        event_type = getattr(self, "event_type", None)
        object.__setattr__(self, "id", self.id or self.event_id)
        object.__setattr__(self, "runid", self.runid or self.run_id)
        object.__setattr__(self, "time", self.time or self.occurred_at)
        object.__setattr__(self, "subject", self.subject or f"runs/{self.run_id}")
        object.__setattr__(
            self,
            "type",
            self.type
            or (f"com.buglens.{event_type}" if event_type else "com.buglens.event"),
        )
        if not self.data:
            excluded = {
                "protocol_version",
                "event_id",
                "run_id",
                "sequence",
                "revision",
                "occurred_at",
                "specversion",
                "id",
                "source",
                "subject",
                "type",
                "time",
                "datacontenttype",
                "data",
                "runid",
            }
            object.__setattr__(
                self, "data", self.model_dump(mode="json", exclude=excluded)
            )


class RunStarted(EventEnvelope):
    event_type: Literal["run_started"] = "run_started"
    profile: str
    config_snapshot_id: str
    config_version: str


class NodeAttemptStarted(EventEnvelope):
    event_type: Literal["node_attempt_started"] = "node_attempt_started"
    node: Literal["analyze", "investigate", "evaluate", "summarize"]
    execution_id: str
    session_id: str
    investigation_attempt: int = Field(ge=0)
    clarification_round: int = Field(ge=0)
    retry_cycle: int = Field(ge=0)
    retry_index: int = Field(ge=0)


class NodeStarted(EventEnvelope):
    """v1 event retained for old event logs and clients."""

    event_type: Literal["node_started"] = "node_started"
    node: Literal["analyze", "investigate", "evaluate", "summarize"]


class NodeRetryScheduled(EventEnvelope):
    event_type: Literal["node_retry_scheduled"] = "node_retry_scheduled"
    node: Literal["analyze", "investigate", "evaluate", "summarize"]
    execution_id: str
    retry_cycle: int = Field(ge=0)
    retry_index: int = Field(ge=0)
    delay_seconds: float = Field(ge=0)
    reason: str = Field(min_length=1, max_length=512)


class NodeAttemptFailed(EventEnvelope):
    event_type: Literal["node_attempt_failed"] = "node_attempt_failed"
    node: Literal["analyze", "investigate", "evaluate", "summarize"]
    execution_id: str
    failure: ExecutionFailure


class NodeCompleted(EventEnvelope):
    event_type: Literal["node_completed"] = "node_completed"
    node: Literal["analyze", "investigate", "evaluate", "summarize"]
    execution_id: str | None = None
    next_node: str


class ToolCallStarted(EventEnvelope):
    event_type: Literal["tool_call_started"] = "tool_call_started"
    node_execution_id: str
    tool_execution_id: str
    sdk_tool_call_id: str
    tool_name: str


class ToolCallCompleted(EventEnvelope):
    event_type: Literal["tool_call_completed"] = "tool_call_completed"
    node_execution_id: str
    tool_execution_id: str
    sdk_tool_call_id: str
    tool_name: str
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)
    duration_ms: int | None = Field(default=None, ge=0)


class ToolCallFailed(EventEnvelope):
    event_type: Literal["tool_call_failed"] = "tool_call_failed"
    node_execution_id: str
    tool_execution_id: str
    sdk_tool_call_id: str
    tool_name: str
    failure: ExecutionFailure


class TargetConfirmationRequired(EventEnvelope):
    event_type: Literal["target_confirmation_required"] = "target_confirmation_required"
    request_id: str = Field(min_length=1, max_length=128)
    requested_target: TargetSpec
    candidates: list[EnvironmentTargetCandidate] = Field(min_length=1, max_length=20)


class TargetConfirmed(EventEnvelope):
    event_type: Literal["target_confirmed"] = "target_confirmed"
    request_id: str | None = Field(default=None, max_length=128)
    environment_id: str = Field(min_length=1, max_length=128)
    primary_service_id: str | None = Field(default=None, max_length=128)
    environment_snapshot_id: str = Field(min_length=1, max_length=128)


class ToolApprovalRequired(EventEnvelope):
    event_type: Literal["tool_approval_required"] = "tool_approval_required"
    request: PendingApproval


class ToolApprovalResolved(EventEnvelope):
    event_type: Literal["tool_approval_resolved"] = "tool_approval_resolved"
    request_id: str
    decision: Literal["approved", "rejected"]


class InputRequired(EventEnvelope):
    event_type: Literal["input_required"] = "input_required"
    request: UserInteractionRequest


class InputSkipped(EventEnvelope):
    event_type: Literal["input_skipped"] = "input_skipped"
    request_id: str
    source_node: Literal["analyze", "investigate", "evaluate"]
    question_ids: list[str] = Field(min_length=1, max_length=3)
    reason: str = Field(min_length=1, max_length=512)


class RunWaiting(EventEnvelope):
    event_type: Literal["run_waiting"] = "run_waiting"
    waiting_for: Literal["user", "tool", "approval", "target"]


class RunResumeAvailable(EventEnvelope):
    event_type: Literal["run_resume_available"] = "run_resume_available"
    node: str
    retry_cycle: int = Field(ge=0)
    retry_index: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=512)


class RunResumed(EventEnvelope):
    event_type: Literal["run_resumed"] = "run_resumed"
    node: str
    retry_cycle: int = Field(ge=0)


class RunCancelRequested(EventEnvelope):
    event_type: Literal["run_cancel_requested"] = "run_cancel_requested"
    reason: str = Field(min_length=1, max_length=1_000)


class UserInputSubmitted(EventEnvelope):
    event_type: Literal["user_input_submitted"] = "user_input_submitted"
    request_id: str
    source_node: Literal["analyze", "investigate", "evaluate"]


class RunCompleted(EventEnvelope):
    event_type: Literal["run_completed"] = "run_completed"
    outcome: DiagnosisOutcome
    summary: str = Field(max_length=4_000)


class RunFailed(EventEnvelope):
    event_type: Literal["run_failed"] = "run_failed"
    failure: FailureRecord


class RunCanceled(EventEnvelope):
    event_type: Literal["run_canceled"] = "run_canceled"
    reason: str = Field(max_length=1_000)


AgentEvent = (
    RunStarted
    | NodeAttemptStarted
    | NodeStarted
    | NodeRetryScheduled
    | NodeAttemptFailed
    | NodeCompleted
    | ToolCallStarted
    | ToolCallCompleted
    | ToolCallFailed
    | TargetConfirmationRequired
    | TargetConfirmed
    | ToolApprovalRequired
    | ToolApprovalResolved
    | InputRequired
    | InputSkipped
    | RunWaiting
    | RunResumeAvailable
    | RunResumed
    | RunCancelRequested
    | UserInputSubmitted
    | RunCompleted
    | RunFailed
    | RunCanceled
)


def event_to_json(event: AgentEvent) -> str:
    return event.model_dump_json()
