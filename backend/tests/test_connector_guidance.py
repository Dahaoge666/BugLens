"""Connector selection, pinned usage and actual SDK inputs, without models."""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from agents import Agent
from buglens_plugin_api import ExecutionContext, ToolResultStatus
from pydantic import ValidationError

from app.admin import AdminApplicationService, PluginInstanceMutation
from app.agents import NativeDiagnosisRunner, NodeRuntimeContext, OpenAINodeRunner
from app.connector_guidance import usage_template
from app.environment import (
    ConnectorUsage,
    ConnectorUsageExample,
    EnvironmentDirectory,
    EnvironmentRepository,
    PluginInstanceConfig,
    ResolvedEnvironmentSnapshot,
    SnapshotPluginInstance,
    SnapshotSource,
    TransportConfig,
)
from app.models import (
    DiagnosisTurnResult,
    InvestigationInput,
    InvestigationResult,
    NativeDiagnosisInput,
    ProblemAnalysis,
    ProblemCategory,
)
from app.plugins import EnvironmentToolService, PluginConfigError, PluginManager
from app.prompts import PromptRegistry
from app.transports import McpTransport


def guide(name="订单知识库"):
    return ConnectorUsage(
        name=name,
        description="只读订单诊断知识",
        when_to_use="请求超时",
        instructions="使用 search_knowledge 按关键词查找，再引用返回证据。password=private-password",
        examples=[
            ConnectorUsageExample(
                operation="search_knowledge",
                request={
                    "query": "连接池",
                    "filters": {"api_key": "private-key"},
                    "source_id": "foreign",
                },
            )
        ],
    )


def snapshot():
    return ResolvedEnvironmentSnapshot(
        snapshot_id="snapshot",
        config_revision="revision",
        environment_id="prod",
        display_name="Production",
        level="production",
        timezone="UTC",
        plugin_instances=[
            SnapshotPluginInstance(
                id="connector-a",
                plugin_id="connector",
                usage=guide(),
                transport=TransportConfig(
                    type="mcp",
                    url="http://private-host/mcp",
                    tool_map={"search_knowledge": "search_docs"},
                ),
            )
        ],
        sources=[
            SnapshotSource(
                id="knowledge-a",
                kind="knowledge",
                plugin_instance_id="connector-a",
                plugin_id="connector",
            )
        ],
    )


def test_guides_bind_examples_and_redact_secrets_without_connection_settings():
    pinned = snapshot()
    projected = pinned.public_view()
    content = json.dumps(projected, ensure_ascii=False)
    assert "private-password" not in content and "private-key" not in content
    assert "private-host" not in content and "search_docs" not in content
    item = projected["connector_guides"][0]
    assert item["available_operations"] == ["search_knowledge"]
    assert item["examples"][0]["arguments"]["source_id"] == "knowledge-a"
    assert "foreign" not in content
    item["examples"][0]["arguments"]["query"] = "changed"
    assert pinned.connector_guides()[0]["examples"][0]["arguments"]["query"] == "连接池"


def test_guides_follow_source_capabilities_mcp_mapping_and_bounds():
    pinned = snapshot()
    pinned.sources[0].capabilities = ["logs.search.v1"]
    assert pinned.connector_guides() == []
    pinned.sources[0].capabilities = []
    pinned.plugin_instances[0].transport.tool_map = {"search_logs": "logs"}
    assert pinned.connector_guides() == []
    pinned.plugin_instances[0].transport.tool_map = {"search_knowledge": "search"}
    pinned.sources = [
        pinned.sources[0].model_copy(update={"id": f"source-{i}"}) for i in range(100)
    ]
    assert len(pinned.connector_guides()) == 24
    pinned.plugin_instances[0].usage.instructions = "只读说明" * 1000
    assert (
        len(json.dumps(pinned.connector_guides(), ensure_ascii=False).encode("utf-8"))
        <= 16_384
    )
    old = pinned.model_dump(mode="json")
    old["plugin_instances"][0].pop("usage")
    assert ResolvedEnvironmentSnapshot.model_validate(old).connector_guides() == []


def test_usage_examples_are_bounded_json_for_registered_read_only_operations():
    for values in (
        {"operation": "reboot", "request": {}},
        {"operation": "search_logs", "request": {"command": "cat /logs"}},
        {"operation": "search_knowledge", "request": {"query": "x" * 3000}},
        {"operation": "search_knowledge", "request": {"top_k": float("nan")}},
    ):
        with pytest.raises(ValidationError):
            ConnectorUsageExample.model_validate(values)


def test_long_guides_remain_useful_and_primary_service_is_prioritized():
    pinned = snapshot()
    pinned.plugin_instances[0].usage = ConnectorUsage(
        name="长说明",
        description="说明" * 1000,
        when_to_use="场景" * 1000,
        instructions="只读步骤" * 1000,
    )
    pinned.primary_service_id = "orders"
    pinned.sources = [
        pinned.sources[0].model_copy(update={"id": f"other-{i}"}) for i in range(30)
    ] + [
        pinned.sources[0].model_copy(update={"id": "orders", "service_ids": ["orders"]})
    ]
    projected = pinned.connector_guides()
    assert projected[0]["source_id"] == "orders"
    assert projected[0]["description"] and projected[0]["instructions"]
    assert projected[0]["truncated"]
    assert len(json.dumps(projected, ensure_ascii=False).encode("utf-8")) <= 16_384


def test_host_guide_examples_use_the_sources_configured_checks():
    pinned = snapshot()
    instance = pinned.plugin_instances[0]
    instance.plugin_id = "ssh"
    instance.transport = TransportConfig()
    instance.usage.examples = [
        ConnectorUsageExample(operation="inspect_host", request={"check": "system"})
    ]
    pinned.sources[0].kind = "host"
    pinned.sources[0].config = {"checks": ["disk", "memory"]}
    projected = pinned.connector_guides()[0]
    assert projected["allowed_checks"] == ["disk", "memory"]
    assert projected["examples"][0]["arguments"]["check"] == "disk"


def test_admin_custom_connector_catalog_and_usage_snapshot_are_independent(tmp_path):
    path = tmp_path / "environments.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "environments": {"prod": {"display_name": "Production"}},
                "services": {"orders": {"name": "Orders", "environment_id": "prod"}},
            }
        ),
        encoding="utf-8",
    )
    repository = EnvironmentRepository(path)
    manager = PluginManager([])
    tools = EnvironmentToolService(repository, manager)
    admin = AdminApplicationService(
        SimpleNamespace(
            environments=repository, runtime=SimpleNamespace(environment_tools=tools)
        )
    )
    assert [item.plugin_id for item in admin.plugins().items] == ["connector"]
    assert admin.plugins().items[0].supported_transports == ["mcp", "cli", "ssh"]
    for suffix in ("a", "b"):
        mutation = PluginInstanceMutation(
            expected_revision=repository.revision,
            instance={
                "id": f"connector-{suffix}",
                "plugin_id": "connector",
                "environment_id": "prod",
                "service_id": "orders",
                "transport": {
                    "type": "mcp",
                    "url": "http://localhost/mcp",
                    "tool_map": {"search_knowledge": "search"},
                },
                "usage": guide(suffix).model_dump(mode="json"),
            },
            sources=[
                {
                    "id": f"knowledge-{suffix}",
                    "kind": "knowledge",
                    "plugin_instance_id": f"connector-{suffix}",
                    "environment_id": "prod",
                    "service_ids": ["orders"],
                }
            ],
        )
        admin.save_plugin_instance(mutation)
    pinned = repository.snapshot("prod")
    mutation.instance["usage"]["instructions"] = "新说明"
    mutation.expected_revision = repository.revision
    admin.save_plugin_instance(mutation, instance_id="connector-b")
    assert pinned.plugin_instance("connector-b").usage.instructions != "新说明"
    assert (
        repository.snapshot("prod").plugin_instance("connector-b").usage.instructions
        == "新说明"
    )
    assert repository.snapshot("prod").plugin_instance("connector-a").usage.name == "a"
    missing = PluginInstanceConfig(
        id="missing",
        plugin_id="connector",
        transport=TransportConfig(type="cli", command=sys.executable),
    )
    with pytest.raises(PluginConfigError, match="usage"):
        manager.validate_instance(missing)
    manager.close()


@pytest.mark.parametrize(
    "plugin_id,kind,operation,tool_request",
    [
        ("sqlite", "database", "query_database", {"sql": "SELECT 1"}),
        ("postgresql", "database", "query_database", {"sql": "SELECT 1"}),
        ("file_logs", "logs", "search_logs", {"text_query": "ERROR"}),
        ("ssh_logs", "logs", "search_logs", {"text_query": "ERROR"}),
        ("ssh", "host", "inspect_host", {"check": "system"}),
    ],
)
@pytest.mark.parametrize("transport_type", ["mcp", "cli"])
@pytest.mark.asyncio
async def test_every_native_plugin_can_execute_via_mcp_or_cli_without_driver_config(
    tmp_path, monkeypatch, plugin_id, kind, operation, tool_request, transport_type
):
    manager = PluginManager.discover()
    code = "import json,sys; p=json.load(sys.stdin); print(json.dumps({'status':'succeeded','result':{'operation':p['operation'],'request':p['request'],'source_config':p['source_config']}}))"
    if transport_type == "cli":
        transport = TransportConfig(
            type="cli", command=sys.executable, args=["-c", code]
        )
    else:
        transport = TransportConfig(
            type="mcp", url="http://test.invalid/mcp", tool_map={operation: "read_tool"}
        )

        async def ensure_server(self):
            self._tool_names = {"read_tool"}

            async def call_tool(name, arguments):
                assert name == "read_tool"
                return {
                    "structuredContent": {
                        "status": "succeeded",
                        "result": {"operation": operation, "request": arguments},
                    },
                }

            return SimpleNamespace(call_tool=call_tool)

        monkeypatch.setattr(McpTransport, "_ensure_server", ensure_server)
    instance = PluginInstanceConfig(
        id="instance", plugin_id=plugin_id, environment_id="prod", transport=transport
    )
    directory = EnvironmentDirectory.model_validate(
        {
            "environments": [{"id": "prod", "display_name": "Production"}],
            "plugin_instances": [instance.model_dump(mode="json")],
            "sources": [
                {
                    "id": "source",
                    "kind": kind,
                    "environment_id": "prod",
                    "plugin_instance_id": "instance",
                    "config": {},
                }
            ],
        }
    )
    manager.validate_directory(directory)
    assert kind in manager.manifest_for_instance(instance).capabilities
    assert usage_template(manager.manifest(plugin_id)).description
    plugin = manager.instance(instance)
    execution = ExecutionContext(
        execution_id="test",
        run_id="run",
        environment_snapshot_id="snapshot",
        plugin_instance_id="instance",
        deadline=datetime.now(UTC) + timedelta(seconds=10),
    )
    try:
        result = await plugin.execute(
            operation, {"selector": "bound"}, tool_request, execution
        )
        assert result.status == ToolResultStatus.SUCCEEDED
        assert result.structured_result["request"] == tool_request
        if transport_type == "cli":
            assert result.structured_result["source_config"] == {"selector": "bound"}
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_mcp_unmapped_operation_is_rejected_without_connecting(monkeypatch):
    transport = McpTransport(
        url="http://test.invalid/mcp", tool_map={"search_logs": "search"}
    )
    ensure = AsyncMock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(transport, "_ensure_server", ensure)
    execution = ExecutionContext(
        execution_id="test",
        run_id="run",
        environment_snapshot_id="snapshot",
        plugin_instance_id="instance",
        deadline=datetime.now(UTC) + timedelta(seconds=1),
    )
    result = await transport.execute("query_database", {}, {}, execution)
    assert result.status == ToolResultStatus.REJECTED
    ensure.assert_not_called()


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.asyncio
async def test_sdk_receives_pinned_guides_in_user_input_not_agent_instructions(
    tmp_path, monkeypatch, native
):
    pinned = snapshot()
    context = NodeRuntimeContext(
        "run", "investigate", "v1", environment_snapshot=pinned
    )
    output = (
        DiagnosisTurnResult.model_construct(kind="needs_input")
        if native
        else InvestigationResult(investigation_summary="信息不足")
    )
    sdk = AsyncMock(
        return_value=SimpleNamespace(
            final_output=output, interruptions=[], new_items=[]
        )
    )
    monkeypatch.setattr("app.agents.Runner.run", sdk)
    runner_cls = NativeDiagnosisRunner if native else OpenAINodeRunner
    runner = runner_cls(PromptRegistry(Path("missing.yml")), tmp_path / "sessions.db")
    payload = (
        NativeDiagnosisInput(question="请求超时")
        if native
        else InvestigationInput(
            analysis=ProblemAnalysis(
                category=ProblemCategory.PERFORMANCE,
                category_confidence=0.9,
                summary="请求超时",
            )
        )
    )
    try:
        if native:
            await runner._run_native_once(
                Agent(name="test", instructions="固定规则"), payload, context
            )
        else:
            await runner.run(
                "investigate", payload, context, ProblemCategory.PERFORMANCE
            )
        sent = json.loads(sdk.call_args.kwargs["input"])
        assert sent["connector_guides"] == pinned.connector_guides()
        assert "订单知识库" not in sdk.call_args.args[0].instructions
        assert payload.connector_guides == []
    finally:
        runner.close()
