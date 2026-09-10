"""End-to-end validation of two isolated log/database environments.

The runner in this module is intentionally deterministic.  It calls the real
environment tools and connectors, while returning strict node outputs instead
of calling a model.  A separate opt-in smoke command can exercise model tool
selection without making the normal test suite depend on credentials.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agents.tool_context import ToolContext
from buglens_file_logs_plugin import FileLogsPlugin
from buglens_sqlite_plugin import SQLitePlugin

from app.application import ApplicationService
from app.config import ConfigRepository
from app.environment import EnvironmentRepository, ResolvedEnvironmentSnapshot
from app.graph import DiagnosisGraph
from app.infra import SQLiteCheckpointStore
from app.models import (
    DiagnosisReport,
    EvaluationResult,
    InvestigationResult,
    ProblemAnalysis,
    ProblemCategory,
    ProgressDelta,
    RootCauseHypothesis,
)
from app.plugins import (
    PluginManager,
    build_plugin_runtime,
)
from app.prompts import PromptRegistry
from app.protocol.commands import StartDiagnosis
from app.runtime import DiagnosisRuntime

_SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))

from prepare_two_environment_e2e import prepare  # noqa: E402


class ScenarioNodeRunner:
    """Return strict node outputs after querying the real connector boundary."""

    def __init__(self, scenario: dict[str, Any]) -> None:
        self.scenario = scenario
        self.calls: list[tuple[str, Any, Any]] = []
        self.tool_results: dict[str, dict[str, Any]] = {}
        self.evidence_ids: dict[str, list[str]] = {}
        self._tool_call_number = 0

    async def run(
        self,
        node_name: str,
        payload: Any,
        runtime: Any,
        category: ProblemCategory | None = None,
    ) -> Any:
        self.calls.append((node_name, payload, runtime))
        environment_id = str(runtime.environment_snapshot.environment_id)
        details = self.scenario["environments"][environment_id]
        if node_name == "analyze":
            return self._analysis(environment_id)
        if node_name == "investigate":
            return await self._investigate(environment_id, details, runtime)
        if node_name == "evaluate":
            return self._evaluation()
        if node_name == "summarize":
            return self._report(environment_id, details, payload)
        raise AssertionError(f"unexpected graph node: {node_name}")

    def _analysis(self, environment_id: str) -> ProblemAnalysis:
        category = (
            ProblemCategory.INTEGRATION
            if environment_id == "staging"
            else ProblemCategory.PERFORMANCE
        )
        return ProblemAnalysis(
            category=category,
            category_confidence=0.98,
            summary=f"{environment_id} order-api has a reproducible incident",
            symptoms=(
                ["order remains pending after payment request"]
                if environment_id == "staging"
                else ["multiple order requests time out"]
            ),
            impact="the seeded order flow is affected",
            environment=environment_id,
            time_window=(
                f"{self.scenario['window_start']}/{self.scenario['window_end']}"
            ),
        )

    async def _investigate(
        self,
        environment_id: str,
        details: dict[str, Any],
        runtime: Any,
    ) -> InvestigationResult:
        assert runtime.node_name == "investigate"
        assert isinstance(runtime.environment_snapshot, ResolvedEnvironmentSnapshot)
        assert runtime.tools_enabled is True
        registry = runtime.tool_registry
        assert registry is not None

        window = {
            "start_time": self.scenario["window_start"],
            "end_time": self.scenario["window_end"],
            "service_ids": [details["service_id"]],
            "node_ids": [details["node_id"]],
            "levels": [],
            "cursor": None,
        }
        if environment_id == "staging":
            log_request = {
                **window,
                "source_id": details["log_source_id"],
                "text_query": "payment callback",
                "correlation_ids": [details["correlation_id"]],
            }
            database_request = {
                "source_id": details["database_source_id"],
                "sql": (
                    "SELECT o.order_id, o.status, e.event_type, e.observed_at, "
                    "e.detail FROM orders AS o JOIN order_events AS e "
                    "ON e.order_id = o.order_id WHERE o.order_id = :order_id "
                    "ORDER BY e.observed_at"
                ),
                "parameters": {"order_id": details["order_id"]},
                "purpose": "verify pending order timeline",
            }
        else:
            log_request = {
                **window,
                "source_id": details["log_source_id"],
                "text_query": "db pool acquire timeout",
                "correlation_ids": details["correlation_ids"],
            }
            database_request = {
                "source_id": details["database_source_id"],
                "sql": (
                    "SELECT o.order_id, o.status, e.event_type, e.observed_at, "
                    "e.detail FROM orders AS o JOIN order_events AS e "
                    "ON e.order_id = o.order_id WHERE e.observed_at >= :start_time "
                    "AND e.observed_at <= :end_time ORDER BY e.observed_at"
                ),
                "parameters": {
                    "start_time": self.scenario["window_start"],
                    "end_time": self.scenario["window_end"],
                },
                "purpose": "correlate timed-out orders with database events",
            }

        logs = await self._invoke(registry, runtime, "search_logs", log_request)
        database = await self._invoke(
            registry, runtime, "query_database", database_request
        )
        assert logs["status"] == "succeeded"
        assert database["status"] == "succeeded"
        assert logs["result"]["entries"]
        assert database["result"]["rows"]

        observer = runtime.execution_observer
        evidence = list(observer.evidence)
        log_evidence = next(item for item in evidence if item.source_type == "logs")
        database_evidence = next(
            item for item in evidence if item.source_type == "database"
        )
        ids = [log_evidence.evidence_id, database_evidence.evidence_id]
        self.tool_results[environment_id] = {"logs": logs, "database": database}
        self.evidence_ids[environment_id] = ids

        if environment_id == "staging":
            cause = (
                "sandbox payment callback timed out; the order never received "
                "payment confirmation and remained pending"
            )
            rationale = (
                "The log records a payment callback deadline exceeded and the "
                "database timeline ends with payment_callback_timeout."
            )
            verification = [
                "Check the sandbox payment provider callback delivery for the trace.",
                "Replay the callback in a non-production test account.",
            ]
        else:
            cause = (
                "database connection pool exhaustion caused connection acquisition "
                "timeouts for the order requests"
            )
            rationale = (
                "The log reports active=20, idle=0 and a waiting queue while the "
                "database events show matching checkout timeouts."
            )
            verification = [
                "Compare pool active, idle and waiters metrics during the window.",
                "Trace connection hold time and query latency before changing limits.",
            ]
        return InvestigationResult(
            investigation_summary=rationale,
            hypotheses=[
                RootCauseHypothesis(
                    rank=1,
                    cause=cause,
                    rationale=rationale,
                    supporting_evidence=ids,
                    confidence=0.96,
                    verification_steps=verification,
                )
            ],
            primary_conclusion=cause,
            progress_delta=ProgressDelta(new_evidence_ids=ids),
        )

    @staticmethod
    def _evaluation() -> EvaluationResult:
        return EvaluationResult(
            passed=True,
            score=90,
            criteria_scores={
                "problem_coverage": 18,
                "evidence_traceability": 23,
                "reasoning_consistency": 18,
                "verification_executability": 18,
                "uncertainty_expression": 13,
            },
            strengths=["two independent source types corroborate the conclusion"],
        )

    def _report(self, environment_id: str, details: dict[str, Any], payload: Any):
        assert payload.evidence
        cause = (
            "sandbox payment callback timed out; the order never received payment "
            "confirmation and remained pending"
            if environment_id == "staging"
            else "database connection pool exhaustion caused connection acquisition "
            "timeouts for the order requests"
        )
        return DiagnosisReport(
            executive_summary=f"{environment_id}: {cause}",
            primary_conclusion=cause,
            status_explanation=(
                "The conclusion is supported by one bounded log search and one "
                "bounded database query."
            ),
            next_actions=[
                "Validate the same signals against the live read-only sources."
            ],
            evidence_ids=[item.evidence_id for item in payload.evidence],
        )

    async def _invoke(
        self, registry: Any, runtime: Any, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        registration = registry.registration(tool_name)
        assert registration is not None
        self._tool_call_number += 1
        encoded = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        context = ToolContext(
            context=runtime,
            tool_name=tool_name,
            tool_call_id=f"e2e-tool-{self._tool_call_number}",
            tool_arguments=encoded,
        )
        return await registration.tool.on_invoke_tool(context, encoded)


@pytest.fixture
def e2e_components(tmp_path: Path):
    fixture_root = tmp_path / "fixtures"
    scenario = prepare(fixture_root)
    repository = EnvironmentRepository(fixture_root / "catalog.yaml")
    manager, environment_service, environment_registry = build_plugin_runtime(
        repository,
        PluginManager([SQLitePlugin(), FileLogsPlugin()]),
    )
    configs = ConfigRepository(fixture_root / "profile.yaml")
    runner = ScenarioNodeRunner(scenario)
    graph = DiagnosisGraph(
        runner,
        PromptRegistry(tmp_path / "missing-prompts.yaml"),
        tracing_enabled=False,
    )
    store = SQLiteCheckpointStore(tmp_path / "checkpoint.sqlite")
    runtime = DiagnosisRuntime(
        graph,
        store,
        configs,
        tracing_enabled=False,
        sleep=lambda _delay: asyncio.sleep(0),
        environment_repository=repository,
        environment_tools=SimpleNamespace(
            registry=environment_registry,
            close=environment_service.close,
        ),
    )
    application = ApplicationService(
        runtime,
        configs,
        require_model_credentials=False,
        environments=repository,
    )
    try:
        yield scenario, repository, environment_service, runner, application, store
    finally:
        application.close()
        store.close()
        # ``application.close`` owns the environment service/manager lifecycle;
        # keep this explicit assertion useful if composition changes later.
        assert manager._instances == {}


@pytest.mark.asyncio
async def test_two_environments_complete_with_own_logs_and_databases(e2e_components):
    scenario, repository, environment_service, runner, application, store = (
        e2e_components
    )
    instances = repository.directory().plugin_instances
    health = [await environment_service.check_instance(item.id) for item in instances]
    assert {item.status for item in health} == {"ok"}
    assert [item.id for item in repository.environments()] == ["staging", "production"]

    states = {}
    for environment_id in ("staging", "production"):
        details = scenario["environments"][environment_id]
        assert scenario["window_start"] in details["question"]
        assert scenario["window_end"] in details["question"]
        command = StartDiagnosis(
            run_id=f"e2e-{environment_id}",
            question=details["question"],
            profile="default",
            context={
                "environment": environment_id,
                "service": details["service_id"],
                "incident_started_at": scenario["window_start"],
                "window_start": scenario["window_start"],
                "window_end": scenario["window_end"],
            },
            target={
                "mode": "explicit",
                "environment_id": environment_id,
                "primary_service_id": details["service_id"],
            },
        )
        events = [event async for event in application.send(command)]
        state = await application.get_run(command.run_id)
        states[environment_id] = state

        assert state.lifecycle_status.value == "completed"
        assert state.outcome.value == "confirmed"
        assert state.target.environment_id == environment_id
        assert state.environment_snapshot_id
        assert state.environment_snapshot["environment_id"] == environment_id
        assert {item["id"] for item in state.environment_snapshot["sources"]} == {
            details["log_source_id"],
            details["database_source_id"],
        }
        assert state.evaluation is not None and state.evaluation.passed is True
        assert state.evaluation.score >= 75
        assert state.evaluation.criteria_scores["evidence_traceability"] >= 15
        assert state.evaluation.criteria_scores["verification_executability"] >= 15
        assert state.report is not None
        assert set(state.report.evidence_ids) >= set(
            runner.evidence_ids[environment_id]
        )
        report_text = json.dumps(
            state.report.model_dump(mode="json"), ensure_ascii=False
        ).casefold()
        for keyword in details["expected_keywords"]:
            assert keyword.casefold() in report_text
        for keyword in details["forbidden_keywords"]:
            assert keyword.casefold() not in report_text

        event_types = [event.event_type for event in events]
        assert "target_confirmed" in event_types
        assert "run_completed" in event_types
        assert "input_required" not in event_types
        assert "run_failed" not in event_types
        persisted_events = store.events_after(command.run_id)
        assert [event.sequence for event in persisted_events] == list(
            range(1, len(persisted_events) + 1)
        )
        assert [event.revision for event in persisted_events] == sorted(
            event.revision for event in persisted_events
        )
        assert (
            sum(event.event_type == "node_completed" for event in persisted_events) == 4
        )
        assert [
            item[0]
            for item in runner.calls
            if item[0]
            in {
                "analyze",
                "investigate",
                "evaluate",
                "summarize",
            }
        ][-4:] == ["analyze", "investigate", "evaluate", "summarize"]

        tools = store.list_tool_executions(run_id=command.run_id)
        assert {item["operation"] for item in tools} == {
            "search_logs",
            "query_database",
        }
        assert {item["status"] for item in tools} == {"succeeded"}
        assert {item["source_id"] for item in tools} == {
            details["log_source_id"],
            details["database_source_id"],
        }
        assert all(
            item["plugin_instance_id"].startswith(f"{environment_id}-")
            for item in tools
        )
        database_audit = next(
            item for item in tools if item["operation"] == "query_database"
        )
        assert database_audit["query_fingerprint"]
        assert "ord_" not in (database_audit["redacted_query"] or "")
        assert "payment_token" not in json.dumps(tools, ensure_ascii=False)

        evidence = store.list_evidence(command.run_id)
        assert {item.source_type for item in evidence} >= {"logs", "database"}
        assert all(item.tool_execution_id for item in evidence)
        serialized = json.dumps(
            {
                "state": state.model_dump(mode="json"),
                "events": [event.model_dump(mode="json") for event in persisted_events],
                "tools": tools,
                "evidence": [item.model_dump(mode="json") for item in evidence],
            },
            ensure_ascii=False,
        ).casefold()
        assert "traceback" not in serialized
        assert "password" not in serialized
        assert "payment_token" not in serialized

    staging_text = json.dumps(
        {
            "state": states["staging"].model_dump(mode="json"),
            "tools": store.list_tool_executions(run_id="e2e-staging"),
            "evidence": [
                item.model_dump(mode="json")
                for item in store.list_evidence("e2e-staging")
            ],
        },
        ensure_ascii=False,
    ).casefold()
    production_text = json.dumps(
        {
            "state": states["production"].model_dump(mode="json"),
            "tools": store.list_tool_executions(run_id="e2e-production"),
            "evidence": [
                item.model_dump(mode="json")
                for item in store.list_evidence("e2e-production")
            ],
        },
        ensure_ascii=False,
    ).casefold()
    assert (
        scenario["environments"]["production"]["marker"].casefold() not in staging_text
    )
    assert (
        scenario["environments"]["staging"]["marker"].casefold() not in production_text
    )


@pytest.mark.asyncio
async def test_two_environment_snapshots_reject_cross_environment_sources(
    e2e_components,
):
    scenario, repository, environment_service, _runner, _application, _store = (
        e2e_components
    )
    for environment_id, foreign_environment_id in (
        ("staging", "production"),
        ("production", "staging"),
    ):
        snapshot = repository.snapshot(environment_id)
        foreign = scenario["environments"][foreign_environment_id]
        runtime = SimpleNamespace(
            graph_run_id=f"cross-{environment_id}",
            execution_id=f"cross-exec-{environment_id}",
            environment_snapshot=snapshot,
            environment_snapshot_id=snapshot.snapshot_id,
            tool_max_calls=20,
            tool_max_concurrent=2,
            tool_max_results=20,
            tool_max_result_rows=200,
            tool_result_bytes=65536,
            tool_timeout_seconds=30,
        )
        result = await environment_service.call(
            "search_logs",
            runtime,
            {
                "source_id": foreign["log_source_id"],
                "start_time": scenario["window_start"],
                "end_time": scenario["window_end"],
                "text_query": "marker",
                "service_ids": [],
                "node_ids": [],
                "levels": [],
                "correlation_ids": [],
            },
        )
        assert result["status"] == "rejected"
        assert "source_not_allowed" in result["warnings"]
        assert result.get("evidence", []) == []
