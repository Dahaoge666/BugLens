"""Stable Command/Event protocol shared by every adapter."""

from .commands import (
    AgentCommand,
    CancelDiagnosis,
    ConfirmDiagnosisTarget,
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
    TargetConfirmationRequired,
    TargetConfirmed,
)

__all__ = [
    "AgentCommand",
    "CancelDiagnosis",
    "ConfirmDiagnosisTarget",
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
    "TargetConfirmationRequired",
    "TargetConfirmed",
]
