"""Versioned commands. Transport DTOs must be converted to these models first."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import Field

from ..models import Evidence, StrictModel, UserAnswer


class CommandEnvelope(StrictModel):
    protocol_version: Literal["1"] = "1"
    command_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
    run_id: str = Field(min_length=1, max_length=128)
    expected_revision: int | None = Field(default=None, ge=0)
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StartDiagnosis(CommandEnvelope):
    command_type: Literal["start_diagnosis"] = "start_diagnosis"
    question: str = Field(min_length=1, max_length=12_000)
    context: dict[str, str | list[str]] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list, max_length=100)
    profile: str = Field(default="default", min_length=1, max_length=128)


class SubmitUserAnswers(CommandEnvelope):
    command_type: Literal["submit_user_answers"] = "submit_user_answers"
    request_id: str = Field(min_length=1, max_length=128)
    answers: list[UserAnswer] = Field(min_length=1, max_length=3)


class CancelDiagnosis(CommandEnvelope):
    command_type: Literal["cancel_diagnosis"] = "cancel_diagnosis"
    reason: str = Field(min_length=1, max_length=1_000)


AgentCommand = StartDiagnosis | SubmitUserAnswers | CancelDiagnosis


def command_from_json(value: str) -> AgentCommand:
    """Parse a command strictly after selecting its discriminating field."""
    from pydantic import TypeAdapter

    return TypeAdapter(AgentCommand).validate_json(value)
