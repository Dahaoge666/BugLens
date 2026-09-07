"""Committed, replayable domain events (never SDK objects or full state)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import Field

from ..models import (
    DiagnosisOutcome,
    FailureRecord,
    StrictModel,
    UserInteractionRequest,
)


class EventEnvelope(StrictModel):
    protocol_version: Literal["1"] = "1"
    event_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
    run_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    revision: int = Field(ge=0)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RunStarted(EventEnvelope):
    event_type: Literal["run_started"] = "run_started"
    profile: str
    config_snapshot_id: str
    config_version: str


class NodeStarted(EventEnvelope):
    event_type: Literal["node_started"] = "node_started"
    node: Literal["analyze", "investigate", "evaluate", "summarize"]


class NodeCompleted(EventEnvelope):
    event_type: Literal["node_completed"] = "node_completed"
    node: Literal["analyze", "investigate", "evaluate", "summarize"]
    next_node: str


class InputRequired(EventEnvelope):
    event_type: Literal["input_required"] = "input_required"
    request: UserInteractionRequest


class RunWaiting(EventEnvelope):
    event_type: Literal["run_waiting"] = "run_waiting"
    waiting_for: Literal["user", "tool", "approval"]


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
    | NodeStarted
    | NodeCompleted
    | InputRequired
    | RunWaiting
    | RunCompleted
    | RunFailed
    | RunCanceled
)


def event_to_json(event: AgentEvent) -> str:
    return event.model_dump_json()
