import asyncio
from pathlib import Path

import pytest

from app.config import ConfigRepository
from app.environment import EnvironmentRepository
from app.graph import DiagnosisGraph
from app.infra import SQLiteCheckpointStore, ValidationFailedError
from app.models import (
    DiagnosisReport,
    EvaluationResult,
    InvestigationResult,
    ProblemAnalysis,
    ProblemCategory,
)
from app.prompts import PromptRegistry
from app.protocol.commands import ConfirmDiagnosisTarget, StartDiagnosis
from app.protocol.events import (
    NodeAttemptStarted,
    TargetConfirmationRequired,
    TargetConfirmed,
)
from app.runtime import DiagnosisRuntime


class FakeRunner:
    def __init__(self):
        self.calls: list[str] = []
        self.outputs = iter(
            [
                ProblemAnalysis(
                    category=ProblemCategory.PERFORMANCE,
                    category_confidence=0.9,
                    summary="slow",
                    symptoms=["timeout"],
                ),
                InvestigationResult(
                    investigation_summary="pool saturation",
                    hypotheses=[],
                    primary_conclusion="pool saturation",
                ),
                EvaluationResult(
                    passed=True,
                    score=90,
                    criteria_scores={
                        "problem_coverage": 20,
                        "evidence_traceability": 20,
                        "reasoning_consistency": 20,
                        "verification_executability": 20,
                        "uncertainty_expression": 10,
                    },
                ),
                DiagnosisReport(
                    executive_summary="supported",
                    primary_conclusion="pool saturation",
                    status_explanation="supported by evidence",
                    next_actions=["inspect pool"],
                ),
            ]
        )

    async def run(self, node_name, payload, runtime, category=None):
        self.calls.append(node_name)
        return next(self.outputs)


async def collect(runtime, command):
    return [event async for event in runtime.run(command)]


def make_runtime(tmp_path: Path):
    catalog = tmp_path / "environments.yaml"
    catalog.write_text(
        """
config_version: catalog-v1
environments:
  production:
    display_name: Production
    aliases: [prod]
    level: production
  staging:
    display_name: Staging
    aliases: [stage]
    level: non-production
services:
  orders-prod:
    name: order-api
    aliases: [orders]
    environment_id: production
""",
        encoding="utf-8",
    )
    runner = FakeRunner()
    graph = DiagnosisGraph(
        runner,
        PromptRegistry(tmp_path / "missing-prompts.yaml"),
        tracing_enabled=False,
    )
    store = SQLiteCheckpointStore(tmp_path / "state.sqlite")
    runtime = DiagnosisRuntime(
        graph,
        store,
        ConfigRepository(None),
        tracing_enabled=False,
        sleep=lambda _delay: asyncio.sleep(0),
        environment_repository=EnvironmentRepository(catalog),
    )
    return runtime, runner, store


@pytest.mark.asyncio
async def test_inferred_target_waits_before_any_node_or_tool(tmp_path: Path):
    runtime, runner, store = make_runtime(tmp_path)
    try:
        command = StartDiagnosis(
            run_id="target-run",
            question="orders timeout",
            context={"environment": "prod"},
        )
        events = await collect(runtime, command)
        state = await runtime.get_run(command.run_id)
        assert state.lifecycle_status.value == "waiting_for_target_confirmation"
        assert state.pending_target_confirmation is not None
        assert runner.calls == []
        assert any(isinstance(event, TargetConfirmationRequired) for event in events)
        assert not any(isinstance(event, NodeAttemptStarted) for event in events)

        pending = state.pending_target_confirmation
        confirm = ConfirmDiagnosisTarget(
            run_id=command.run_id,
            expected_revision=state.revision,
            request_id=pending.request_id,
            environment_id="production",
        )
        confirmed_events = await collect(runtime, confirm)
        final = await runtime.get_run(command.run_id)
        assert final.lifecycle_status.value == "completed"
        assert final.environment_snapshot_id
        assert final.target.environment_id == "production"
        assert any(isinstance(event, TargetConfirmed) for event in confirmed_events)
        assert runner.calls == ["analyze", "investigate", "evaluate", "summarize"]
        snapshot = store.get_environment_snapshot(final.environment_snapshot_id)
        assert snapshot.environment_id == "production"

        replay = await collect(runtime, confirm)
        assert any(isinstance(event, TargetConfirmed) for event in replay)
        assert runner.calls == ["analyze", "investigate", "evaluate", "summarize"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_inference_service_name_is_not_replayed_as_a_service_id(
    tmp_path: Path,
):
    runtime, runner, store = make_runtime(tmp_path)
    try:
        command = StartDiagnosis(
            run_id="target-service-name-run",
            question="orders timeout",
            context={"environment": "prod", "service": "order-api"},
        )
        await collect(runtime, command)
        state = await runtime.get_run(command.run_id)
        pending = state.pending_target_confirmation
        assert pending is not None

        await collect(
            runtime,
            ConfirmDiagnosisTarget(
                run_id=command.run_id,
                expected_revision=state.revision,
                request_id=pending.request_id,
                environment_id="production",
            ),
        )
        final = await runtime.get_run(command.run_id)
        assert final.lifecycle_status.value == "completed"
        assert final.target.primary_service_id is None
        assert runner.calls == ["analyze", "investigate", "evaluate", "summarize"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_inferred_candidates_cannot_be_confirmed_after_catalog_changes(
    tmp_path: Path,
):
    runtime, runner, store = make_runtime(tmp_path)
    try:
        command = StartDiagnosis(
            run_id="stale-target-run",
            question="orders timeout",
            context={"environment": "prod"},
        )
        await collect(runtime, command)
        state = await runtime.get_run(command.run_id)
        pending = state.pending_target_confirmation
        assert pending is not None
        repository = runtime.environment_repository
        assert repository is not None
        raw = repository.public_config()
        raw["environments"][0]["display_name"] = "Production Updated"
        repository.apply(raw, repository.revision)

        with pytest.raises(ValidationFailedError, match="environment config changed"):
            await collect(
                runtime,
                ConfirmDiagnosisTarget(
                    run_id=command.run_id,
                    expected_revision=state.revision,
                    request_id=pending.request_id,
                    environment_id="production",
                ),
            )
        assert runner.calls == []
    finally:
        store.close()
