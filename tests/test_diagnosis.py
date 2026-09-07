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
from app.protocol.commands import StartDiagnosis


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


# --- Fixes 4-8: regression tests for provider compat hardening ---


def test_node_runtime_context_carries_model_field():
    ctx = NodeRuntimeContext("run", "analyze", "v1", model="gpt-4.1-mini")
    assert ctx.model == "gpt-4.1-mini"
    # default remains None so the SDK default applies when unset
    assert NodeRuntimeContext("run", "analyze", "v1").model is None


@pytest.mark.asyncio
async def test_node_policy_model_is_passed_to_agent(monkeypatch, tmp_path):
    """Fix #4: NodePolicy.model must reach Agent(model=...), not stay dead code."""
    sdk = AsyncMock(return_value=SimpleNamespace(final_output=analysis()))
    monkeypatch.setattr("app.agents.Runner.run", sdk)
    runner = OpenAINodeRunner(
        PromptRegistry(Path("missing.yml")), tmp_path / "sessions.db"
    )
    ctx = NodeRuntimeContext("run", "analyze", "v1", model="my-custom-model")
    try:
        await runner.run("analyze", AnalyzeInput(question="a", evidence=[]), ctx)
    finally:
        runner.close()
    agent = sdk.call_args.args[0]
    assert agent.model == "my-custom-model"


def test_settings_read_openai_provider_options(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://gateway.local/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("BUGLENS_OPENAI_TIMEOUT", "120")
    monkeypatch.setenv("BUGLENS_TRACING", "false")
    settings = Settings.from_env()
    assert settings.openai_base_url == "http://gateway.local/v1"
    assert settings.openai_api_key == "sk-test"
    assert settings.openai_timeout == 120.0


def test_settings_reject_invalid_timeout(monkeypatch):
    monkeypatch.setenv("BUGLENS_OPENAI_TIMEOUT", "0")
    with pytest.raises(ValidationError):
        Settings.from_env()


def test_configure_openai_provider_returns_custom_flag(monkeypatch):
    from app.bootstrap import configure_openai_provider

    def restore():
        import agents

        agents._default_openai_api = None

    # With neither base_url nor api_key -> False, SDK default preserved
    assert configure_openai_provider(base_url=None, api_key=None, timeout=60.0) is False
    # With a custom endpoint -> True
    import agents as agents_mod

    seen = {}

    def fake_client(client, use_for_tracing=True):
        seen["client"] = client
        seen["use_for_tracing"] = use_for_tracing

    def fake_api(api):
        seen["api"] = api

    monkeypatch.setattr(agents_mod, "set_default_openai_client", fake_client)
    monkeypatch.setattr(agents_mod, "set_default_openai_api", fake_api)
    result = configure_openai_provider(
        base_url="http://gateway.local/v1", api_key="sk-test", timeout=90.0
    )
    assert result is True
    assert seen["api"] == "chat_completions"
    assert seen["use_for_tracing"] is False
    # AsyncOpenAI normalizes the URL (e.g. trailing slash); compare by origin.
    assert str(seen["client"].base_url).rstrip("/") == "http://gateway.local/v1"
    restore()


@pytest.mark.asyncio
async def test_application_service_requires_model_credentials(tmp_path):
    """Fix #5: missing credentials fail fast with an actionable error."""
    from app.application import ApplicationService, ModelCredentialsError
    from app.config import ConfigRepository
    from app.graph import DiagnosisGraph
    from app.infra import SQLiteCheckpointStore
    from app.prompts import PromptRegistry

    prompts = PromptRegistry(Path("missing.yml"))

    class NoOpRunner:
        async def run(self, *args, **kwargs):
            raise AssertionError("should not run")

    graph = DiagnosisGraph(NoOpRunner(), prompts, tracing_enabled=False)
    store = SQLiteCheckpointStore(tmp_path / "db.sqlite")
    configs = ConfigRepository(None)
    from app.runtime import DiagnosisRuntime

    runtime = DiagnosisRuntime(graph, store, configs, tracing_enabled=False)
    service = ApplicationService(
        runtime,
        configs,
        require_model_credentials=True,
        openai_base_url=None,
        openai_api_key=None,
    )
    start = StartDiagnosis(run_id="r1", question="q")
    with pytest.raises(ModelCredentialsError) as exc:
        async for _ in service.send(start):
            pass
    assert exc.value.code == "model_credentials_not_configured"


@pytest.mark.asyncio
async def test_application_service_custom_endpoint_needs_no_api_key(tmp_path):
    """Fix #5: a custom gateway may use its own auth; only require key on default."""
    from app.application import ApplicationService, ModelCredentialsError
    from app.config import ConfigRepository
    from app.graph import DiagnosisGraph
    from app.infra import SQLiteCheckpointStore
    from app.prompts import PromptRegistry
    from app.runtime import DiagnosisRuntime

    prompts = PromptRegistry(Path("missing.yml"))

    class NoOpRunner:
        async def run(self, *args, **kwargs):
            raise AssertionError("should not run")

    graph = DiagnosisGraph(NoOpRunner(), prompts, tracing_enabled=False)
    store = SQLiteCheckpointStore(tmp_path / "db.sqlite")
    configs = ConfigRepository(None)
    runtime = DiagnosisRuntime(graph, store, configs, tracing_enabled=False)
    service = ApplicationService(
        runtime,
        configs,
        require_model_credentials=True,
        openai_base_url="http://gateway.local/v1",
        openai_api_key=None,
    )
    # With a custom endpoint the credential gate passes (custom gateways may use
    # their own auth), so _check_model_credentials must NOT raise.
    service._check_model_credentials()

    # And the default path (no base_url, no api_key) still fails fast.
    default_service = ApplicationService(
        runtime,
        configs,
        require_model_credentials=True,
        openai_base_url=None,
        openai_api_key=None,
    )
    with pytest.raises(ModelCredentialsError):
        default_service._check_model_credentials()


def test_console_clarifier_rejects_non_tty():
    """Fix #6: non-interactive stdin must not exit as a silent cancellation."""
    from app.cli import ConsoleClarifier, NonInteractiveClarifierError

    class FakeStream:
        def isatty(self):
            return False

    import asyncio

    clarifier = ConsoleClarifier(stream=FakeStream())
    with pytest.raises(NonInteractiveClarifierError) as exc:
        asyncio.run(clarifier.ask(interaction("analyze")))
    assert exc.value.code == "non_interactive_clarifier"
    assert "--resume" in str(exc.value) or "Web adapter" in str(exc.value)


def test_build_local_service_disables_tracing_for_custom_endpoint(
    monkeypatch, tmp_path
):
    """Fix #7: tracing must default off on custom gateways even if BUGLENS_TRACING=true."""
    from app import bootstrap
    from app.config import Settings

    monkeypatch.setenv("OPENAI_BASE_URL", "http://gateway.local/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("BUGLENS_TRACING", "true")
    monkeypatch.setenv("BUGLENS_SESSION_DB", str(tmp_path / "db.sqlite"))

    seen = {}

    def fake(*, base_url, api_key, timeout):
        seen["called"] = True
        return True

    monkeypatch.setattr(bootstrap, "configure_openai_provider", fake)
    settings = Settings.from_env()
    service, store, runner = bootstrap.build_local_service(settings)
    try:
        assert seen["called"] is True
        # tracing_enabled on runner/graph/runtime is forced False despite setting=True
        assert service.runtime.tracing_enabled is False
    finally:
        runner.close()
        store.close()
