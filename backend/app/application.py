"""Application service: profile selection and the shared runtime boundary."""

from __future__ import annotations

from typing import AsyncIterator, Protocol

from pydantic import Field

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
    capabilities: list[str] = Field(default_factory=list, max_length=50)


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
                self._check_model_credentials(command.profile)
            resolved = self.configs.resolve(command.profile)
            if identity is not None:
                context = dict(command.context)
                if identity.tenant_id:
                    context["tenant_id"] = identity.tenant_id
                else:
                    context.pop("tenant_id", None)
                context["capabilities"] = identity.capabilities
                command = command.model_copy(update={"context": context})
            async for event in self.runtime.run(command, resolved):
                yield event
            return
        async for event in self.runtime.run(command):
            yield event

    def _check_model_credentials(self, profile: str = "default") -> None:
        """Fail fast with an actionable error instead of deep inside Runner.

        Credentials can come from two places now: the environment variables
        (used by bare model names that fall back to the SDK global client) or the
        ``models`` registry in a profile (each named model may carry its own
        ``api_key`` / ``base_url``). Pass if either source is configured.
        """
        # 1) Environment-level credentials (bare-name / global-client path).
        if self._openai_base_url or self._openai_api_key:
            return
        # 2) Per-model credentials in the selected profile's models registry.
        try:
            resolved = self.configs.resolve(profile)
        except Exception:
            resolved = None
        if resolved is not None:
            for model_cfg in resolved.policy.models.values():
                if model_cfg.api_key or model_cfg.base_url:
                    return
        raise ModelCredentialsError(
            "no model credentials configured; set OPENAI_API_KEY/OPENAI_BASE_URL "
            "or define api_key/base_url on a model in the config profile"
        )

    async def get_run(self, run_id: str, identity: Identity | None = None) -> RunView:
        return await self.runtime.get_run(run_id)

    async def events(self, run_id: str, after: int = 0) -> AsyncIterator[AgentEvent]:
        async for event in self.runtime.events(run_id, after):
            yield event


class RuntimeApplication(Protocol):
    async def send(self, command: AgentCommand) -> AsyncIterator[AgentEvent]: ...
