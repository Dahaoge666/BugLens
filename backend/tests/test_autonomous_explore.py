"""Tests for autonomous exploration mode (auto-skip + read-only tools)."""

import asyncio
import json
from pathlib import Path

import pytest

from app.builtin_tools import _MAX_RESPONSE_BYTES, build_builtin_tool_registry, http_get
from app.config import ConfigRepository
from app.graph import DiagnosisGraph
from app.infra import SQLiteCheckpointStore
from app.models import (
    ClarificationQuestion,
    DiagnosisReport,
    EvaluationResult,
    InvestigationResult,
    ProblemAnalysis,
    ProblemCategory,
    RootCauseHypothesis,
    UserInteractionRequest,
)
from app.prompts import PromptRegistry
from app.protocol.commands import StartDiagnosis
from app.protocol.events import InputSkipped
from app.runtime import DiagnosisRuntime
from app.tools import ToolRegistry


def _analysis(request=None):
    return ProblemAnalysis(
        category=ProblemCategory.PERFORMANCE,
        category_confidence=0.9,
        summary="slow",
        symptoms=["timeout"],
        interaction_request=request,
    )


def _investigation():
    return InvestigationResult(
        investigation_summary="pool likely full",
        hypotheses=[
            RootCauseHypothesis(
                rank=1,
                cause="connection pool saturation",
                rationale="timeouts",
                supporting_evidence=["log"],
                confidence=0.7,
                verification_steps=["inspect pool metric"],
            )
        ],
        primary_conclusion="connection pool saturation",
    )


def _evaluation():
    return EvaluationResult(
        passed=True,
        score=85,
        criteria_scores={
            "problem_coverage": 18,
            "evidence_traceability": 20,
            "reasoning_consistency": 17,
            "verification_executability": 18,
            "uncertainty_expression": 12,
        },
        retry_guidance=[],
    )


def _report():
    return DiagnosisReport(
        executive_summary="pool saturation",
        primary_conclusion="connection pool saturation",
        status_explanation="supported",
        next_actions=["inspect pool metric"],
    )


def _clarification_request(node: str):
    return UserInteractionRequest(
        request_id=f"ask-{node}",
        source_node=node,
        resume_node=node,
        reason="insufficient_evidence",
        explanation="Need a metric",
        questions=[
            ClarificationQuestion(
                id="metric", question="Pool usage?", rationale="Confirm saturation"
            )
        ],
    )


class FakeRunner:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    async def run(self, node_name, payload, runtime, category=None):
        self.calls.append((node_name, payload, runtime))
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        return output


async def _collect(runtime, command):
    return [event async for event in runtime.run(command)]


def _make_runtime(tmp_path, outputs):
    runner = FakeRunner(outputs)
    prompts = PromptRegistry(Path("missing.yml"))
    graph = DiagnosisGraph(runner, prompts, tracing_enabled=False)
    store = SQLiteCheckpointStore(tmp_path / "buglens.sqlite")
    configs = ConfigRepository(None)
    return (
        DiagnosisRuntime(
            graph,
            store,
            configs,
            tracing_enabled=False,
            sleep=lambda _delay: asyncio.sleep(0),
        ),
        runner,
        store,
    )


@pytest.mark.asyncio
async def test_auto_explore_skips_clarification_and_continues(tmp_path):
    runtime, runner, store = _make_runtime(
        tmp_path,
        [
            _analysis(_clarification_request("analyze")),
            _analysis(),
            _investigation(),
            _evaluation(),
            _report(),
        ],
    )
    try:
        events = await _collect(
            runtime,
            StartDiagnosis(
                run_id="run-auto",
                question="timeouts",
                context={"auto_explore": True},
            ),
        )
        final = await runtime.get_run("run-auto")

        # An input_skipped event was emitted for the auto-skipped clarification.
        skipped_events = [e for e in events if e.event_type == "input_skipped"]
        assert len(skipped_events) == 1
        assert isinstance(skipped_events[0], InputSkipped)
        assert skipped_events[0].source_node == "analyze"
        assert skipped_events[0].reason.startswith("auto_explore")
        assert skipped_events[0].question_ids == ["metric"]

        # The run never stopped in WAITING_USER: no committed state waited, and
        # the final state advanced past analyze to completed.
        assert final.lifecycle_status.value == "completed"
        assert final.outcome.value == "confirmed"
        assert "waiting_user" not in {
            row.lifecycle_status.value for row in store.list_state_history("run-auto")
        }
        assert [item[0] for item in runner.calls] == [
            "analyze",
            "analyze",
            "investigate",
            "evaluate",
            "summarize",
        ]
        # The 2nd analyze call received the skip ClarificationInput payload.
        assert runner.calls[1][1].information_unavailable is True
        assert len(final.skipped_interactions) == 1
        assert final.skipped_interactions[0].source_node == "analyze"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_auto_explore_exhausts_budget_and_goes_inconclusive(tmp_path):
    # Every analyze result asks for clarification. After two auto-skips the
    # per-node clarification budget is exhausted and the graph routes to
    # Summarize as INCONCLUSIVE rather than looping forever.
    runtime, runner, store = _make_runtime(
        tmp_path,
        [
            _analysis(_clarification_request("analyze")),
            _analysis(_clarification_request("analyze")),
            _analysis(_clarification_request("analyze")),
            _report(),
        ],
    )
    try:
        events = await _collect(
            runtime,
            StartDiagnosis(
                run_id="run-budget",
                question="timeouts",
                context={"auto_explore": True},
            ),
        )
        final = await runtime.get_run("run-budget")

        skipped_events = [e for e in events if e.event_type == "input_skipped"]
        assert len(skipped_events) == 2  # two skips before the budget runs out
        assert final.lifecycle_status.value == "completed"
        assert final.outcome.value == "inconclusive"
        assert [item[0] for item in runner.calls] == [
            "analyze",
            "analyze",
            "analyze",
            "summarize",
        ]
        assert final.current_node.value == "done"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_http_get_bounds_and_sanitizes(monkeypatch):
    import httpx2

    secret = "sk-supersecret-value-1234567890"
    # Valid JSON so the dict-sanitization path runs; the huge trace value
    # exercises the byte ceiling and the api_key exercises secret redaction.
    payload = json.dumps({"api_key": secret, "note": "ok", "trace": "x" * 100_000})

    class FakeResponse:
        def __init__(self, text, content_type, status):
            self.text = text
            self.status_code = status
            self._content_type = content_type

        class _Headers:
            def __init__(self, ct):
                self._ct = ct

            def get(self, key, default=""):
                return self._ct if key == "content-type" else default

        @property
        def headers(self):
            return self._Headers(self._content_type)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return FakeResponse(payload, "application/json", 200)

    monkeypatch.setattr(httpx2, "AsyncClient", FakeClient)

    result = await http_get("https://example.example/status")
    assert result.startswith("HTTP 200")
    assert len(result.encode("utf-8")) <= _MAX_RESPONSE_BYTES
    # The secret value was redacted before reaching the model context.
    assert secret not in result
    assert "[REDACTED]" in result


def test_build_builtin_tool_registry_registers_http_get_read_only():
    registry = build_builtin_tool_registry()
    assert isinstance(registry, ToolRegistry)
    registration = registry.registration("http_get")
    assert registration is not None
    assert registration.needs_approval is False
    assert registration.nodes == frozenset({"analyze", "investigate"})
    # Exposed only on enabled profiles that allow these nodes.
    assert registry.is_enabled(
        registration,
        node_name="analyze",
        enabled=True,
        allowed_nodes={"analyze", "investigate"},
    )
    assert not registry.is_enabled(
        registration,
        node_name="evaluate",
        enabled=True,
        allowed_nodes={"analyze", "investigate"},
    )
    # Disabled profiles never expose the connector even when registered.
    assert not registry.is_enabled(
        registration,
        node_name="analyze",
        enabled=False,
        allowed_nodes={"analyze", "investigate"},
    )
