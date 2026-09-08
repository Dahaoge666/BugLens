from pathlib import Path

import pytest

from app.agents import NativeDiagnosisRunner, NodeRuntimeContext
from app.config import ConfigRepository
from app.graph import NativeDiagnosisGraph, NativeRunTracker
from app.infra import SQLiteCheckpointStore
from app.models import (
    ClarificationQuestion,
    DiagnosisReport,
    DiagnosisTurnResult,
    EvaluationResult,
    InvestigationResult,
    ProblemAnalysis,
    ProblemCategory,
    UserAnswer,
    UserInteractionRequest,
)
from app.prompts import PromptRegistry
from app.protocol.commands import StartDiagnosis, SubmitUserAnswers
from app.runtime import DiagnosisRuntime


def _analysis() -> ProblemAnalysis:
    return ProblemAnalysis(
        category=ProblemCategory.PERFORMANCE,
        category_confidence=0.92,
        summary="请求延迟升高",
        symptoms=["timeout"],
    )


def _investigation() -> InvestigationResult:
    return InvestigationResult(investigation_summary="连接池可能耗尽")


def _evaluation() -> EvaluationResult:
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
    )


def _report() -> DiagnosisReport:
    return DiagnosisReport(
        executive_summary="定位结果已完成独立评测",
        primary_conclusion="连接池耗尽",
        status_explanation="证据与验证步骤一致",
        next_actions=["核对连接池使用率"],
    )


class FakeNativeRunner:
    def __init__(self, outputs: list[DiagnosisTurnResult]):
        self.outputs = iter(outputs)
        self.calls: list[tuple[str, object, object]] = []

    async def run(self, node_name, payload, runtime, category=None):
        self.calls.append((node_name, payload, runtime))
        return next(self.outputs)


async def _collect(runtime, command):
    return [event async for event in runtime.run(command)]


def _runtime(tmp_path, outputs):
    runner = FakeNativeRunner(outputs)
    graph = NativeDiagnosisGraph(
        runner, PromptRegistry(Path("missing.yml")), tracing_enabled=False
    )
    store = SQLiteCheckpointStore(tmp_path / "native.sqlite")
    runtime = DiagnosisRuntime(
        graph,
        store,
        ConfigRepository(None),
        tracing_enabled=False,
        sleep=lambda _delay: __import__("asyncio").sleep(0),
    )
    return runtime, runner, store


def _completed(review_count: int = 1) -> DiagnosisTurnResult:
    return DiagnosisTurnResult(
        kind="completed",
        analysis=_analysis(),
        investigation=_investigation(),
        evaluation=_evaluation(),
        report=_report(),
        active_agent="investigator:performance",
        review_count=review_count,
    )


@pytest.mark.asyncio
async def test_native_runtime_completes_in_one_sdk_turn(tmp_path):
    runtime, runner, store = _runtime(tmp_path, [_completed()])
    try:
        events = await _collect(
            runtime, StartDiagnosis(run_id="native-complete", question="接口超时")
        )
        state = await runtime.get_run("native-complete")
        assert state.status == "completed"
        assert state.review_count == 1
        assert state.attempt == 1
        assert state.active_agent is None
        assert len(runner.calls) == 1
        assert runner.calls[0][0] == "analyze"
        assert runner.calls[0][2].session_id == "native-complete:diagnosis"
        assert events[-1].event_type == "run_completed"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_native_user_input_restarts_same_diagnosis_session(tmp_path):
    request = UserInteractionRequest(
        request_id="native-question",
        source_node="analyze",
        resume_node="analyze",
        reason="missing_problem_context",
        explanation="需要影响范围",
        questions=[
            ClarificationQuestion(
                id="impact",
                question="影响了多少请求？",
                rationale="判断问题范围",
            )
        ],
    )
    waiting = DiagnosisTurnResult(
        kind="needs_input",
        analysis=_analysis().model_copy(update={"interaction_request": request}),
        interaction_request=request,
        active_agent="triage",
    )
    runtime, runner, store = _runtime(tmp_path, [waiting, _completed()])
    try:
        await _collect(
            runtime, StartDiagnosis(run_id="native-wait", question="接口超时")
        )
        waiting_state = await runtime.get_run("native-wait")
        assert waiting_state.status == "waiting_user"
        await _collect(
            runtime,
            SubmitUserAnswers(
                run_id="native-wait",
                expected_revision=waiting_state.revision,
                request_id=request.request_id,
                answers=[
                    UserAnswer(
                        request_id=request.request_id,
                        question_id="impact",
                        answer="10% 请求失败",
                    )
                ],
            ),
        )
        final = await runtime.get_run("native-wait")
        assert final.status == "completed"
        assert len(runner.calls) == 2
        assert runner.calls[0][2].session_id == runner.calls[1][2].session_id
        assert final.answers[0].answer == "10% 请求失败"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_native_gate_marks_missing_independent_review_inconclusive(tmp_path):
    runtime, _runner, store = _runtime(tmp_path, [_completed(review_count=0)])
    try:
        await _collect(
            runtime, StartDiagnosis(run_id="native-no-review", question="接口超时")
        )
        state = await runtime.get_run("native-no-review")
        assert state.status == "inconclusive"
        assert state.outcome.value == "inconclusive"
        assert state.report.primary_conclusion is None
    finally:
        store.close()


def test_native_agent_hierarchy_has_category_handoffs_and_independent_reviewer(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = ConfigRepository(None).resolve("default")
    runner = NativeDiagnosisRunner(
        PromptRegistry(Path("missing.yml")), tmp_path / "sessions.sqlite"
    )
    runtime = NodeRuntimeContext(
        graph_run_id="native-structure",
        node_name="analyze",
        config_version=config.config_version,
        model="default",
        model_config=config.policy.model("default"),
        resolved_config=config,
        native_tracker=NativeRunTracker(),
        session_id="native-structure:diagnosis",
        tool_allowed_nodes=frozenset({"investigate"}),
    )
    triage = runner._triage(runtime)
    investigator = runner._investigator(runtime, ProblemCategory.PERFORMANCE)
    assert len(triage.handoffs) == len(ProblemCategory)
    assert any(tool.name == "review_diagnosis" for tool in investigator.tools)
