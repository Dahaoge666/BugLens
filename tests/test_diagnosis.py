from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.agents import NodeExecutionError, NodeRuntimeContext, OpenAINodeRunner
from app.cli import _key_values, build_parser
from app.config import Settings
from app.graph import DiagnosisGraph
from app.models import (
    AnalyzeInput,
    ClarificationInput,
    ClarificationQuestion,
    DiagnosisReport,
    EvaluationResult,
    GraphContractError,
    InvestigationResult,
    ProblemAnalysis,
    ProblemCategory,
    RetryInput,
    RootCauseHypothesis,
    UserAnswer,
    UserInteractionRequest,
)
from app.prompts import PromptRegistry


class FakeRunner:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    async def run(self, node_name, payload, runtime, category=None):
        self.calls.append((node_name, payload, runtime, category))
        return next(self.outputs)


class FakeClarifier:
    def __init__(self, answers=None):
        self.answers = iter(answers or [])
        self.requests = []

    async def ask(self, request):
        self.requests.append(request)
        return next(self.answers)


def interaction(node="investigate"):
    return UserInteractionRequest(
        request_id="ask",
        source_node=node,
        resume_node=node,
        reason="insufficient_evidence",
        explanation="Need a metric",
        questions=[
            ClarificationQuestion(
                id="pool", question="Pool usage?", rationale="Confirm saturation"
            )
        ],
    )


def answer():
    return UserAnswer(request_id="ask", question_id="pool", answer="100/100")


def analysis(request=None):
    return ProblemAnalysis(
        category=ProblemCategory.PERFORMANCE,
        category_confidence=0.9,
        summary="slow",
        symptoms=["timeout"],
        interaction_request=request,
    )


def investigation(request=None):
    return InvestigationResult(
        investigation_summary="pool likely full",
        hypotheses=[
            RootCauseHypothesis(
                rank=1,
                cause="connection pool saturation",
                rationale="timeouts",
                supporting_evidence=["timeout log"],
                confidence=0.7,
                verification_steps=["inspect pool metric"],
            )
        ],
        primary_conclusion="connection pool saturation",
        interaction_request=request,
    )


def evaluation(passed=True):
    return EvaluationResult(
        passed=passed,
        score=85 if passed else 50,
        criteria_scores={
            "problem_coverage": 18,
            "evidence_traceability": 20,
            "reasoning_consistency": 17,
            "verification_executability": 18,
            "uncertainty_expression": 12,
        },
        retry_guidance=[] if passed else ["add evidence"],
    )


def report():
    return DiagnosisReport(
        executive_summary="pool saturation",
        primary_conclusion="connection pool saturation",
        status_explanation="supported",
        next_actions=["inspect pool metric"],
    )


def graph(outputs):
    prompts = PromptRegistry(Path("missing.yml"))
    runner = FakeRunner(outputs)
    return DiagnosisGraph(runner, prompts, tracing_enabled=False), runner


@pytest.mark.asyncio
async def test_completed_workflow_uses_four_isolated_nodes():
    workflow, runner = graph([analysis(), investigation(), evaluation(), report()])
    result = await workflow.run("timeouts", {}, [], FakeClarifier())
    assert result.status == "completed"
    assert [call[0] for call in runner.calls] == [
        "analyze",
        "investigate",
        "evaluate",
        "summarize",
    ]
    assert len({call[2].node_name for call in runner.calls}) == 4


@pytest.mark.asyncio
async def test_analyzer_clarification_continues_with_typed_followup():
    workflow, runner = graph(
        [
            analysis(interaction("analyze")),
            analysis(),
            investigation(),
            evaluation(),
            report(),
        ]
    )
    result = await workflow.run("timeouts", {}, [], FakeClarifier([[answer()]]))
    assert result.status == "completed"
    assert isinstance(runner.calls[1][1], ClarificationInput)
    assert runner.calls[0][2].graph_run_id == runner.calls[1][2].graph_run_id


@pytest.mark.asyncio
async def test_investigator_clarification_does_not_consume_attempt():
    workflow, runner = graph(
        [
            analysis(),
            investigation(interaction()),
            investigation(),
            evaluation(),
            report(),
        ]
    )
    result = await workflow.run(
        "timeouts", {}, [], FakeClarifier([[answer()]]), max_attempts=1
    )
    assert result.status == "completed" and result.attempt == 1
    assert isinstance(runner.calls[2][1], ClarificationInput)


@pytest.mark.asyncio
async def test_evaluation_feedback_continues_investigator_session():
    workflow, runner = graph(
        [
            analysis(),
            investigation(),
            evaluation(False),
            investigation(),
            evaluation(),
            report(),
        ]
    )
    result = await workflow.run("timeouts", {}, [], FakeClarifier())
    assert result.status == "completed" and result.attempt == 2
    assert isinstance(runner.calls[3][1], RetryInput)
    assert runner.calls[1][2].graph_run_id == runner.calls[3][2].graph_run_id


@pytest.mark.asyncio
async def test_clarification_limit_produces_inconclusive_report():
    workflow, _ = graph([analysis(interaction("analyze")), report()])
    result = await workflow.run(
        "timeouts", {}, [], FakeClarifier(), max_clarification_rounds=0
    )
    assert result.status == "inconclusive"
    assert result.report.primary_conclusion is None
    assert "尚未确认根因" in result.report.status_explanation


@pytest.mark.asyncio
async def test_sdk_runner_uses_same_session_per_node_and_separate_node_sessions(
    monkeypatch, tmp_path
):
    sdk = AsyncMock(
        side_effect=[
            SimpleNamespace(final_output=analysis()),
            SimpleNamespace(final_output=analysis()),
            SimpleNamespace(final_output=evaluation()),
        ]
    )
    monkeypatch.setattr("app.agents.Runner.run", sdk)
    runner = OpenAINodeRunner(
        PromptRegistry(Path("missing.yml")), tmp_path / "sessions.db"
    )
    analyze_context = NodeRuntimeContext("run", "analyze", "v1")
    evaluate_context = NodeRuntimeContext("run", "evaluate", "v1")
    try:
        await runner.run(
            "analyze", AnalyzeInput(question="a", evidence=[]), analyze_context
        )
        await runner.run(
            "analyze", AnalyzeInput(question="b", evidence=[]), analyze_context
        )
        await runner.run(
            "evaluate",
            SimpleNamespace(model_dump_json=lambda: "{}"),
            evaluate_context,
        )
    finally:
        runner.close()
    sessions = [call.kwargs["session"] for call in sdk.call_args_list]
    assert sessions[0] is sessions[1]
    assert sessions[0] is not sessions[2]
    assert sessions[0].session_id == "run:analyze"
    assert sessions[2].session_id == "run:evaluate"


@pytest.mark.asyncio
async def test_sdk_invalid_output_retries_once(monkeypatch, tmp_path):
    sdk = AsyncMock(return_value=SimpleNamespace(final_output={}))
    monkeypatch.setattr("app.agents.Runner.run", sdk)
    runner = OpenAINodeRunner(
        PromptRegistry(Path("missing.yml")), tmp_path / "sessions.db"
    )
    with pytest.raises(NodeExecutionError):
        await runner.run(
            "analyze",
            AnalyzeInput(question="timeout", evidence=[]),
            NodeRuntimeContext("run", "analyze", "v1"),
        )
    runner.close()
    assert sdk.await_count == 2


@pytest.mark.parametrize(
    "output_type",
    [ProblemAnalysis, InvestigationResult, EvaluationResult, DiagnosisReport],
)
def test_sdk_accepts_strict_output_schema(output_type):
    from agents import AgentOutputSchema

    assert AgentOutputSchema(output_type).is_strict_json_schema()


def test_evaluation_score_is_recomputed_and_thresholds_enforced():
    result = evaluation().model_copy(update={"score": 100}).enforce_rubric()
    assert result.score == 85 and result.passed
    data = evaluation().model_dump()
    data["criteria_scores"]["problem_coverage"] = 100
    with pytest.raises(ValidationError):
        EvaluationResult.model_validate(data)


def test_cli_parser_and_key_values():
    args = build_parser().parse_args(
        [
            "timeout",
            "--context",
            "environment=prod",
            "--json",
            "--output",
            "result.json",
        ]
    )
    assert args.question == "timeout" and args.json
    assert args.output == Path("result.json")
    assert _key_values(args.context, "--context") == {"environment": "prod"}
    with pytest.raises(ValueError):
        _key_values(["invalid"], "--context")


def test_settings_use_sdk_session_database(monkeypatch, tmp_path):
    monkeypatch.setenv("BUGLENS_SESSION_DB", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("BUGLENS_TRACING", "false")
    settings = Settings.from_env()
    assert settings.session_db == tmp_path / "sessions.db"
    assert not settings.tracing_enabled


def test_invalid_tracing_setting_is_rejected(monkeypatch):
    monkeypatch.setenv("BUGLENS_TRACING", "sometimes")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_clarification_contract_rejects_wrong_answer():
    with pytest.raises(GraphContractError):
        interaction().validate_answers(
            [UserAnswer(request_id="wrong", question_id="pool", answer="100/100")]
        )
