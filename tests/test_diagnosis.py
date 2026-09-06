from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import api
from app.agents import NodeExecutionError, NodeRuntimeContext, OpenAINodeRunner
from app.config import Settings
from app.graph import DiagnosisGraph
from app.models import (
    AnalyzeInput,
    ClarificationQuestion,
    DiagnosisReport,
    DiagnosisState,
    EvaluationResult,
    Evidence,
    GraphContractError,
    InvestigationResult,
    ProblemAnalysis,
    ProblemCategory,
    RootCauseHypothesis,
    UserAnswer,
    UserInteractionRequest,
)
from app.prompts import PromptRegistry
from app.service import DiagnosisService
from app.storage import JsonStateStore


class FakeRunner:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    async def run(self, node_name, payload, runtime: NodeRuntimeContext, category=None):
        self.calls.append((node_name, payload, runtime, category))
        return next(self.outputs)


def analysis(request=None):
    return ProblemAnalysis(
        category=ProblemCategory.PERFORMANCE,
        category_confidence=0.9,
        summary="slow",
        symptoms=["timeout"],
        time_window="10:00",
        environment="prod",
        extracted_evidence=[],
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
    )


def summary():
    return DiagnosisReport(
        executive_summary="pool saturation is the leading hypothesis",
        primary_conclusion=None,
        status_explanation="verified enough",
        next_actions=["inspect pool metric"],
    )


@pytest.mark.asyncio
async def test_completed_graph_keeps_nodes_isolated(tmp_path: Path):
    runner = FakeRunner([analysis(), investigation(), evaluation(), summary()])
    graph = DiagnosisGraph(
        runner, JsonStateStore(tmp_path), PromptRegistry(Path("missing.yml"))
    )
    state = DiagnosisState.create("timeouts", {"tenant_id": "a"}, [])
    result = await graph.start(state)
    assert result.status == "completed"
    assert [call[0] for call in runner.calls] == [
        "analyze",
        "investigate",
        "evaluate",
        "summarize",
    ]
    assert all(isinstance(call[2], NodeRuntimeContext) for call in runner.calls)
    assert runner.calls[2][1].model_dump().keys() == {
        "analysis",
        "investigation",
        "rubric_version",
    }
    assert runner.calls[3][1].model_dump().keys() == {
        "analysis",
        "investigation",
        "evaluation",
        "status",
        "attempts",
    }


@pytest.mark.asyncio
async def test_investigation_clarification_resumes_fresh_investigator(tmp_path: Path):
    request = UserInteractionRequest(
        request_id="ask_1",
        source_node="investigate",
        resume_node="investigate",
        reason="insufficient_evidence",
        explanation="need pool metric",
        questions=[
            ClarificationQuestion(
                id="pool", question="metric?", rationale="confirm saturation"
            )
        ],
    )
    runner = FakeRunner(
        [analysis(), investigation(request), investigation(), evaluation(), summary()]
    )
    store = JsonStateStore(tmp_path)
    graph = DiagnosisGraph(runner, store, PromptRegistry(Path("missing.yml")))
    paused = await graph.start(DiagnosisState.create("timeouts", {}, []))
    assert paused.status == "awaiting_user_input"
    resumed = await graph.resume(
        paused,
        [
            UserAnswer(
                request_id="ask_1", question_id="pool", answer="active=100 max=100"
            )
        ],
    )
    assert resumed.status == "completed"
    assert [call[0] for call in runner.calls] == [
        "analyze",
        "investigate",
        "investigate",
        "evaluate",
        "summarize",
    ]
    assert runner.calls[1][2] is not runner.calls[2][2]


@pytest.mark.asyncio
async def test_graph_enforces_evaluation_gate_even_if_evaluator_claims_pass(
    tmp_path: Path,
):
    weak_evaluation = EvaluationResult(
        passed=True,
        score=88,
        criteria_scores={
            "problem_coverage": 20,
            "evidence_traceability": 10,
            "reasoning_consistency": 20,
            "verification_executability": 18,
            "uncertainty_expression": 15,
        },
    )
    runner = FakeRunner([analysis(), investigation(), weak_evaluation, summary()])
    graph = DiagnosisGraph(
        runner, JsonStateStore(tmp_path), PromptRegistry(Path("missing.yml"))
    )
    result = await graph.start(
        DiagnosisState.create("timeouts", {}, [], max_attempts=1)
    )
    assert result.status == "inconclusive"
    assert result.evaluation is not None and result.evaluation.passed is False


def request(node="investigate"):
    return UserInteractionRequest(
        request_id="ask",
        source_node=node,
        resume_node=node,
        reason="insufficient_evidence",
        explanation="Need measurements",
        questions=[
            ClarificationQuestion(
                id="q", question="Pool usage?", rationale="Distinguish saturation"
            )
        ],
    )


def answer(**changes):
    return UserAnswer(
        **({"request_id": "ask", "question_id": "q", "answer": "active=100"} | changes)
    )


def setup_graph(tmp_path, outputs):
    runner = FakeRunner(outputs)
    graph = DiagnosisGraph(
        runner, JsonStateStore(tmp_path), PromptRegistry(Path("missing.yml"))
    )
    return graph, runner


@pytest.mark.asyncio
async def test_last_attempt_can_resume_after_persisted_pause(tmp_path):
    graph, runner = setup_graph(
        tmp_path,
        [
            analysis(),
            investigation(request()),
            investigation(),
            evaluation(),
            summary(),
        ],
    )
    paused = await graph.start(DiagnosisState.create("timeout", {}, [], max_attempts=1))
    loaded = graph.store.load(paused.run_id)
    done = await graph.resume(loaded, [answer()])
    assert done.status == "completed" and done.attempt == 1
    assert len({id(call[2]) for call in runner.calls}) == len(runner.calls)


@pytest.mark.asyncio
async def test_retry_feedback_and_attempt_limit(tmp_path):
    graph, runner = setup_graph(
        tmp_path,
        [
            analysis(),
            investigation(),
            evaluation(False),
            investigation(),
            evaluation(False),
            summary(),
        ],
    )
    done = await graph.start(DiagnosisState.create("timeout", {}, []))
    assert done.status == "inconclusive" and done.attempt == 2
    assert runner.calls[3][1].previous_evaluation.retry_guidance
    assert done.report.primary_conclusion is None
    assert "尚未确认根因" in done.report.status_explanation


@pytest.mark.asyncio
async def test_analyzer_resumes_without_prior_history(tmp_path):
    graph, runner = setup_graph(
        tmp_path,
        [
            analysis(request("analyze")),
            analysis(),
            investigation(),
            evaluation(),
            summary(),
        ],
    )
    paused = await graph.start(DiagnosisState.create("timeout", {}, []))
    done = await graph.resume(paused, [answer()])
    assert done.status == "completed"
    assert [call[0] for call in runner.calls][:2] == ["analyze", "analyze"]
    assert runner.calls[1][1].clarification_answers[0].answer == "active=100"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answers",
    [
        [answer(request_id="wrong")],
        [answer(question_id="wrong")],
        [answer(answer=" ")],
        [answer(), answer()],
        [],
    ],
)
async def test_invalid_answers_do_not_mutate_state(tmp_path, answers):
    graph, _ = setup_graph(tmp_path, [analysis(request("analyze"))])
    paused = await graph.start(DiagnosisState.create("timeout", {}, []))
    before = paused.model_dump_json()
    with pytest.raises(GraphContractError):
        await graph.resume(paused, answers)
    assert paused.model_dump_json() == before
    assert graph.store.load(paused.run_id).model_dump_json() == before


@pytest.mark.asyncio
async def test_clarification_limit(tmp_path):
    graph, _ = setup_graph(tmp_path, [analysis(request("analyze")), summary()])
    done = await graph.start(
        DiagnosisState.create("timeout", {}, [], max_clarification_rounds=0)
    )
    assert done.status == "inconclusive" and done.pending_interaction is None


@pytest.mark.asyncio
async def test_direct_resume_redacts_answers_and_evidence(tmp_path):
    graph, runner = setup_graph(
        tmp_path,
        [
            analysis(),
            investigation(request()),
            investigation(),
            evaluation(),
            summary(),
        ],
    )
    paused = await graph.start(DiagnosisState.create("timeout", {}, []))
    await graph.resume(
        paused,
        [
            answer(
                answer="token=private person@example.com 13812345678",
                attachments=[
                    Evidence(
                        source="log",
                        content="password=private",
                        reference="token=private",
                    )
                ],
            )
        ],
    )
    serialized = runner.calls[2][1].model_dump_json()
    assert (
        "private" not in serialized
        and "person@example.com" not in serialized
        and "13812345678" not in serialized
    )


@pytest.mark.parametrize(
    "output_type",
    [ProblemAnalysis, InvestigationResult, EvaluationResult, DiagnosisReport],
)
def test_sdk_accepts_strict_output_schema(output_type):
    from agents import AgentOutputSchema

    assert AgentOutputSchema(output_type).is_strict_json_schema()


@pytest.mark.asyncio
async def test_sdk_retry_is_fresh_and_validated(monkeypatch):
    import agents

    sdk = AsyncMock(
        side_effect=[
            SimpleNamespace(final_output={}),
            SimpleNamespace(final_output=analysis()),
        ]
    )
    monkeypatch.setattr(agents.Runner, "run", sdk)
    runner = OpenAINodeRunner(PromptRegistry(Path("missing.yml")))
    result = await runner.run(
        "analyze",
        AnalyzeInput(question="timeout", evidence=[]),
        NodeRuntimeContext("run", "analyze", "v1"),
    )
    assert result.category == analysis().category
    assert sdk.await_count == 2
    first, second = sdk.call_args_list
    assert first.kwargs["context"] is not second.kwargs["context"]
    assert set(first.kwargs) == {"input", "context", "max_turns"}
    assert first.args[0].tools == []


@pytest.mark.asyncio
async def test_sdk_invalid_output_stops_after_one_retry(monkeypatch):
    import agents

    sdk = AsyncMock(return_value=SimpleNamespace(final_output={}))
    monkeypatch.setattr(agents.Runner, "run", sdk)
    with pytest.raises(NodeExecutionError):
        await OpenAINodeRunner(PromptRegistry(Path("missing.yml"))).run(
            "analyze",
            AnalyzeInput(question="timeout", evidence=[]),
            NodeRuntimeContext("run", "analyze", "v1"),
        )
    assert sdk.await_count == 2


def test_api_pause_get_resume_and_replay(tmp_path):
    graph, _ = setup_graph(
        tmp_path,
        [
            analysis(request("analyze")),
            analysis(),
            investigation(),
            evaluation(),
            summary(),
        ],
    )
    with TestClient(api.create_app(DiagnosisService(graph))) as client:
        created = client.post("/diagnoses", json={"question": "timeout"})
        assert created.status_code == 201
        path = "/diagnoses/" + created.json()["run_id"]
        assert "executive_summary" not in created.json()
        assert client.get(path).json() == created.json()
        body = {
            "request_id": "ask",
            "answers": [{"question_id": "q", "answer": "pool full"}],
        }
        result = client.post(path + "/answers", json=body)
        assert result.status_code == 200 and result.json()["status"] == "completed"
        assert result.json()["primary_conclusion"] is None
        assert len(result.json()["alternative_hypotheses"]) == 1
        assert client.post(path + "/answers", json=body).status_code == 422
        assert client.get("/diagnoses/invalid!").status_code == 404
        assert (
            client.post(
                "/diagnoses", json={"question": "timeout", "max_attempts": 3}
            ).status_code
            == 422
        )


def test_api_cancel(tmp_path):
    graph, _ = setup_graph(tmp_path, [analysis(request("analyze")), summary()])
    with TestClient(api.create_app(DiagnosisService(graph))) as client:
        run = client.post("/diagnoses", json={"question": "timeout"}).json()["run_id"]
        result = client.post(f"/diagnoses/{run}/cancel")
        assert result.status_code == 200 and result.json()["status"] == "inconclusive"


def test_health_endpoint(tmp_path):
    graph, _ = setup_graph(tmp_path, [])
    with TestClient(api.create_app(DiagnosisService(graph))) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_settings_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("BUGLENS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("BUGLENS_PROMPT_CONFIG", "config/prompts.yaml")
    monkeypatch.setenv("BUGLENS_HOST", "0.0.0.0")
    monkeypatch.setenv("BUGLENS_PORT", "9000")
    monkeypatch.setenv("BUGLENS_LOG_LEVEL", "DEBUG")
    settings = Settings.from_env()
    assert settings.state_dir == tmp_path
    assert settings.prompt_config == Path("config/prompts.yaml")
    assert (settings.host, settings.port, settings.log_level) == (
        "0.0.0.0",
        9000,
        "debug",
    )


@pytest.mark.parametrize("port", ["invalid", "0", "65536"])
def test_settings_reject_invalid_port(monkeypatch, port):
    monkeypatch.setenv("BUGLENS_PORT", port)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_api_provider_failure_is_persisted_without_details(tmp_path):
    graph, runner = setup_graph(tmp_path, [])
    runner.run = AsyncMock(side_effect=NodeExecutionError("secret provider details"))
    with TestClient(api.create_app(DiagnosisService(graph))) as client:
        response = client.post("/diagnoses", json={"question": "timeout"})
        assert response.status_code == 502 and "secret" not in response.text
        run = response.json()["detail"]["run_id"]
        assert client.get(f"/diagnoses/{run}").json()["status"] == "inconclusive"


def test_score_ranges_and_total_are_enforced():
    data = evaluation().model_dump()
    data["criteria_scores"]["problem_coverage"] = 100
    with pytest.raises(ValidationError):
        EvaluationResult.model_validate(data)
    result = evaluation().model_copy(update={"score": 100}).enforce_rubric()
    assert result.score == 85


def test_app_instances_use_independent_services(tmp_path):
    first, _ = setup_graph(tmp_path / "first", [analysis(request("analyze"))])
    second, _ = setup_graph(tmp_path / "second", [])
    with (
        TestClient(api.create_app(DiagnosisService(first))) as client_a,
        TestClient(api.create_app(DiagnosisService(second))) as client_b,
    ):
        created = client_a.post("/diagnoses", json={"question": "timeout"}).json()
        path = "/diagnoses/" + created["run_id"]
        assert client_a.get(path).status_code == 200
        assert client_b.get(path).status_code == 404


@pytest.mark.parametrize(
    "kind,value,valid",
    [
        ("single_select", "a", True),
        ("single_select", "c", False),
        ("multi_select", '["a", "b"]', True),
        ("multi_select", '["c"]', False),
        ("multi_select", '"a"', False),
        ("multi_select", "invalid JSON", False),
    ],
)
def test_clarification_model_validates_selections(kind, value, valid):
    interaction = request()
    interaction.questions[0].answer_type = kind
    interaction.questions[0].options = ["a", "b"]
    if valid:
        interaction.validate_answers([answer(answer=value)])
    else:
        with pytest.raises(GraphContractError):
            interaction.validate_answers([answer(answer=value)])
