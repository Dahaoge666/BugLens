from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import ConfigRepository
from app.context import ContextAssembler
from app.graph import DiagnosisGraph, NativeDiagnosisGraph
from app.infra import SQLiteCheckpointStore
from app.memory import build_memory, rank_memories
from app.models import (
    ClarificationQuestion,
    DiagnosisOutcome,
    DiagnosisReport,
    DiagnosisState,
    DiagnosisTurnResult,
    EvaluationResult,
    Evidence,
    GraphContractError,
    GraphNode,
    InvestigationResult,
    LifecycleStatus,
    MemoryMatch,
    ProblemAnalysis,
    ProblemCategory,
    RootCauseHypothesis,
    UserAnswer,
    UserInteractionRequest,
    replace_state,
)
from app.prompts import BASE_INVESTIGATION_PROMPT, PromptRegistry
from app.protocol.commands import StartDiagnosis, SubmitUserAnswers
from app.protocol.events import RunCompleted, RunStarted
from app.runtime import DiagnosisRuntime
from app.security import sanitize_data


def _analysis():
    return ProblemAnalysis(
        category=ProblemCategory.PERFORMANCE,
        category_confidence=0.9,
        summary="订单接口连接池超时 timeout",
        symptoms=["连接池等待超时", "timeout"],
        missing_information=["缺少连接池使用率"],
    )


def _investigation(evidence_id="old-ev"):
    return InvestigationResult(
        investigation_summary="连接池耗尽可能导致超时",
        primary_conclusion="连接池耗尽",
        hypotheses=[
            RootCauseHypothesis(
                rank=1,
                cause="连接池耗尽",
                rationale="等待连接的请求超时",
                supporting_evidence=[evidence_id],
                confidence=0.8,
                verification_steps=["检查连接池等待时间与使用率"],
            ),
            RootCauseHypothesis(
                rank=2,
                cause="慢查询",
                rationale="慢查询可能占用连接",
                supporting_evidence=[evidence_id],
                confidence=0.4,
                verification_steps=["核对同一时间窗口的慢查询日志"],
            ),
        ],
        evidence_gaps=["缺少慢查询日志"],
        next_data_to_collect=["连接池指标"],
    )


def _evaluation(passed=True):
    return EvaluationResult(
        passed=passed,
        score=85,
        criteria_scores={
            "problem_coverage": 18,
            "evidence_traceability": 20,
            "reasoning_consistency": 17,
            "verification_executability": 18,
            "uncertainty_expression": 12,
        },
        deficiencies=[] if passed else ["证据不足，待验证"],
    )


def _report(evidence_id="old-ev"):
    return DiagnosisReport(
        executive_summary="连接池超时诊断完成",
        primary_conclusion="连接池耗尽",
        status_explanation="已完成独立评测",
        next_actions=["核对连接池等待指标"],
        evidence_ids=[evidence_id],
    )


def _state(run_id="source", tenant="a", environment="prod", service="orders"):
    state = DiagnosisState.create(
        "订单接口连接池超时 timeout",
        {"tenant_id": tenant, "environment": environment, "service": service},
        [
            Evidence(
                evidence_id="old-ev" if run_id == "source" else f"ev-{run_id}",
                content="连接池等待超时 timeout",
            )
        ],
    )
    return replace_state(state, run_id=run_id)


def _completed(state, passed=True):
    evidence_id = (
        state.source_evidence[0].evidence_id if state.source_evidence else "old-ev"
    )
    return replace_state(
        state,
        analysis=_analysis(),
        investigation=_investigation(evidence_id),
        evaluation=_evaluation(passed),
        report=_report(evidence_id),
        lifecycle_status=LifecycleStatus.COMPLETED,
        current_node=GraphNode.DONE,
        outcome=DiagnosisOutcome.CONFIRMED if passed else DiagnosisOutcome.INCONCLUSIVE,
        revision=state.revision + 1,
        updated_at=datetime.now(UTC),
    )


def _seed(store, state=None, passed=True):
    state = state or _state()
    store.create_run(
        state,
        [
            RunStarted(
                run_id=state.run_id,
                sequence=1,
                revision=0,
                profile=state.config_profile,
                config_snapshot_id=state.config_snapshot_id,
                config_version=state.config_version,
            )
        ],
        StartDiagnosis(run_id=state.run_id, question=state.user_question),
    )
    completed = _completed(state, passed)
    store.commit(
        completed,
        state.revision,
        [
            RunCompleted(
                run_id=state.run_id,
                sequence=1,
                revision=completed.revision,
                outcome=completed.outcome,
                summary="diagnosis complete",
            )
        ],
    )
    return completed


def test_memory_preserves_uncertainty_and_reference_provenance():
    state = _completed(_state())
    case = build_memory(state)
    assert case.confirmed_conclusion == "连接池耗尽"
    assert [item.status for item in case.hypotheses] == [
        "evidence_supported",
        "unverified",
    ]
    assert case.hypotheses[0].historical_evidence_ids == ["old-ev"]
    assert case.hypotheses[0].verification_steps
    assert case.information_gaps == ["缺少连接池使用率", "缺少慢查询日志"]
    inconclusive = build_memory(_completed(_state(), passed=False))
    assert inconclusive.confirmed_conclusion is None
    assert all(item.status == "unverified" for item in inconclusive.hypotheses)
    assert "证据不足，待验证" in inconclusive.limitations
    assert build_memory(_state()) is None


def test_missing_or_inconsistent_evidence_never_confirms_memory():
    state = _completed(_state())
    state = replace_state(state, source_evidence=[])
    assert build_memory(state).confirmed_conclusion is None
    state = replace_state(
        state, report=_report().model_copy(update={"primary_conclusion": "其他原因"})
    )
    assert all(item.status == "unverified" for item in build_memory(state).hypotheses)


def test_memory_is_redacted_and_bounded():
    state = _completed(_state())
    state.analysis.summary = "timeout password=secret-value test@example.com"
    state.investigation.hypotheses[0].verification_steps = [
        "token=hidden " + "x" * 2000
    ] * 20
    state.investigation.evidence_gaps = ["x" * 2000] * 50
    case = build_memory(state)
    encoded = case.model_dump_json()
    assert "secret-value" not in encoded and "hidden" not in encoded
    assert "test@example.com" not in encoded
    assert len(case.hypotheses[0].verification_steps) <= 5
    assert len(case.hypotheses[0].verification_steps[0]) <= 500
    assert len(case.information_gaps) <= 10


def test_redaction_at_truncation_boundary_survives_checkpoint_sanitization():
    state = _completed(_state())
    state.analysis.summary = "x" * 1990 + " token=hidden"
    case = build_memory(state)
    matched = replace_state(
        _state("query"), memory_matches=[MemoryMatch(case=case, similarity=0.8)]
    )
    reloaded = DiagnosisState.model_validate(
        sanitize_data(matched.model_dump(mode="json"))
    )
    assert reloaded.memory_matches[0].case.problem_summary == case.problem_summary
    assert "hidden" not in reloaded.model_dump_json()


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "b"),
        ("environment", "test"),
        ("service", "payments"),
        ("service", None),
    ],
)
def test_search_isolates_scope(tmp_path, field, value):
    store = SQLiteCheckpointStore(tmp_path / "memory.db")
    try:
        _seed(store)
        context = {"tenant_id": "a", "environment": "prod", "service": "orders"}
        context[field] = value
        query = DiagnosisState.create("订单接口连接池超时 timeout", context, [])
        assert store.search_memories(query) == []
    finally:
        store.close()


def test_retrieval_filters_category_unrelated_cases_and_self(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "memory.db")
    try:
        source = _seed(store)
        query = replace_state(_state("query"), analysis=_analysis())
        assert store.search_memories(query)[0].case.source_run_id == "source"
        assert store.search_memories(source) == []
        query.analysis.category = ProblemCategory.SECURITY_ACCESS
        assert store.search_memories(query) == []
        unrelated = replace_state(
            _state("other"),
            source_evidence=[],
            user_question="证书过期 SSL certificate expired",
        )
        assert store.search_memories(unrelated) == []
        for index in range(5):
            _seed(store, _state(f"source-{index}"))
        assert len(store.search_memories(_state("query"))) == 3
        store.db.execute(
            "UPDATE diagnosis_memories SET case_json = 'broken' WHERE source_run_id = 'source'"
        )
        store.db.commit()
        assert store.get_memory("source") is None
        assert len(store.search_memories(_state("query"))) == 3
    finally:
        store.close()


def test_archive_rolls_back_with_terminal_commit_and_survives_restart(
    tmp_path, monkeypatch
):
    path = tmp_path / "memory.db"
    store = SQLiteCheckpointStore(path)
    state = _state()
    store.create_run(
        state, [], StartDiagnosis(run_id=state.run_id, question=state.user_question)
    )
    original = store._save_memory_locked

    def fail_after_archive(state):
        original(state)
        raise RuntimeError("interrupted transaction")

    monkeypatch.setattr(store, "_save_memory_locked", fail_after_archive)
    with pytest.raises(RuntimeError):
        store.commit(_completed(state), 0, [])
    assert store.get_memory(state.run_id) is None
    assert store.get_state(state.run_id).revision == 0
    assert store.list_guides(category=None, limit=100, offset=0).total == 0
    monkeypatch.setattr(store, "_save_memory_locked", original)
    store.commit(_completed(state), 0, [])
    # Reopening also exercises the repeatable migration.
    store.close()
    store = SQLiteCheckpointStore(path)
    try:
        assert store.get_memory("source").source_revision == 1
        guides = store.list_guides(category=None, limit=100, offset=0)
        assert guides.total == 1
        assert guides.items[0].source_run_id == "source"
        assert (
            store.db.execute("SELECT count(*) FROM diagnosis_memories").fetchone()[0]
            == 1
        )
    finally:
        store.close()


def test_schema_upgrade_backfills_completed_reports_once(tmp_path):
    path = tmp_path / "memory.db"
    store = SQLiteCheckpointStore(path)
    _seed(store)
    store.db.execute("DELETE FROM diagnosis_memories")
    store.db.execute("UPDATE buglens_schema SET version = 3")
    store.db.commit()
    store.close()
    store = SQLiteCheckpointStore(path)
    try:
        assert store.get_memory("source").confirmed_conclusion == "连接池耗尽"
        assert (
            store.db.execute("SELECT version FROM buglens_schema").fetchone()[0]
            == SQLiteCheckpointStore.SCHEMA_VERSION
        )
    finally:
        store.close()


class FakeRunner:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    async def run(self, node_name, payload, runtime, category=None):
        self.calls.append(payload)
        return next(self.outputs)


def _runtime(store, outputs, native=True):
    runner = FakeRunner(outputs)
    graph_type = NativeDiagnosisGraph if native else DiagnosisGraph
    graph = graph_type(
        runner, PromptRegistry(Path("missing.yml")), tracing_enabled=False
    )
    return DiagnosisRuntime(
        graph, store, ConfigRepository(None), tracing_enabled=False
    ), runner


def _turn():
    return DiagnosisTurnResult(
        kind="completed",
        analysis=_analysis(),
        investigation=_investigation("current-ev"),
        evaluation=_evaluation(),
        report=_report("current-ev"),
        review_count=1,
    )


def _command(run_id="current"):
    return StartDiagnosis(
        run_id=run_id,
        question="订单接口连接池超时 timeout",
        context={"tenant_id": "a", "environment": "prod", "service": "orders"},
        evidence=[Evidence(evidence_id="current-ev", content="连接池等待超时 timeout")],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_runtime_injects_guidance_and_command_replay_does_not_duplicate(
    tmp_path, native
):
    store = SQLiteCheckpointStore(tmp_path / "memory.db")
    try:
        _seed(store)
        outputs = (
            [_turn()]
            if native
            else [
                _analysis(),
                _investigation("current-ev"),
                _evaluation(),
                _report("current-ev"),
            ]
        )
        runtime, runner = _runtime(store, outputs, native)
        command = _command()
        first = [event async for event in runtime.run(command)]
        replay = [event async for event in runtime.run(command)]
        assert first[-1].event_id == replay[-1].event_id
        state = await runtime.get_run("current")
        assert state.memory_case.source_run_id == "current"
        assert state.memory_matches[0].case.source_run_id == "source"
        guides = store.list_guides(category=None, limit=100, offset=0)
        assert guides.total == 2
        assert {guide.source_run_id for guide in guides.items} == {"source", "current"}
        assert all(guide.origin == "memory" for guide in guides.items)
        payload = runner.calls[0 if native else 1]
        assert payload.memory_matches[0].case.source_run_id == "source"
        assert "old-ev" not in {item.evidence_id for item in payload.evidence}
        assert (
            store.db.execute("SELECT count(*) FROM diagnosis_memories").fetchone()[0]
            == 2
        )
        audit = store.list_node_executions(run_id="current")
        assert any('"memory_matches"' in row["input_context_json"] for row in audit)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_clarification_uses_pinned_memory_after_process_restart(tmp_path):
    path = tmp_path / "memory.db"
    store = SQLiteCheckpointStore(path)
    _seed(store)
    request = UserInteractionRequest(
        request_id="req",
        source_node="investigate",
        resume_node="investigate",
        reason="insufficient_evidence",
        explanation="需要连接池指标",
        questions=[
            ClarificationQuestion(
                id="q1", question="连接池使用率？", rationale="验证饱和"
            )
        ],
    )
    runtime, runner = _runtime(
        store,
        [
            DiagnosisTurnResult(
                kind="needs_input",
                analysis=_analysis(),
                interaction_request=request,
                active_agent="investigator:performance",
            )
        ],
    )
    [event async for event in runtime.run(_command())]
    selected = runner.calls[0].memory_matches
    waiting = store.get_state("current")
    _seed(store, _state("new-case"))
    store.close()
    store = SQLiteCheckpointStore(path)
    try:
        runtime, runner = _runtime(store, [_turn()])
        answer = SubmitUserAnswers(
            run_id="current",
            expected_revision=waiting.revision,
            request_id="req",
            answers=[UserAnswer(request_id="req", question_id="q1", answer="100%")],
        )
        [event async for event in runtime.run(answer)]
        assert runner.calls[0].memory_matches == selected
        assert (await runtime.get_run("current")).memory_selection_completed
    finally:
        store.close()


def test_historical_ids_are_rejected_as_current_report_evidence():
    state = replace_state(
        _state("current"),
        source_evidence=[Evidence(evidence_id="current-ev", content="本次 timeout")],
        analysis=_analysis(),
    )
    matches = build_memory(_completed(_state()))
    state = replace_state(
        state, memory_matches=[MemoryMatch(case=matches, similarity=0.9)]
    )
    assert {item.evidence_id for item in ContextAssembler().evidence(state)} == {
        "current-ev"
    }
    with pytest.raises(GraphContractError, match="registered evidence"):
        DiagnosisGraph._validate_report(_report("old-ev"), state)
    assert "不能引用为本次证据" in BASE_INVESTIGATION_PROMPT


def test_native_analysis_cannot_register_a_historical_id_as_new_evidence():
    source = build_memory(_completed(_state()))
    state = replace_state(
        _state("current"),
        lifecycle_status=LifecycleStatus.RUNNING,
        memory_matches=[MemoryMatch(case=source, similarity=0.9)],
    )
    graph = NativeDiagnosisGraph(FakeRunner([]), PromptRegistry(Path("missing.yml")))
    plan = graph.prepare_step(state, ConfigRepository(None).resolve("default"))
    output = _turn()
    output.analysis.extracted_evidence = [
        Evidence(evidence_id="old-ev", content="历史 timeout")
    ]
    with pytest.raises(GraphContractError, match="Historical evidence"):
        graph.apply_result(state, plan, output)


def test_oversized_memory_cannot_exceed_model_input_budget():
    case = build_memory(_completed(_state()))
    oversized = case.model_copy(
        update={
            "source_run_id": "oversized",
            "information_gaps": ["数据" * 250] * 10,
            "next_data_to_collect": ["数据" * 250] * 10,
            "limitations": ["数据" * 250] * 10,
        }
    )
    matches = rank_memories(_state("query"), [oversized, case])
    assert [item.case.source_run_id for item in matches] == ["source"]
    assert (
        sum(len(item.model_dump_json().encode("utf-8")) + 1 for item in matches) + 2
        <= 32768
    )
