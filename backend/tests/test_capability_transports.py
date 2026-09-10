import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from buglens_plugin_api import ExecutionContext, PluginManifest, ToolResultStatus

from app.capabilities import CapabilityRegistry, CapabilitySpec
from app.environment import (
    EnvironmentDirectory,
    EnvironmentRepository,
    PluginInstanceConfig,
    TransportConfig,
)
from app.plugins import PluginManager
from app.transports import McpTransport, SshTransport, SshTransportConfig


def _context(**overrides):
    values = {
        "execution_id": "exec-transport",
        "run_id": "run-transport",
        "environment_snapshot_id": "envsnap-transport",
        "plugin_instance_id": "instance-transport",
        "deadline": datetime.now(UTC) + timedelta(seconds=10),
    }
    values.update(overrides)
    return ExecutionContext(**values)


def test_capability_registry_has_new_read_only_families():
    registry = CapabilityRegistry.default()
    assert registry.for_operation("search_knowledge").id == "knowledge.search.v1"
    assert registry.for_operation("search_traffic").id == "traffic.search.v1"
    assert registry.for_source("knowledge")[0].tool_name == "search_knowledge"
    with pytest.raises(ValueError, match="duplicate capability id"):
        registry.register(
            CapabilitySpec(
                id="knowledge.search.v1",
                tool_name="other",
                operation="other",
            )
        )


def test_environment_directory_accepts_extensible_source_kinds_and_pins_them(
    tmp_path: Path,
):
    path = tmp_path / "environments.yaml"
    path.write_text(
        """
environments:
  prod: {display_name: Production}
plugin_instances:
  knowledge:
    plugin_id: external-knowledge
    transport:
      type: cli
      command: python
      args: [-c, '']
sources:
  handbook:
    kind: knowledge
    capabilities: [knowledge.search.v1]
    plugin_instance_id: knowledge
    environment_id: prod
""",
        encoding="utf-8",
    )
    repository = EnvironmentRepository(path)
    snapshot = repository.snapshot("prod")
    source = snapshot.source("handbook")
    assert source is not None
    assert source.kind == "knowledge"
    assert source.capabilities == ["knowledge.search.v1"]
    assert snapshot.plugin_instance("knowledge").transport.type == "cli"


def test_legacy_driver_family_can_declare_versioned_source_capabilities():
    plugin_manifest = PluginManifest(
        plugin_id="legacy-db", implementation_version="1", capabilities=["database"]
    )

    class LegacyDatabase:
        manifest = plugin_manifest

    directory = EnvironmentDirectory.model_validate(
        {
            "environments": {"prod": {"display_name": "Production"}},
            "plugin_instances": {"db": {"plugin_id": "legacy-db"}},
            "sources": {
                "orders": {
                    "kind": "database",
                    "capabilities": ["database.query.v1"],
                    "plugin_instance_id": "db",
                    "environment_id": "prod",
                }
            },
        }
    )
    PluginManager([LegacyDatabase()]).validate_directory(directory)


def test_declarative_cli_instance_does_not_require_python_plugin():
    instance = PluginInstanceConfig(
        id="knowledge",
        plugin_id="external-knowledge",
        transport=TransportConfig(type="cli", command=sys.executable, args=["-c", ""]),
    )
    manager = PluginManager()
    manager.validate_instance(instance)
    assert manager.instance(instance).kind == "cli"
    manager.close()


def test_ssh_transport_builds_fixed_argv_without_model_command():
    transport = SshTransport(
        SshTransportConfig(
            host="node-1.internal",
            user="probe",
            port=2222,
            command="/opt/buglens/bin/probe",
            known_hosts="/etc/buglens/known_hosts",
            identity_file="/etc/buglens/probe_ed25519",
        )
    )
    assert transport._argv() == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-p",
        "2222",
        "-o",
        "UserKnownHostsFile=/etc/buglens/known_hosts",
        "-i",
        "/etc/buglens/probe_ed25519",
        "probe@node-1.internal",
        "/opt/buglens/bin/probe",
    ]


def test_cli_transport_uses_json_wire_protocol_and_bounds_result(tmp_path: Path):
    script = tmp_path / "connector.py"
    script.write_text(
        "import json,sys; request=json.load(sys.stdin); "
        "assert request['protocol']=='buglens-tool/v1'; "
        "print(json.dumps({'status':'succeeded','result':{'operation':request['operation']}}))",
        encoding="utf-8",
    )
    instance = PluginInstanceConfig(
        id="knowledge",
        plugin_id="external-knowledge",
        transport=TransportConfig(
            type="cli", command=sys.executable, args=[str(script)]
        ),
    )
    manager = PluginManager()
    result = asyncio.run(
        manager.execute(
            instance,
            "search_knowledge",
            {},
            {"query": "timeout"},
            _context(),
        )
    )
    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.structured_result == {"operation": "search_knowledge"}
    manager.close()


def test_mcp_transport_normalizes_structured_content():
    class FakeServer:
        async def call_tool(self, tool_name, arguments):
            assert tool_name == "search_knowledge"
            assert arguments == {"query": "timeout"}
            return type(
                "Result",
                (),
                {"isError": False, "structuredContent": {"items": [1]}},
            )()

    transport = McpTransport(url="https://mcp.example.test/mcp")
    transport._server = FakeServer()
    transport._tool_names = {"search_knowledge"}
    result = asyncio.run(
        transport.execute("search_knowledge", {}, {"query": "timeout"}, _context())
    )
    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.structured_result == {"items": [1]}
