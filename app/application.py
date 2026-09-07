"""Application service: profile selection and the shared runtime boundary."""

from __future__ import annotations

from typing import AsyncIterator, Protocol

from .config import ConfigRepository
from .models import StrictModel
from .protocol.commands import AgentCommand, StartDiagnosis
from .protocol.events import AgentEvent
from .runtime import DiagnosisRuntime, RunView


class ModelCredentialsError(RuntimeError):
    """Raised when model credentials are not usable before any node runs."""

    code = "model_credentials_not_configured"


class Identity(StrictModel):
    subject: str = "local"
    tenant_id: str | None = None


class ApplicationService:
    def __init__(
        self,
        runtime: DiagnosisRuntime,
        configs: ConfigRepository,
        *,
        require_model_credentials: bool = False,
        openai_base_url: str | None = None,
        openai_api_key: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.configs = configs
        self._require_model_credentials = require_model_credentials
        self._openai_base_url = openai_base_url
        self._openai_api_key = openai_api_key

    async def send(
        self, command: AgentCommand, identity: Identity | None = None
    ) -> AsyncIterator[AgentEvent]:
        # Authorization and quotas can be added here without changing adapters or
        # graph semantics. Profile is the only strategy choice exposed to callers.
        if isinstance(command, StartDiagnosis):
            if self._require_model_credentials:
                self._check_model_credentials()
            resolved = self.configs.resolve(command.profile)
            async for event in self.runtime.run(command, resolved):
                yield event
            return
        async for event in self.runtime.run(command):
            yield event

    def _check_model_credentials(self) -> None:
        """Fail fast with an actionable error instead of deep inside Runner.

        The OpenAI SDK reads ``OPENAI_API_KEY`` lazily, so a missing key surfaces
        only when a node runs, wrapped as an opaque ``NodeExecutionError``. This
        guard keeps the root cause visible at the application boundary. It is opt-in
        (``require_model_credentials``) so tests that never call a model stay green.
        """
        # A custom endpoint with its own auth may not need an api_key header; only
        # the default OpenAI path strictly requires one.
        if not self._openai_base_url and not self._openai_api_key:
            raise ModelCredentialsError(
                "OPENAI_API_KEY is not configured; set it (or OPENAI_BASE_URL for an "
                "OpenAI-compatible gateway) before starting a diagnosis"
            )

    async def get_run(self, run_id: str, identity: Identity | None = None) -> RunView:
        return await self.runtime.get_run(run_id)

    async def events(self, run_id: str, after: int = 0) -> AsyncIterator[AgentEvent]:
        async for event in self.runtime.events(run_id, after):
            yield event


class RuntimeApplication(Protocol):
    async def send(self, command: AgentCommand) -> AsyncIterator[AgentEvent]: ...
