import json
from pathlib import Path
from types import SimpleNamespace

import httpx2 as httpx
import pytest
from agents import (
    OpenAIChatCompletionsModel,
    RunContextWrapper,
    ToolInputGuardrailData,
    ToolOutputGuardrailData,
)
from agents.tool_context import ToolContext
from openai import AsyncOpenAI
from pydantic import TypeAdapter

from app.admin import AdminApplicationService, ConfigMutation
from app.agents import (
    NodeApprovalRequired,
    NodeRuntimeContext,
    OpenAINodeRunner,
    _schema_hint,
)
from app.config import ConfigRepository, ModelConfig, Settings
from app.graph import DiagnosisGraph
from app.infra import SQLiteCheckpointStore
from app.models import (
    DiagnosisReport,
    EvaluationResult,
    InvestigationResult,
    ProblemAnalysis,
    ProblemCategory,
    RootCauseHypothesis,
)
from app.prompts import PromptRegistry
from app.protocol.commands import ApproveTool, StartDiagnosis
from app.runtime import DiagnosisRuntime
from app.security import SDKRunStateCipher
from app.tools import ToolRegistry


def analysis() -> ProblemAnalysis:
    return ProblemAnalysis(
        category=ProblemCategory.PERFORMANCE,
        category_confidence=0.9,
        summary="database pool is saturated",
    )


def investigation() -> InvestigationResult:
    return InvestigationResult(
        investigation_summary="the pool saturation hypothesis is testable",
        hypotheses=[
            RootCauseHypothesis(
                rank=1,
                cause="connection pool saturation",
                rationale="requests wait for an available connection",
                supporting_evidence=["log-1"],
                confidence=0.8,
                verification_steps=["inspect pool wait metrics"],
            )
        ],
    )


def evaluation() -> EvaluationResult:
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


def report() -> DiagnosisReport:
    return DiagnosisReport(
        executive_summary="the report is supported by the supplied evidence",
        primary_conclusion="connection pool saturation",
        status_explanation="the evaluation passed",
    )


@pytest.mark.parametrize(
    "output_type",
    [ProblemAnalysis, InvestigationResult, EvaluationResult, DiagnosisReport],
)
def test_schema_hint_contains_a_strict_json_schema(output_type):
    hint = _schema_hint(output_type)
    assert hint and '"properties"' in hint


def test_native_node_guardrails_validate_input_and_output_contracts(tmp_path):
    runner = OpenAINodeRunner(
        PromptRegistry(Path("missing.yml")),
        tmp_path / "sessions.db",
    )
    runtime = NodeRuntimeContext(
        graph_run_id="run-guardrail",
        node_name="analyze",
        config_version="v1",
    )
    agent = runner._agent("analyze", None, None, "gpt-test")
    context = RunContextWrapper(runtime)

    assert len(agent.input_guardrails) == 1
    assert len(agent.output_guardrails) == 1
    valid_input = json.dumps(
        {"question": "为什么慢？", "context": {}, "evidence": []},
        ensure_ascii=False,
    )
    valid = agent.input_guardrails[0].guardrail_function(context, agent, valid_input)
    valid_message_list = agent.input_guardrails[0].guardrail_function(
        context,
        agent,
        [{"role": "user", "content": valid_input}],
    )
    blocked_input = agent.input_guardrails[0].guardrail_function(
        context, agent, json.dumps({"unexpected": True})
    )
    assert valid.tripwire_triggered is False
    assert valid_message_list.tripwire_triggered is False
    assert blocked_input.tripwire_triggered is True

    blocked_output = agent.output_guardrails[0].guardrail_function(
        context, agent, {"not": "a ProblemAnalysis"}
    )
    assert blocked_output.tripwire_triggered is True

    run_config = runner._run_config(runtime)
    assert run_config.tool_execution is not None
    assert run_config.tool_execution.pre_approval_tool_input_guardrails is True


def test_native_tool_guardrails_fail_closed_at_input_and_output_boundaries():
    async def lookup(query: str) -> str:
        return query

    registry = ToolRegistry(enabled=True, allowed_nodes={"analyze"})
    tool = registry.register(lookup, name="lookup", nodes={"analyze"})
    assert len(tool.tool_input_guardrails) == 1
    assert len(tool.tool_output_guardrails) == 1

    runtime = NodeRuntimeContext(
        graph_run_id="run-tool-guardrail",
        node_name="analyze",
        config_version="v1",
        tool_registry=registry,
        tools_enabled=True,
        tool_allowed_nodes=frozenset({"analyze"}),
        tool_result_bytes=32,
    )
    context = ToolContext(
        runtime,
        tool_name="lookup",
        tool_call_id="call-1",
        tool_arguments='{"query":"pool"}',
    )
    accepted_input = tool.tool_input_guardrails[0].guardrail_function(
        ToolInputGuardrailData(context=context, agent=None)
    )
    rejected_output = tool.tool_output_guardrails[0].guardrail_function(
        ToolOutputGuardrailData(
            context=context,
            agent=None,
            output={"value": "x" * 64},
        )
    )
    blocked_context = ToolContext(
        NodeRuntimeContext(
            graph_run_id="run-tool-guardrail",
            node_name="analyze",
            config_version="v1",
            tool_registry=registry,
            tools_enabled=False,
            tool_allowed_nodes=frozenset({"analyze"}),
        ),
        tool_name="lookup",
        tool_call_id="call-2",
        tool_arguments='{"query":"pool"}',
    )
    rejected_input = tool.tool_input_guardrails[0].guardrail_function(
        ToolInputGuardrailData(context=blocked_context, agent=None)
    )

    assert accepted_input.behavior["type"] == "allow"
    assert rejected_output.behavior["type"] == "raise_exception"
    assert rejected_input.behavior["type"] == "raise_exception"


def test_legacy_user_context_is_promoted_when_loading_a_v1_state():
    from app.models import DiagnosisState

    state = DiagnosisState.model_validate(
        {
            "run_id": "legacy-run",
            "user_question": "why is it slow?",
            "user_context": {"environment": "production", "tenant_id": "acme"},
        }
    )

    assert state.context.environment == "production"
    assert state.context.attributes == {"tenant_id": "acme"}


@pytest.mark.parametrize(
    ("model_text", "expected"),
    [
        ('```json\n{"count": 2}\n```', {"count": 2}),
        ('Here is the result:\n{"count": 3}\nThanks.', {"count": 3}),
        ('{"count": 4}', {"count": 4}),
    ],
)
def test_json_coercion_handles_gateway_wrappers(model_text, expected):
    import agents.util._json as sdk_json

    assert (
        sdk_json.validate_json(model_text, TypeAdapter(dict[str, int]), partial=False)
        == expected
    )


def test_json_coercion_does_not_invent_json_for_plain_text():
    import agents.util._json as sdk_json

    with pytest.raises(Exception):
        sdk_json.validate_json(
            "the provider returned no structured result",
            TypeAdapter(dict[str, int]),
            partial=False,
        )


def test_json_coercion_installation_is_idempotent():
    import importlib

    import agents.util._json as sdk_json

    import app.agents as agents_adapter

    patched = sdk_json.validate_json
    importlib.reload(agents_adapter)
    assert sdk_json.validate_json is patched


class _MockOpenAIGateway:
    """A local chat-completions transport that deliberately ignores response_format."""

    def __init__(self):
        self.requests: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"path": request.url.path, "body": body})
        content = analysis().model_dump_json()
        if body.get("stream"):
            pieces = [content[: len(content) // 2], content[len(content) // 2 :]]
            lines = []
            for piece in pieces:
                lines.append(
                    "data: "
                    + json.dumps(
                        {
                            "id": "chatcmpl-mock",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "gateway-model",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": piece},
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )
                )
            lines.extend(
                [
                    "data: "
                    + json.dumps(
                        {
                            "id": "chatcmpl-mock",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "gateway-model",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                    ),
                    "data: [DONE]",
                    "",
                ]
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=("\n\n".join(lines)).encode(),
            )

        # The gateway ignores response_format and wraps the valid object in prose
        # and a Markdown fence. The adapter's bounded coercion must still parse it.
        wrapped = f"Here is the result:\n```json\n{content}\n```\n"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": 1,
                "model": "gateway-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": wrapped},
                        "finish_reason": "stop",
                    }
                ],
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_compatibility_gateway_uses_chat_completions_and_parses_ignored_schema(
    monkeypatch, tmp_path, streaming
):
    gateway = _MockOpenAIGateway()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(gateway.handle))
    client = AsyncOpenAI(
        api_key="sk-gateway",
        base_url="http://gateway.test/v1",
        max_retries=0,
        http_client=http_client,
    )
    model = OpenAIChatCompletionsModel("gateway-model", client)
    runner = OpenAINodeRunner(
        PromptRegistry(Path("missing.yml")),
        tmp_path / "sessions.db",
        default_streaming=streaming,
    )
    monkeypatch.setattr(runner, "_model_instance", lambda _config: model)
    try:
        from app.models import AnalyzeInput

        result = await runner.run(
            "analyze",
            AnalyzeInput(question="why is the database slow?"),
            NodeRuntimeContext(
                "gateway-run",
                "analyze",
                "v1",
                model="gateway-model",
                model_config=ModelConfig(
                    model="gateway-model",
                    base_url="http://gateway.test/v1",
                    api_key="sk-gateway",
                    streaming=streaming,
                ),
                max_retries=0,
            ),
        )
    finally:
        runner.close()
        await client.close()

    assert result == analysis()
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["body"].get("stream", False) is streaming
    assert request["body"]["response_format"]["type"] == "json_schema"
    assert "properties" in request["body"]["response_format"]["json_schema"]["schema"]


class _StreamResult:
    final_output = analysis()
    interruptions = []
    run_loop_exception = None
    new_items = []

    async def stream_events(self):
        yield SimpleNamespace()


@pytest.mark.asyncio
async def test_streaming_switch_uses_the_selected_runner_method(monkeypatch, tmp_path):
    streamed_calls = []
    normal_calls = []

    def streamed(*args, **kwargs):
        streamed_calls.append((args, kwargs))
        return _StreamResult()

    async def normal(*args, **kwargs):
        normal_calls.append((args, kwargs))
        return SimpleNamespace(final_output=analysis(), interruptions=[], new_items=[])

    monkeypatch.setattr("app.agents.Runner.run_streamed", streamed)
    monkeypatch.setattr("app.agents.Runner.run", normal)
    runner = OpenAINodeRunner(
        PromptRegistry(Path("missing.yml")),
        tmp_path / "sessions.db",
        streaming=True,
    )
    try:
        from app.models import AnalyzeInput

        await runner.run(
            "analyze",
            AnalyzeInput(question="slow"),
            NodeRuntimeContext("r", "analyze", "v"),
        )
        runner.default_streaming = False
        await runner.run(
            "analyze",
            AnalyzeInput(question="slow again"),
            NodeRuntimeContext("r", "analyze", "v"),
        )
    finally:
        runner.close()
    assert len(streamed_calls) == 1
    assert len(normal_calls) == 1


@pytest.mark.asyncio
async def test_sdk_approval_resume_restores_state_and_applies_decision(
    monkeypatch, tmp_path
):
    class FakeState:
        def __init__(self):
            self.items = [SimpleNamespace(call_id="call-1")]
            self.approved = []
            self.rejected = []

        def get_interruptions(self):
            return self.items

        def approve(self, item):
            self.approved.append(item)

        def reject(self, item, **kwargs):
            self.rejected.append((item, kwargs))

    restored = FakeState()

    async def from_string(agent, state_string, *, context_override):
        assert state_string == "encrypted-state-plaintext"
        assert context_override.node_name == "analyze"
        return restored

    async def run(*args, **kwargs):
        assert kwargs["input"] is restored
        return SimpleNamespace(final_output=analysis(), interruptions=[], new_items=[])

    monkeypatch.setattr("app.agents.RunState.from_string", from_string)
    monkeypatch.setattr("app.agents.Runner.run", run)
    runner = OpenAINodeRunner(
        PromptRegistry(Path("missing.yml")), tmp_path / "sessions.db"
    )
    try:
        from app.models import AnalyzeInput

        result = await runner.resume_approval(
            "analyze",
            AnalyzeInput(question="slow"),
            NodeRuntimeContext("run", "analyze", "v1"),
            state_string="encrypted-state-plaintext",
            decision="approved",
            expected_tool_call_ids=["call-1"],
        )
    finally:
        runner.close()
    assert result == analysis()
    assert len(restored.approved) == 1 and not restored.rejected


def test_model_config_binds_a_dedicated_client_and_cache(tmp_path):
    runner = OpenAINodeRunner(
        PromptRegistry(Path("missing.yml")), tmp_path / "sessions.db"
    )
    config = ModelConfig(
        model="gateway-model",
        base_url="http://gateway.test/v1",
        api_key="sk-gateway",
        timeout=7,
        streaming=True,
    )
    try:
        first = runner._model_instance(config)
        second = runner._model_instance(config)
        assert first is second
        assert first.model == "gateway-model"
        assert str(first._client.base_url).rstrip("/") == "http://gateway.test/v1"
        assert first._client.api_key == "sk-gateway"
        assert first._client.timeout == 7
        assert runner._streaming_for(
            NodeRuntimeContext("r", "analyze", "v", model_config=config)
        )
        assert runner._streaming_for(NodeRuntimeContext("r", "analyze", "v")) is False
    finally:
        runner.close()


def test_admin_masks_and_backfills_model_keys(tmp_path):
    from app.bootstrap import build_local_service

    service, store, runner = build_local_service(
        Settings(session_db=tmp_path / "state.db", config_path=tmp_path / "config.yaml")
    )
    secret = "sk-real-secret"
    try:
        current = service.configs.profiles()["default"]
        candidate = json.loads(json.dumps(current))
        candidate["models"] = {"default": {"model": "gpt-test", "api_key": secret}}
        candidate["config_version"] = "default-key-v2"
        service.configs.apply_profile("default", candidate, service.configs.revision)
        view = AdminApplicationService(service).config()
        assert view.profiles["default"]["models"]["default"]["api_key"] == "sk-****cret"

        admin = AdminApplicationService(service)
        masked = json.loads(json.dumps(view.profiles["default"]))
        masked["config_version"] = "default-key-v3"
        updated = admin.apply_config(
            ConfigMutation(
                profile="default",
                config=masked,
                expected_revision=view.revision,
            )
        )
        assert (
            updated.profiles["default"]["models"]["default"]["api_key"] == "sk-****cret"
        )
        assert (
            service.configs.resolve("default").policy.models["default"].api_key
            == secret
        )
    finally:
        runner.close()
        store.close()


def test_admin_lists_skip_corrupt_states_and_report_degradation(tmp_path):
    from app.bootstrap import build_local_service
    from app.models import DiagnosisState, replace_state
    from app.protocol.events import RunStarted

    service, store, runner = build_local_service(
        Settings(session_db=tmp_path / "state.db", config_path=tmp_path / "config.yaml")
    )
    try:
        config = service.configs.resolve("default")
        store.save_config_snapshot(config)
        state = replace_state(
            DiagnosisState.create(
                "valid",
                {},
                [],
                config.policy.graph.max_investigation_attempts,
                config.policy.graph.max_clarification_rounds,
                config_snapshot_id=config.snapshot_id,
                profile=config.profile,
                config_version=config.config_version,
            ),
            run_id="valid-run",
            lifecycle_status="running",
        )
        start = StartDiagnosis(run_id=state.run_id, question=state.user_question)
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
            start,
        )
        store.db.execute(
            "INSERT INTO diagnosis_runs(run_id, revision, state_json, updated_at) VALUES (?, ?, ?, ?)",
            ("bad-run", 0, "{not valid", "2026-09-08T00:00:00+00:00"),
        )
        store.db.commit()
        admin = AdminApplicationService(service)
        runs = admin.runs()
        assert runs.total == 1 and runs.degraded_count == 1
        store.db.executescript(
            """CREATE TABLE agent_sessions (
                session_id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT
            );
            INSERT INTO agent_sessions VALUES ('valid-run:analyze', 'now', 'now');"""
        )
        store.db.commit()
        sessions = admin.sessions()
        assert sessions.total == 1 and sessions.degraded_count == 1
    finally:
        runner.close()
        store.close()


def test_reasoning_audit_keeps_provider_summary_only():
    from agents import Agent
    from agents.items import ReasoningItem
    from openai.types.responses import ResponseReasoningItem

    captured: list[list[str]] = []
    runtime = NodeRuntimeContext(
        "run",
        "analyze",
        "v1",
        execution_observer=SimpleNamespace(
            reasoning_captured=lambda summaries: captured.append(summaries)
        ),
    )
    item = ReasoningItem(
        Agent(name="test", instructions="test"),
        ResponseReasoningItem(
            id="reason-1",
            type="reasoning",
            summary=[{"type": "summary_text", "text": "compare the supplied evidence"}],
            content=[{"type": "reasoning_text", "text": "hidden chain"}],
        ),
    )
    OpenAINodeRunner._record_reasoning(SimpleNamespace(new_items=[item]), runtime)
    assert captured == [["compare the supplied evidence"]]
    assert "hidden chain" not in json.dumps(captured)


class _ApprovalRunner:
    def __init__(self, *, interrupt: bool):
        self.interrupt = interrupt
        self.calls: list[str] = []
        self.resume_calls: list[dict] = []

    async def run(self, node_name, payload, runtime, category=None):
        self.calls.append(node_name)
        if node_name == "analyze" and self.interrupt:
            self.interrupt = False
            raise NodeApprovalRequired(
                request_id="approval-test",
                state_string="opaque-sdk-state",
                tool_name="lookup_readonly",
                tool_call_ids=["call-1"],
                arguments={"lookup_readonly.query": "pool"},
                explanation="只读工具调用需要你的审批：lookup_readonly",
            )
        return {
            "analyze": analysis,
            "investigate": investigation,
            "evaluate": evaluation,
            "summarize": report,
        }[node_name]()

    async def resume_approval(self, node_name, payload, runtime, **kwargs):
        self.resume_calls.append(kwargs)
        return analysis()


@pytest.mark.asyncio
async def test_tool_approval_is_encrypted_and_resumable_by_a_new_runtime(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "state.db")
    prompts = PromptRegistry(Path("missing.yml"))
    first_runner = _ApprovalRunner(interrupt=True)
    config = ConfigRepository(None)
    first = DiagnosisRuntime(
        DiagnosisGraph(first_runner, prompts, tracing_enabled=False),
        store,
        config,
        tracing_enabled=False,
        run_state_cipher=SDKRunStateCipher("approval-secret"),
        sleep=lambda _delay: __import__("asyncio").sleep(0),
    )
    try:
        started = [
            event
            async for event in first.run(
                StartDiagnosis(run_id="approval-run", question="why slow")
            )
        ]
        waiting = await first.get_run("approval-run")
        assert waiting.lifecycle_status.value == "waiting_approval"
        assert waiting.pending_approval.tool_name == "lookup_readonly"
        row = store.get_sdk_run_state("approval-run", "approval-test")
        assert row is not None
        assert row["encrypted_state"] != "opaque-sdk-state"
        assert SDKRunStateCipher("approval-secret").decrypt(row["encrypted_state"]) == (
            "opaque-sdk-state"
        )
        assert any(event.event_type == "tool_approval_required" for event in started)
        assert store.list_node_executions(run_id="approval-run")[0]["status"] == (
            "waiting_approval"
        )

        second_runner = _ApprovalRunner(interrupt=False)
        second = DiagnosisRuntime(
            DiagnosisGraph(second_runner, prompts, tracing_enabled=False),
            store,
            config,
            tracing_enabled=False,
            run_state_cipher=SDKRunStateCipher("approval-secret"),
            sleep=lambda _delay: __import__("asyncio").sleep(0),
        )
        approved = [
            event
            async for event in second.run(
                ApproveTool(
                    run_id="approval-run",
                    expected_revision=waiting.revision,
                    request_id="approval-test",
                )
            )
        ]
        final = await second.get_run("approval-run")
        assert final.lifecycle_status.value == "completed"
        assert second_runner.resume_calls[0]["decision"] == "approved"
        assert second_runner.resume_calls[0]["expected_tool_call_ids"] == ["call-1"]
        assert any(event.event_type == "tool_approval_resolved" for event in approved)
        approval_row = store.get_sdk_run_state("approval-run", "approval-test")
        assert approval_row["decision"] == "approved"
        assert approval_row["resolved_at"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_approval_command_replay_continues_a_committed_decision_once(tmp_path):
    from app.models import LifecycleStatus, replace_state
    from app.protocol.events import ToolApprovalResolved

    store = SQLiteCheckpointStore(tmp_path / "state.db")
    config = ConfigRepository(None)
    prompts = PromptRegistry(Path("missing.yml"))
    first = DiagnosisRuntime(
        DiagnosisGraph(_ApprovalRunner(interrupt=True), prompts, tracing_enabled=False),
        store,
        config,
        tracing_enabled=False,
        run_state_cipher=SDKRunStateCipher("approval-secret"),
        sleep=lambda _delay: __import__("asyncio").sleep(0),
    )
    try:
        async for _event in first.run(
            StartDiagnosis(run_id="approval-replay-run", question="why slow")
        ):
            pass
        waiting = await first.get_run("approval-replay-run")
        execution_id = store.list_node_executions(run_id=waiting.run_id)[0][
            "execution_id"
        ]
        command = ApproveTool(
            run_id=waiting.run_id,
            expected_revision=waiting.revision,
            request_id="approval-test",
            command_id="approval-replay-command",
        )
        committed = replace_state(
            waiting,
            lifecycle_status=LifecycleStatus.RUNNING,
            pending_approval=None,
            active_execution_id=execution_id,
            revision=waiting.revision + 1,
        )
        resolved = ToolApprovalResolved(
            run_id=waiting.run_id,
            sequence=1,
            revision=committed.revision,
            request_id=command.request_id,
            decision="approved",
        )
        owner = "approval-replay-test"
        token = store.acquire_lease(waiting.run_id, owner)
        try:
            store.commit(
                committed,
                waiting.revision,
                [resolved],
                command,
                lease_owner=owner,
                fencing_token=token,
                sdk_run_state_decision={
                    "run_id": waiting.run_id,
                    "request_id": command.request_id,
                    "decision": "approved",
                    "decision_reason": "user approved tool execution",
                },
            )
        finally:
            store.release_lease(waiting.run_id, owner, token)

        replay_runner = _ApprovalRunner(interrupt=False)
        replay = DiagnosisRuntime(
            DiagnosisGraph(replay_runner, prompts, tracing_enabled=False),
            store,
            config,
            tracing_enabled=False,
            run_state_cipher=SDKRunStateCipher("approval-secret"),
            sleep=lambda _delay: __import__("asyncio").sleep(0),
        )
        async for _event in replay.run(command):
            pass

        final = await replay.get_run(waiting.run_id)
        resolved_events = [
            event
            for event in store.events_after(waiting.run_id)
            if event.event_type == "tool_approval_resolved"
        ]
        assert final.lifecycle_status == LifecycleStatus.COMPLETED
        assert len(resolved_events) == 1
        assert replay_runner.resume_calls[0]["decision"] == "approved"
    finally:
        store.close()
