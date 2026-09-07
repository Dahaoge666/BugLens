"""Application service: profile selection and the shared runtime boundary."""

from __future__ import annotations

from typing import AsyncIterator, Protocol

from .config import ConfigRepository
from .models import StrictModel
from .protocol.commands import AgentCommand, StartDiagnosis
from .protocol.events import AgentEvent
from .runtime import DiagnosisRuntime, RunView


class Identity(StrictModel):
    subject: str = "local"
    tenant_id: str | None = None


class ApplicationService:
    def __init__(self, runtime: DiagnosisRuntime, configs: ConfigRepository) -> None:
        self.runtime = runtime
        self.configs = configs

    async def send(
        self, command: AgentCommand, identity: Identity | None = None
    ) -> AsyncIterator[AgentEvent]:
        # Authorization and quotas can be added here without changing adapters or
        # graph semantics. Profile is the only strategy choice exposed to callers.
        if isinstance(command, StartDiagnosis):
            resolved = self.configs.resolve(command.profile)
            async for event in self.runtime.run(command, resolved):
                yield event
            return
        async for event in self.runtime.run(command):
            yield event

    async def get_run(self, run_id: str, identity: Identity | None = None) -> RunView:
        return await self.runtime.get_run(run_id)

    async def events(self, run_id: str, after: int = 0) -> AsyncIterator[AgentEvent]:
        async for event in self.runtime.events(run_id, after):
            yield event


class RuntimeApplication(Protocol):
    async def send(self, command: AgentCommand) -> AsyncIterator[AgentEvent]: ...
