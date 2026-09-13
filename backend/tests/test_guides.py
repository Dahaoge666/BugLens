import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.application import ApplicationService, Identity
from app.cli import ConsoleClarifier, build_parser, run_cli
from app.client import LocalAgentClient, RemoteAgentClient
from app.config import ConfigRepository
from app.graph import DiagnosisGraph
from app.guides import (
    GuideApplicationService,
    GuideContent,
    GuideImportRequest,
    GuideQuery,
    GuideRevisionConflictError,
    GuideUpdateRequest,
    guide_from_memory,
)
from app.infra import SQLiteCheckpointStore
from app.models import (
    DiagnosisMemory,
    DiagnosisOutcome,
    DiagnosisReport,
    DiagnosisState,
    GraphNode,
    LifecycleStatus,
    MemoryHypothesis,
    ProblemCategory,
    replace_state,
)
from app.prompts import PromptRegistry
from app.runtime import DiagnosisRuntime, RunView
from app.web import BugLensASGI


def _content(**updates):
    return GuideContent.model_validate(
        {
            "title": "订单接口连接池超时 timeout",
            "category": "performance",
            "phenomenon": "订单接口连接池超时 timeout",
            "symptoms": ["连接池等待超时", "timeout"],
            "steps": [
                {
                    "instruction": "检查连接池使用率与等待时间",
                    "expected_observation": "是否同时升高",
                }
            ],
            **updates,
        }
    )


def _case(**updates):
    return DiagnosisMemory.model_validate(
        {
            "memory_id": "mem_source",
            "source_run_id": "source",
            "source_revision": 5,
            "source_config_version": "v1",
            "recorded_at": datetime.now(UTC),
            "category": "performance",
            "outcome": "confirmed",
            "problem_summary": "订单接口连接池超时 timeout",
            "symptoms": ["连接池等待超时", "timeout"],
            "environment": "prod",
            "service": "orders",
            "confirmed_conclusion": "连接池耗尽",
            "hypotheses": [
                MemoryHypothesis(
                    cause="连接池耗尽",
                    status="evidence_supported",
                    rationale="等待连接超时",
                    historical_evidence_ids=["old-ev"],
                    verification_steps=["检查连接池使用率与等待时间"],
                )
            ],
            **updates,
        }
    )


class NoRunner:
    async def run(self, *args, **kwargs):
        raise AssertionError("guide lookup must not invoke a model")


@pytest.fixture
def service(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "guides.db")
    configs = ConfigRepository(None)
    graph = DiagnosisGraph(
        NoRunner(), PromptRegistry(Path("missing.yml")), tracing_enabled=False
    )
    runtime = DiagnosisRuntime(graph, store, configs, tracing_enabled=False)
    service = ApplicationService(runtime, configs, require_model_credentials=True)
    yield service
    store.close()


def _import(service, content=None, tenant=None):
    return service.guides.import_manual(
        GuideImportRequest(tenant_id=tenant, guides=[content or _content()])
    ).items[0]


def _query(**updates):
    return GuideQuery.model_validate(
        {
            "question": "订单接口连接池超时 timeout",
            "context": {"environment": "prod", "service": "orders"},
            **updates,
        }
    )


def test_auto_guides_require_specific_supported_memory_and_keep_classification():
    guide = guide_from_memory(_case())
    assert guide.origin == "memory" and guide.category == ProblemCategory.PERFORMANCE
    assert guide.source_run_id == "source" and guide.source_memory_id == "mem_source"
    assert guide.historical_conclusion == "连接池耗尽"
    assert guide.steps[0].instruction == "检查连接池使用率与等待时间"
    for case in [
        _case(symptoms=[]),
        _case(category="unknown"),
        _case(confirmed_conclusion=None),
        _case(outcome="inconclusive"),
        _case(hypotheses=[]),
    ]:
        assert guide_from_memory(case) is None


@pytest.mark.asyncio
async def test_lookup_returns_guides_without_run_session_or_model_credentials(service):
    guide = _import(service)
    result = await service.find_guides(_query())
    assert result.next_action == "confirm_similarity"
    assert result.matches[0].guide.guide_id == guide.guide_id
    store = service.runtime.store
    assert store.db.execute("SELECT count(*) FROM diagnosis_runs").fetchone()[0] == 0
    assert (
        store.db.execute("SELECT count(*) FROM diagnosis_node_executions").fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_no_similar_problem_returns_diagnose_and_no_premature_run(service):
    _import(service)
    result = await service.find_guides(
        _query(question="证书过期 TLS certificate expired")
    )
    assert result.next_action == "diagnose" and not result.matches
    assert (
        service.runtime.store.db.execute(
            "SELECT count(*) FROM diagnosis_runs"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_tenant_isolation_and_authenticated_identity_overrides_supplied_tenant(
    service,
):
    _import(service, tenant="a")
    spoofed = _query(context={"tenant_id": "a"})
    assert (await service.find_guides(spoofed)).matches
    assert not (await service.find_guides(spoofed, Identity(tenant_id="b"))).matches
    assert not (await service.find_guides(spoofed, Identity())).matches
    assert (await service.find_guides(_query(), Identity(tenant_id="a"))).matches


@pytest.mark.asyncio
async def test_scope_and_known_version_filtering_allow_explicitly_generic_manual_guides(
    service,
):
    scoped = _import(
        service, _content(environment="prod", service="orders", version="v1")
    )
    assert not (
        await service.find_guides(
            _query(context={"environment": "test", "service": "orders"})
        )
    ).matches
    assert not (
        await service.find_guides(
            _query(
                context={"environment": "prod", "service": "orders", "version": "v2"}
            )
        )
    ).matches
    generic = _import(service)
    assert (
        await service.find_guides(
            _query(context={"environment": "test", "service": "billing"})
        )
    ).matches[0].guide.guide_id == generic.guide_id
    assert scoped.guide_id != generic.guide_id


@pytest.mark.asyncio
async def test_auto_guides_never_treat_missing_environment_as_a_wildcard(service):
    store = service.runtime.store
    store.import_guides([guide_from_memory(_case())], "")
    assert (await service.find_guides(_query())).matches
    assert not (await service.find_guides(_query(context={}))).matches
    assert not (await service.find_guides(_query(category="security_access"))).matches


def test_import_is_redacted_and_batched_in_one_transaction(service, monkeypatch):
    imported = _import(
        service, _content(phenomenon="timeout password=hidden test@example.com")
    )
    assert "hidden" not in imported.model_dump_json()
    assert "test@example.com" not in imported.model_dump_json()
    store = service.runtime.store
    original = store._insert_guide_locked
    calls = 0

    def fail_on_second(guide, tenant):
        nonlocal calls
        calls += 1
        original(guide, tenant)
        if calls == 2:
            raise RuntimeError("interrupted import")

    monkeypatch.setattr(store, "_insert_guide_locked", fail_on_second)
    with pytest.raises(RuntimeError):
        service.guides.import_manual(
            GuideImportRequest(guides=[_content(), _content()])
        )
    assert service.guides.list().total == 1


@pytest.mark.asyncio
async def test_updates_use_revision_and_disabled_guides_are_not_matches(service):
    guide = _import(service)
    disabled = _content(enabled=False)
    updated = service.guides.update(
        guide.guide_id, GuideUpdateRequest(expected_revision=1, guide=disabled)
    )
    assert updated.revision == 2
    assert not (await service.find_guides(_query())).matches
    with pytest.raises(GuideRevisionConflictError):
        service.guides.update(
            guide.guide_id, GuideUpdateRequest(expected_revision=1, guide=_content())
        )
    assert service.guides.list(category="performance").items[0].enabled is False
    assert service.guides.list(category="security_access").total == 0


def test_schema_v5_backfills_memory_guides_and_preserves_manual_edits_on_restart(
    tmp_path,
):
    path = tmp_path / "guides.db"
    store = SQLiteCheckpointStore(path)
    case = _case()
    store.db.execute(
        "INSERT INTO diagnosis_memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            case.memory_id,
            case.source_run_id,
            5,
            "",
            "prod",
            "orders",
            "performance",
            case.model_dump_json(),
            case.recorded_at.isoformat(),
        ),
    )
    store.db.execute("UPDATE buglens_schema SET version = 4")
    store.db.commit()
    store.close()
    store = SQLiteCheckpointStore(path)
    app = GuideApplicationService(store)
    generated = app.list().items[0]
    assert generated.guide_id == "guide_source"
    app.update(
        generated.guide_id,
        GuideUpdateRequest(expected_revision=1, guide=_content(enabled=False)),
    )
    store.close()
    store = SQLiteCheckpointStore(path)
    try:
        assert GuideApplicationService(store).list().total == 1
        assert GuideApplicationService(store).list().items[0].enabled is False
    finally:
        store.close()


async def _http(app, path, method="GET", payload=None, token=None):
    sent = []

    async def receive():
        return {
            "type": "http.request",
            "body": json.dumps(payload).encode() if payload is not None else b"",
            "more_body": False,
        }

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {token}".encode())]
            if token
            else [],
        },
        receive,
        send,
    )
    return sent[0]["status"], json.loads(sent[1]["body"])


@pytest.mark.asyncio
async def test_http_guide_import_auth_validation_lookup_and_revision_conflicts(service):
    app = BugLensASGI(service, admin_token="test-admin")
    payload = {"guides": [_content().model_dump(mode="json")]}
    assert (await _http(app, "/v1/admin/guides/import", "POST", payload))[0] == 401
    status, imported = await _http(
        app, "/v1/admin/guides/import", "POST", payload, "test-admin"
    )
    assert status == 201
    guide = imported["items"][0]
    status, result = await _http(
        app, "/v1/guides/search", "POST", _query().model_dump(mode="json")
    )
    assert status == 200 and result["next_action"] == "confirm_similarity"
    assert (await _http(app, "/v1/guides/categories"))[1]["items"][0][
        "id"
    ] == "application_error"
    invalid = {"guides": [payload["guides"][0], {**payload["guides"][0], "steps": []}]}
    assert (await _http(app, "/v1/admin/guides/import", "POST", invalid, "test-admin"))[
        0
    ] == 422
    assert service.guides.list().total == 1
    update = {
        "expected_revision": 1,
        "guide": _content(enabled=False).model_dump(mode="json"),
    }
    assert (
        await _http(
            app, f"/v1/admin/guides/{guide['guide_id']}", "PUT", update, "test-admin"
        )
    )[0] == 200
    assert (
        await _http(
            app, f"/v1/admin/guides/{guide['guide_id']}", "PUT", update, "test-admin"
        )
    )[0] == 409
    assert (await _http(app, "/v1/admin/guides/missing", "PUT", update, "test-admin"))[
        0
    ] == 404


class FakeCLIClient:
    def __init__(self, lookup):
        self.lookup = lookup
        self.lookup_calls = 0
        self.sent = []

    async def find_guides(self, query):
        self.lookup_calls += 1
        return self.lookup

    async def send(self, command):
        self.sent.append(command)
        if False:
            yield None

    async def get_run(self, run_id):
        state = DiagnosisState.create("问题", {}, [])
        state = replace_state(
            state,
            run_id=run_id,
            current_node=GraphNode.DONE,
            lifecycle_status=LifecycleStatus.COMPLETED,
            outcome=DiagnosisOutcome.CONFIRMED,
            report=DiagnosisReport(
                executive_summary="诊断完成", status_explanation="完成"
            ),
        )
        return RunView.model_validate(state.model_dump())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,expected_sends",
    [("preview", 0), ("select", 0), ("reject", 1), ("find-only", 0), ("unmatched", 1)],
)
async def test_cli_entry_only_starts_diagnosis_for_no_match_or_explicit_rejection(
    service, monkeypatch, capsys, mode, expected_sends
):
    guide = _import(service)
    found = await service.find_guides(
        _query(question="unrelated certificate expired")
        if mode == "unmatched"
        else _query()
    )
    client = FakeCLIClient(found)

    async def make_client(settings):
        return client, None, None

    monkeypatch.setattr("app.cli._client", make_client)
    flags = {
        "select": ["--guide", guide.guide_id],
        "reject": ["--diagnose"],
        "find-only": ["--find-guides"],
    }.get(mode, [])
    args = build_parser().parse_args(["订单接口连接池超时 timeout", "--json", *flags])
    code = await run_cli(
        args, ConsoleClarifier(stream=SimpleNamespace(isatty=lambda: False))
    )
    assert len(client.sent) == expected_sends
    assert code == (2 if mode == "preview" else 0)
    assert json.loads(capsys.readouterr().out)


@pytest.mark.asyncio
async def test_local_client_uses_the_same_guide_lookup_and_import(service):
    client = LocalAgentClient(service)
    imported = await client.import_guides(GuideImportRequest(guides=[_content()]))
    assert (await client.find_guides(_query())).matches[
        0
    ].guide.guide_id == imported.items[0].guide_id


@pytest.mark.asyncio
async def test_remote_client_uses_json_endpoints_and_admin_token(service, monkeypatch):
    found = await service.find_guides(_query())
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return (
                found.model_dump_json().encode()
                if seen[-1].full_url.endswith("/search")
                else b'{"items":[],"total":0}'
            )

    def urlopen(request):
        seen.append(request)
        return Response()

    monkeypatch.setattr("app.client.urlopen", urlopen)
    client = RemoteAgentClient("http://buglens.test", "test-admin")
    assert await client.find_guides(_query()) == found
    await client.import_guides(GuideImportRequest(guides=[_content()]))
    assert seen[0].full_url == "http://buglens.test/v1/guides/search"
    assert seen[1].get_header("Authorization") == "Bearer test-admin"
