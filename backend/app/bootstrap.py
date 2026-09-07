"""The sole dependency-composition point for local execution."""

from __future__ import annotations

from openai import AsyncOpenAI

from .agents import OpenAINodeRunner
from .application import ApplicationService
from .client import LocalAgentClient
from .config import ConfigRepository, Settings
from .graph import DiagnosisGraph
from .infra import SQLiteCheckpointStore
from .prompts import PromptRegistry
from .runtime import DiagnosisRuntime


def configure_openai_provider(
    *, base_url: str | None, api_key: str | None, timeout: float
) -> bool:
    """Bind the SDK to an OpenAI-compatible endpoint when one is configured.

    BugLens stays provider-neutral: when a base URL and/or API key are configured
    via Settings (``BUGLENS_OPENAI_BASE_URL`` / ``OPENAI_API_KEY``), point the
    Agents SDK at that client and force the chat-completions API. This lets
    self-hosted gateways (vLLM, litellm, Azure-compatible, etc.) work without code
    changes. When neither is set the SDK keeps its OpenAI default behaviour.

    Returns True when a custom endpoint is in use so callers can enable streaming:
    some gateways only work in streaming mode.
    """
    if not base_url and not api_key:
        return False
    # Import lazily so the dependency is only required for real model calls.
    from agents import set_default_openai_api, set_default_openai_client

    client = AsyncOpenAI(
        base_url=base_url or None, api_key=api_key or "", timeout=timeout
    )
    set_default_openai_client(client, use_for_tracing=False)
    # Many OpenAI-compatible gateways do not implement the /responses endpoint;
    # chat completions is the universally supported path.
    set_default_openai_api("chat_completions")
    return True


def build_local_client(
    settings: Settings,
) -> tuple[LocalAgentClient, SQLiteCheckpointStore, OpenAINodeRunner]:
    service, store, runner = build_local_service(settings)
    return LocalAgentClient(service), store, runner


def build_local_service(
    settings: Settings,
) -> tuple[ApplicationService, SQLiteCheckpointStore, OpenAINodeRunner]:
    """Build the shared application service for CLI, HTTP and admin adapters."""

    custom_endpoint = configure_openai_provider(
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        timeout=settings.openai_timeout,
    )
    configs = ConfigRepository(settings.config_path)
    prompts = PromptRegistry(settings.prompt_config)
    runner = OpenAINodeRunner(
        prompts, settings.session_db, default_streaming=custom_endpoint
    )
    # A custom gateway's tracing endpoint usually differs from OpenAI's; default
    # to disabling tracing there unless the operator explicitly enables it.
    tracing_enabled = settings.tracing_enabled and not custom_endpoint
    graph = DiagnosisGraph(runner, prompts, tracing_enabled=tracing_enabled)
    store = SQLiteCheckpointStore(settings.session_db)
    runtime = DiagnosisRuntime(graph, store, configs, tracing_enabled=tracing_enabled)
    service = ApplicationService(
        runtime,
        configs,
        require_model_credentials=True,
        openai_base_url=settings.openai_base_url,
        openai_api_key=settings.openai_api_key,
    )
    return service, store, runner
