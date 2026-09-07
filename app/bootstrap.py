"""The sole dependency-composition point for local execution."""

from __future__ import annotations

from .agents import OpenAINodeRunner
from .application import ApplicationService
from .client import LocalAgentClient
from .config import ConfigRepository, Settings
from .graph import DiagnosisGraph
from .infra import SQLiteCheckpointStore
from .prompts import PromptRegistry
from .runtime import DiagnosisRuntime


def build_local_client(
    settings: Settings,
) -> tuple[LocalAgentClient, SQLiteCheckpointStore, OpenAINodeRunner]:
    configs = ConfigRepository(settings.config_path)
    prompts = PromptRegistry(settings.prompt_config)
    runner = OpenAINodeRunner(prompts, settings.session_db)
    graph = DiagnosisGraph(runner, prompts, tracing_enabled=settings.tracing_enabled)
    store = SQLiteCheckpointStore(settings.session_db)
    runtime = DiagnosisRuntime(
        graph, store, configs, tracing_enabled=settings.tracing_enabled
    )
    service = ApplicationService(runtime, configs)
    return LocalAgentClient(service), store, runner
