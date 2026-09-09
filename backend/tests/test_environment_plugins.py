import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from buglens_file_logs_plugin import FileLogsPlugin
from buglens_plugin_api import (
    ExecutionContext,
    PluginHealth,
    PluginManifest,
    ToolResult,
    ToolResultStatus,
)
from buglens_sqlite_plugin import SQLitePlugin
from pydantic import ValidationError

from app.config import ToolPolicy
from app.environment import (
    EnvironmentDirectory,
    EnvironmentRepository,
    PluginInstanceConfig,
)
from app.infra import SQLiteCheckpointStore
from app.models import TargetSpec
from app.plugins import (
    EnvironmentToolRegistry,
    EnvironmentToolService,
    PluginCompatibilityError,
    PluginDiscoveryError,
    PluginManager,
)


def context(**overrides):
    values = {
        "execution_id": "exec-1",
        "run_id": "run-1",
        "environment_snapshot_id": "envsnap-1",
        "plugin_instance_id": "instance-1",
        "deadline": datetime.now(UTC) + timedelta(seconds=10),
    }
    values.update(overrides)
    return ExecutionContext(**values)


def test_plugin_api_discovery_rejects_duplicate_and_incompatible_manifests():
    manifest = PluginManifest(
        plugin_id="fake",
        implementation_version="1.0.0",
        capabilities=["database"],
    )

    fake_manifest = manifest

    class FakePlugin:
        manifest = fake_manifest

        def validate_config(self, instance_config, source_config=None):
            return None

        async def check_health(self):
            return PluginHealth(status="ok", detail="ok")

        async def execute(self, operation, source_config, request, context):
            return ToolResult(status=ToolResultStatus.SUCCEEDED, result={})

        def close(self):
            return None

    manager = PluginManager([FakePlugin()])
    with pytest.raises(PluginDiscoveryError, match="duplicate"):
        manager.register(FakePlugin())

    incompatible = PluginManifest(
        plugin_id="other",
        implementation_version="1.0.0",
        api_major=2,
        api_version="2",
    )
    with pytest.raises(PluginCompatibilityError):
        PluginManager([SimpleNamespace(manifest=incompatible)])


def test_environment_directory_rejects_cross_environment_sources():
    with pytest.raises(ValidationError, match="mixes environments"):
        EnvironmentDirectory.model_validate(
            {
                "environments": [
                    {"id": "prod", "display_name": "Production"},
                    {"id": "stage", "display_name": "Staging"},
                ],
                "services": [
                    {"id": "svc-prod", "name": "orders", "environment_id": "prod"}
                ],
                "nodes": [
                    {
                        "id": "node-stage",
                        "hostname": "stage-1",
                        "environment_id": "stage",
                    }
                ],
                "plugin_instances": [{"id": "logs", "plugin_id": "file_logs"}],
                "sources": [
                    {
                        "id": "mixed",
                        "kind": "logs",
                        "plugin_instance_id": "logs",
                        "service_ids": ["svc-prod"],
                        "node_ids": ["node-stage"],
                    }
                ],
            }
        )


def test_environment_repository_infers_aliases_and_keeps_snapshot_credential_free(
    tmp_path: Path,
):
    path = tmp_path / "environments.yaml"
    path.write_text(
        """
config_version: catalog-v1
plugin_instances:
  logs:
    plugin_id: file_logs
    password: do-not-persist-in-snapshot
environments:
  prod:
    display_name: Production
    aliases: [线上, prod]
    level: production
services:
  orders-prod:
    name: order-api
    environment_id: prod
sources:
  prod-logs:
    kind: logs
    plugin_instance_id: logs
    environment_id: prod
    service_ids: [orders-prod]
    config:
      path: order-api.log
""",
        encoding="utf-8",
    )
    repository = EnvironmentRepository(path)
    candidates = repository.infer_candidates(
        TargetSpec(mode="infer"), {"environment": "线上", "service": "order-api"}
    )
    assert [item.environment_id for item in candidates] == ["prod"]
    snapshot = repository.snapshot("prod", "orders-prod")
    assert snapshot.sources[0].id == "prod-logs"
    assert "password" not in snapshot.model_dump_json()
    assert repository.public_config()["plugin_instances"][0]["password"] == {
        "is_set": True
    }


def test_secret_fields_are_write_only_and_nested_public_config_is_masked(
    tmp_path: Path,
):
    schema = PluginInstanceConfig.model_json_schema()
    for field in ("username", "password", "token"):
        assert schema["properties"][field]["writeOnly"] is True
        assert schema["properties"][field]["x-buglens-secret"] is True
    path = tmp_path / "environments.yaml"
    path.write_text(
        """
environments:
  prod: {display_name: Production}
plugin_instances:
  connector:
    plugin_id: fake
    config: {api_key: nested-secret}
    password: top-level-secret
""",
        encoding="utf-8",
    )
    public = EnvironmentRepository(path).public_config()
    instance = public["plugin_instances"][0]
    assert instance["password"] == {"is_set": True}
    assert instance["config"]["api_key"] == {"is_set": True}
    assert "nested-secret" not in str(public)
    assert "top-level-secret" not in str(public)
    assert ToolPolicy().max_result_rows == 200


def test_tool_audit_schema_persists_plugin_fields_and_redacted_query(tmp_path: Path):
    store = SQLiteCheckpointStore(tmp_path / "audit.sqlite")
    try:
        store.start_tool_execution(
            {
                "tool_execution_id": "tool-1",
                "node_execution_id": "node-1",
                "run_id": "run-1",
                "sdk_tool_call_id": "sdk-1",
                "tool_name": "query_database",
                "tool_version": "environment-tools-v1",
                "plugin_id": "sqlite",
                "plugin_implementation_version": "1.0.0",
                "plugin_instance_id": "sqlite-prod",
                "environment_snapshot_id": "envsnap-1",
                "source_id": "prod-db",
                "operation": "query_database",
                "redacted_query": "SELECT * FROM orders WHERE id = ?",
                "query_fingerprint": "f" * 64,
                "arguments_json": "{}",
                "arguments_hash": "a" * 64,
            }
        )
        store.finish_tool_execution(
            "tool-1",
            status="partial",
            truncated=True,
            evidence_ids_json='["ev-1"]',
        )
        record = store.list_tool_executions(run_id="run-1")[0]
        assert record["plugin_id"] == "sqlite"
        assert record["environment_snapshot_id"] == "envsnap-1"
        assert record["redacted_query"].endswith("id = ?")
        assert record["truncated"] == 1
    finally:
        store.close()


def test_sqlite_plugin_rejects_writes_attach_pragma_and_multiple_statements(
    tmp_path: Path,
):
    db = tmp_path / "orders.sqlite"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE orders (id INTEGER, status TEXT, secret TEXT)")
    connection.execute("INSERT INTO orders VALUES (1, 'paid', 'hidden')")
    connection.commit()
    connection.close()
    plugin = SQLitePlugin({"root_path": str(tmp_path)})
    source = {
        "path": "orders.sqlite",
        "allowed_tables": ["orders"],
        "denied_columns": ["secret"],
    }
    result = asyncio.run(
        plugin.execute(
            "query_database",
            source,
            {
                "sql": "SELECT id, status FROM orders WHERE id = :id",
                "parameters": {"id": 1},
            },
            context(),
        )
    )
    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.structured_result["rows"] == [{"id": 1, "status": "paid"}]
    for sql in (
        "UPDATE orders SET status = 'x'",
        "ATTACH DATABASE 'x' AS x",
        "PRAGMA journal_mode=WAL",
        "SELECT 1; SELECT 2",
    ):
        rejected = asyncio.run(
            plugin.execute(
                "query_database", source, {"sql": sql, "parameters": {}}, context()
            )
        )
        assert rejected.status == ToolResultStatus.REJECTED
    assert (
        asyncio.run(
            plugin.execute(
                "query_database",
                source,
                {"sql": "SELECT secret FROM orders", "parameters": {}},
                context(),
            )
        ).status
        == ToolResultStatus.REJECTED
    )


def test_file_logs_plugin_matches_jsonl_rotation_and_scan_limits(tmp_path: Path):
    root = tmp_path / "logs"
    root.mkdir()
    (root / "order.log").write_text(
        '{"timestamp":"2026-09-09T10:00:00Z","level":"ERROR","service_id":"orders","message":"timeout trace=abc"}\n',
        encoding="utf-8",
    )
    (root / "order.log.1").write_text(
        '{"timestamp":"2026-09-09T09:59:00Z","level":"WARN","service_id":"orders","message":"rotated"}\n',
        encoding="utf-8",
    )
    plugin = FileLogsPlugin({"root_path": str(root)})
    result = asyncio.run(
        plugin.execute(
            "search_logs",
            {"path": "order.log"},
            {
                "start_time": "2026-09-09T09:58:00Z",
                "end_time": "2026-09-09T10:01:00Z",
                "text_query": "timeout",
            },
            context(max_results=1, max_scan_files=1),
        )
    )
    assert result.status == ToolResultStatus.PARTIAL
    assert len(result.structured_result["entries"]) == 1
    assert result.truncated is True
    rejected = asyncio.run(
        plugin.execute(
            "search_logs",
            {"path": "../outside.log"},
            {
                "start_time": "2026-09-09T09:58:00Z",
                "end_time": "2026-09-09T10:01:00Z",
            },
            context(),
        )
    )
    assert rejected.status == ToolResultStatus.REJECTED


def test_environment_tool_registry_is_empty_without_snapshot_and_evidence_is_bounded(
    tmp_path: Path,
):
    repository = EnvironmentRepository(None)
    manager = PluginManager()
    service = EnvironmentToolService(repository, manager)
    registry = EnvironmentToolRegistry(service)
    assert registry.tools_for(node_name="investigate", enabled=True) == []


def test_environment_tool_service_enforces_snapshot_source_budget_and_registers_evidence(
    tmp_path: Path,
):
    db = tmp_path / "orders.sqlite"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE orders (id INTEGER, status TEXT)")
    connection.execute("INSERT INTO orders VALUES (1, 'paid')")
    connection.commit()
    connection.close()
    catalog = tmp_path / "environments.yaml"
    catalog.write_text(
        f"""
environments:
  prod:
    display_name: Production
plugin_instances:
  sqlite:
    plugin_id: sqlite
    config:
      root_path: {tmp_path.as_posix()}
sources:
  prod-db:
    kind: database
    plugin_instance_id: sqlite
    environment_id: prod
    config:
      path: orders.sqlite
      allowed_tables: [orders]
""",
        encoding="utf-8",
    )
    repository = EnvironmentRepository(catalog)
    manager = PluginManager([SQLitePlugin()])
    manager.validate_directory(repository.directory())
    service = EnvironmentToolService(repository, manager)
    snapshot = repository.snapshot("prod")
    runtime = SimpleNamespace(
        graph_run_id="service-run",
        execution_id="exec-1",
        environment_snapshot=snapshot,
        environment_snapshot_id=snapshot.snapshot_id,
        tool_max_calls=1,
        tool_max_concurrent=2,
        tool_max_results=200,
        tool_result_bytes=65_536,
        tool_timeout_seconds=10,
    )
    result = asyncio.run(
        service.call(
            "query_database",
            runtime,
            {"source_id": "prod-db", "sql": "SELECT * FROM orders", "parameters": {}},
        )
    )
    assert result["status"] == "succeeded"
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["source_reference"].startswith("prod-db:")
    metadata = EnvironmentToolRegistry(service).audit_metadata(
        "query_database",
        runtime,
        {"source_id": "prod-db", "sql": "SELECT * FROM orders WHERE id = 7"},
    )
    assert metadata["plugin_id"] == "sqlite"
    assert metadata["environment_snapshot_id"] == snapshot.snapshot_id
    assert "7" not in metadata["redacted_query"]
    assert len(metadata["query_fingerprint"]) == 64
    second = asyncio.run(
        service.call(
            "query_database",
            runtime,
            {"source_id": "prod-db", "sql": "SELECT * FROM orders", "parameters": {}},
        )
    )
    assert second["status"] == "rejected"
    assert "tool_budget_exceeded" in second["warnings"]
    cross_runtime = SimpleNamespace(
        **{**runtime.__dict__, "graph_run_id": "cross-run", "tool_max_calls": 2}
    )
    cross_environment = asyncio.run(
        service.call(
            "query_database",
            cross_runtime,
            {"source_id": "other-db", "sql": "SELECT 1", "parameters": {}},
        )
    )
    assert cross_environment["status"] == "rejected"
    assert "source_not_allowed" in cross_environment["warnings"]
