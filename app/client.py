"""Local and remote clients with identical Command/Event APIs."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Protocol
from urllib.request import Request, urlopen

from .application import ApplicationService
from .infra import event_from_json
from .protocol.commands import AgentCommand, CancelDiagnosis, StartDiagnosis
from .protocol.events import AgentEvent
from .runtime import RunView


class AgentClient(Protocol):
    def send(self, command: AgentCommand) -> AsyncIterator[AgentEvent]: ...
    async def get_run(self, run_id: str) -> RunView: ...


class LocalAgentClient:
    def __init__(self, service: ApplicationService) -> None:
        self.service = service

    async def send(self, command: AgentCommand) -> AsyncIterator[AgentEvent]:
        async for event in self.service.send(command):
            yield event

    async def get_run(self, run_id: str) -> RunView:
        return await self.service.get_run(run_id)


class RemoteAgentClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def send(self, command: AgentCommand) -> AsyncIterator[AgentEvent]:
        endpoint = f"{self.base_url}/v1/runs"
        if not isinstance(command, StartDiagnosis):
            suffix = "/cancel" if isinstance(command, CancelDiagnosis) else "/commands"
            endpoint = f"{self.base_url}/v1/runs/{command.run_id}{suffix}"
        body = command.model_dump_json().encode()
        response = await asyncio.to_thread(
            lambda: urlopen(
                Request(
                    endpoint, data=body, headers={"Content-Type": "application/json"}
                )
            )
        )
        for line in response:
            decoded = line.decode("utf-8").strip()
            if decoded.startswith("data:"):
                yield event_from_json(decoded[5:].strip())

    async def get_run(self, run_id: str) -> RunView:
        response = await asyncio.to_thread(urlopen, f"{self.base_url}/v1/runs/{run_id}")
        data = await asyncio.to_thread(response.read)
        return RunView.model_validate_json(data)
