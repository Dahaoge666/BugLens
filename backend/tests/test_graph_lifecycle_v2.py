import asyncio
from pathlib import Path

import pytest
from agents.tool_context import ToolContext

from app.agents import NodeExecutionError
from app.config import ConfigRepository
from app.graph import DiagnosisGraph
from app.infra import (
    InvalidRunStatusError,
    SQLiteCheckpointStore,
    ValidationFailedError,
)
from app.models import (
    ClarificationQuestion,
    DiagnosisReport,
    DiagnosisState,
    EvaluationResult,
    EvidenceRecord,
    InvestigationResult,
    ProblemAnalysis,
    ProblemCategory,
    RootCauseHypothesis,
    UserAnswer,
    UserInteractionRequest,
    replace_state,
)
from app.prompts import PromptRegistry
from app.protocol.commands import (
    ResumeDiagnosis,
    SkipUserInteraction,
    StartDiagnosis,
    SubmitUserAnswers,
)
from app.protocol.events import NodeAttemptStarted, RunStarted
from app.runtime import DiagnosisRuntime
from app.tools import ToolExecutionError, ToolRegistry


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
                supporting_evidence=["log"],
                confidence=0.7,
                verification_steps=["inspect pool metric"],
            )
        ],
        primary_conclusion="connection pool saturation",
        interaction_request=request,
    )


def evaluation(passed=True, request=None):
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
        interaction_request=request,
    )


def report():
    return DiagnosisReport(
        executive_summary="pool saturation",
        primary_conclusion="connection pool saturation",
        status_explanation="supported",
        next_actions=["inspect pool metric"],
    )


def request(node: str, *, evaluate=False):
    return UserInteractionRequest(
        request_id=f"ask-{node}",
        source_node=node,
        resume_node="investigate" if evaluate else node,
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


async def collect(runtime, command):
    return [event async for event in runtime.run(command)]


def make_runtime(tmp_path, outputs):
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
async def test_runtime_drives_one_command_to_terminal_and_audits_each_attempt(tmp_path):
    runtime, runner, store = make_runtime(
        tmp_path, [analysis(), investigation(), evaluation(), report()]
    )
    try:
        events = await collect(
            runtime, StartDiagnosis(run_id="run-v2", question="timeouts")
        )
        state = await runtime.get_run("run-v2")
        assert state.lifecycle_status.value == "completed"
        assert state.outcome.value == "confirmed"
        assert [item[0] for item in runner.calls] == [
            "analyze",
            "investigate",
            "evaluate",
            "summarize",
        ]
        assert [
            row["status"] for row in store.list_node_executions(run_id="run-v2")
        ] == [
            "succeeded",
            "succeeded",
            "succeeded",
            "succeeded",
        ]
        assert len(store.list_state_history("run-v2")) == state.revision + 1
        assert events[-1].event_type == "run_completed"
        assert events[-1].protocol_version == "2"
        assert events[-1].specversion == "1.0"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_replay_continues_after_command_checkpoint_before_first_node(tmp_path):
    runtime, runner, store = make_runtime(
        tmp_path, [analysis(), investigation(), evaluation(), report()]
    )
    config = ConfigRepository(None).resolve("default")
    command = StartDiagnosis(
        run_id="run-replay", command_id="cmd-replay", question="timeouts"
    )
    store.save_config_snapshot(config)
    state = DiagnosisState.create(
        command.question,
        command.context,
        command.evidence,
        config.policy.graph.max_investigation_attempts,
        config.policy.graph.max_clarification_rounds,
        config_snapshot_id=config.snapshot_id,
        profile=config.profile,
        config_version=config.config_version,
    )
    state = replace_state(
        state,
        run_id=command.run_id,
        lifecycle_status="running",
    )
    store.create_run(
        state,
        [
            RunStarted(
                run_id=state.run_id,
                sequence=1,
                revision=state.revision,
                profile=config.profile,
                config_snapshot_id=config.snapshot_id,
                config_version=config.config_version,
            )
        ],
        command,
    )
    try:
        replayed = await collect(runtime, command)
        final = await runtime.get_run(command.run_id)
        assert final.lifecycle_status.value == "completed"
        assert replayed[0].event_type == "run_started"
        assert [item[0] for item in runner.calls] == [
            "analyze",
            "investigate",
            "evaluate",
            "summarize",
        ]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_skip_records_unavailable_information_and_continues(tmp_path):
    runtime, runner, store = make_runtime(
        tmp_path,
        [
            analysis(request("analyze")),
            analysis(),
            investigation(),
            evaluation(),
            report(),
        ],
    )
    try:
        await collect(runtime, StartDiagnosis(run_id="run-skip", question="timeouts"))
        waiting = await runtime.get_run("run-skip")
        assert waiting.lifecycle_status.value == "waiting_user"
        assert waiting.available_actions == ["submit_answers", "skip_input", "cancel"]
        await collect(
            runtime,
            SkipUserInteraction(
                run_id="run-skip",
                expected_revision=waiting.revision,
                request_id=waiting.pending_interaction.request_id,
                reason="not available",
            ),
        )
        final = await runtime.get_run("run-skip")
        assert final.lifecycle_status.value == "completed"
        assert len(final.skipped_interactions) == 1
        assert final.skipped_interactions[0].reason == "not available"
        assert final.answers == []
        assert isinstance(runner.calls[1][1].information_unavailable, bool)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_evaluate_clarification_resumes_investigate(tmp_path):
    evaluate_request = request("evaluate", evaluate=True)
    runtime, runner, store = make_runtime(
        tmp_path,
        [
            analysis(),
            investigation(),
            evaluation(request=evaluate_request),
            investigation(),
            evaluation(),
            report(),
        ],
    )
    try:
        await collect(runtime, StartDiagnosis(run_id="run-eval", question="timeouts"))
        waiting = await runtime.get_run("run-eval")
        assert waiting.pending_interaction.source_node == "evaluate"
        await collect(
            runtime,
            SubmitUserAnswers(
                run_id="run-eval",
                expected_revision=waiting.revision,
                request_id=waiting.pending_interaction.request_id,
                answers=[
                    UserAnswer(
                        request_id=waiting.pending_interaction.request_id,
                        question_id="metric",
                        answer="100/100",
                    )
                ],
            ),
        )
        final = await runtime.get_run("run-eval")
        assert final.lifecycle_status.value == "completed"
        assert final.clarification_rounds["evaluate"] == 1
        assert [item[0] for item in runner.calls] == [
            "analyze",
            "investigate",
            "evaluate",
            "investigate",
            "evaluate",
            "summarize",
        ]
        assert runner.calls[3][1].answers[0].source_node == "evaluate"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_retry_exhaustion_offers_resume_and_resume_starts_new_cycle(tmp_path):
    runtime, runner, store = make_runtime(
        tmp_path,
        [NodeExecutionError("temporary", code="connector_timeout", retryable=True)] * 6
        + [analysis(), investigation(), evaluation(), report()],
    )
    try:
        await collect(runtime, StartDiagnosis(run_id="run-retry", question="timeouts"))
        failed = await runtime.get_run("run-retry")
        assert failed.lifecycle_status.value == "failed"
        assert failed.resume_available is True
        assert failed.available_actions == ["resume"]
        assert len(store.list_node_executions(run_id="run-retry")) == 6
        await collect(
            runtime,
            ResumeDiagnosis(run_id="run-retry", expected_revision=failed.revision),
        )
        final = await runtime.get_run("run-retry")
        assert final.lifecycle_status.value == "completed"
        assert final.retry_cycle == 1
        assert len(store.list_node_executions(run_id="run-retry")) == 10
        assert runner.calls[6][0] == "analyze"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_stale_running_execution_becomes_resumable_failure(tmp_path):
    runtime, _runner, store = make_runtime(tmp_path, [])
    configs = ConfigRepository(None)
    config = configs.resolve("default")
    command = StartDiagnosis(run_id="run-crash", question="timeouts")
    store.save_config_snapshot(config)
    state = DiagnosisState.create(
        command.question,
        command.context,
        command.evidence,
        config.policy.graph.max_investigation_attempts,
        config.policy.graph.max_clarification_rounds,
        config_snapshot_id=config.snapshot_id,
        profile=config.profile,
        config_version=config.config_version,
    )
    state = replace_state(
        state,
        run_id=command.run_id,
        lifecycle_status="running",
    )
    started = RunStarted(
        run_id=state.run_id,
        sequence=1,
        revision=state.revision,
        profile=config.profile,
        config_snapshot_id=config.snapshot_id,
        config_version=config.config_version,
    )
    store.create_run(state, [started], command)
    owner = "crashed-process"
    token = store.acquire_lease(state.run_id, owner, 60)
    plan = runtime.graph.prepare_step(state, config)
    active = replace_state(
        state,
        active_execution_id=plan.execution_id,
        revision=state.revision + 1,
        lifecycle_status="running",
    )
    node_started = NodeAttemptStarted(
        run_id=state.run_id,
        sequence=2,
        revision=active.revision,
        node=plan.node,
        execution_id=plan.execution_id,
        session_id=plan.session_id,
        investigation_attempt=plan.investigation_attempt,
        clarification_round=plan.clarification_round,
        retry_cycle=plan.retry_cycle,
        retry_index=plan.retry_index,
    )
    store.start_node_execution(
        active,
        state.revision,
        plan,
        node_started,
        lease_owner=owner,
        fencing_token=token,
    )
    store.release_lease(state.run_id, owner, token)
    try:
        recovered = await runtime.get_run(state.run_id)
        assert recovered.lifecycle_status.value == "failed"
        assert recovered.last_error.code == "process_interrupted"
        assert recovered.available_actions == ["resume"]
        assert (
            store.list_node_executions(run_id=state.run_id)[0]["status"]
            == "interrupted"
        )
    finally:
        store.close()


def test_tool_registry_uses_strict_schema_and_policy_filters():
    def lookup(query: str) -> EvidenceRecord:
        return EvidenceRecord(source="fake", content=query)

    registry = ToolRegistry(enabled=True, allowed_nodes={"analyze"})
    tool = registry.register(lookup, name="fake_lookup", timeout_seconds=7)
    assert tool.strict_json_schema is True
    selected = registry.tools_for(node_name="analyze", timeout_seconds=11)
    assert selected[0].timeout_seconds == 11
    assert registry.tools_for(node_name="investigate") == []


@pytest.mark.asyncio
async def test_tool_failure_reaches_runtime_as_structured_error():
    async def failing_lookup(query: str) -> str:
        raise RuntimeError(f"connector failed for {query}")

    tool = ToolRegistry(enabled=True).register(failing_lookup, name="failing_lookup")
    context = ToolContext(
        context=None,
        tool_name=tool.name,
        tool_call_id="call-1",
        tool_arguments='{"query":"x"}',
    )
    with pytest.raises(ToolExecutionError) as error:
        await tool.on_invoke_tool(context, '{"query":"x"}')
    assert error.value.code == "tool_execution_failed"


@pytest.mark.asyncio
async def test_state_and_config_audits_are_secret_free(tmp_path):
    runtime, _runner, store = make_runtime(
        tmp_path, [analysis(), investigation(), evaluation(), report()]
    )
    secret = "sk-test-secret-value"
    config = ConfigRepository(None).resolve("default")
    model = config.policy.models["default"].model_copy(update={"api_key": secret})
    config = config.model_copy(
        update={
            "policy": config.policy.model_copy(update={"models": {"default": model}})
        }
    )
    try:
        store.save_config_snapshot(config)
        await collect(
            runtime,
            StartDiagnosis(
                run_id="run-secret",
                question=f"api_key={secret}",
                evidence=[EvidenceRecord(source="log", content=f"api_key={secret}")],
            ),
        )
        state_row = store.db.execute(
            "SELECT state_json FROM diagnosis_runs WHERE run_id = ?", ("run-secret",)
        ).fetchone()
        snapshot_row = store.db.execute(
            "SELECT config_json FROM run_config_snapshots WHERE snapshot_id = ?",
            (config.snapshot_id,),
        ).fetchone()
        event_rows = store.db.execute(
            "SELECT event_json FROM diagnosis_events WHERE run_id = ?",
            ("run-secret",),
        ).fetchall()
        assert secret not in state_row["state_json"]
        assert secret not in snapshot_row["config_json"]
        assert all(secret not in row["event_json"] for row in event_rows)
        assert "[REDACTED]" in state_row["state_json"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_command_collision_and_terminal_cancel_are_rejected(tmp_path):
    runtime, _runner, store = make_runtime(
        tmp_path, [analysis(), investigation(), evaluation(), report()]
    )
    try:
        command = StartDiagnosis(
            run_id="run-command-one", command_id="shared-command", question="q"
        )
        await collect(runtime, command)
        with pytest.raises(ValidationFailedError):
            await collect(
                runtime,
                StartDiagnosis(
                    run_id="run-command-two",
                    command_id="shared-command",
                    question="q",
                ),
            )
        state = await runtime.get_run("run-command-one")
        from app.protocol.commands import CancelDiagnosis

        with pytest.raises(InvalidRunStatusError):
            await collect(
                runtime,
                CancelDiagnosis(
                    run_id=state.run_id,
                    expected_revision=state.revision,
                    reason="too late",
                ),
            )
    finally:
        store.close()
