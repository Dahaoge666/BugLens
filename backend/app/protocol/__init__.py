"""Stable Command/Event protocol shared by every adapter."""

from .commands import (
    AgentCommand,
    CancelDiagnosis,
    StartDiagnosis,
    SubmitUserAnswers,
)
from .events import (
    AgentEvent,
    InputRequired,
    NodeCompleted,
    NodeStarted,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunStarted,
    RunWaiting,
)

__all__ = [
    "AgentCommand",
    "CancelDiagnosis",
    "SubmitUserAnswers",
    "StartDiagnosis",
    "AgentEvent",
    "InputRequired",
    "NodeCompleted",
    "NodeStarted",
    "RunCanceled",
    "RunCompleted",
    "RunFailed",
    "RunStarted",
    "RunWaiting",
]
