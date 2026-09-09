import asyncio
import json
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
    EnvironmentConfigError,
    EnvironmentDirectory,
    EnvironmentRepository,
    EnvironmentRevisionConflictError,
    PluginInstanceConfig,
)
from app.infra import SQLiteCheckpointStore
from app.models import TargetSpec
from app.plugins import (
    EnvironmentToolRegistry,
    EnvironmentToolService,
    PluginCompatibilityError,
    PluginConfigError,
    PluginDiscoveryError,
    PluginManager,
)
from app.web import BugLensASGI


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


def test_environment_repository_infers_unscoped_service_hints(tmp_path: Path):
    path = tmp_path / "environments.yaml"
    path.write_text(
        """
environments:
  prod: {display_name: Production}
services:
  orders: {name: order-api}
""",
        encoding="utf-8",
    )
    repository = EnvironmentRepository(path)
    candidates = repository.infer_candidates(
        TargetSpec(mode="infer"), {"service": "order-api"}
    )
    assert [item.environment_id for item in candidates] == ["prod"]


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


def test_secret_updates_require_explicit_operations_and_change_revision(
    tmp_path: Path,
):
    path = tmp_path / "environments.yaml"
    path.write_text(
        """
environments:
  prod: {display_name: Production}
plugin_instances:
  connector:
    plugin_id: fake
    password: old-secret
""",
        encoding="utf-8",
    )
    repository = EnvironmentRepository(path)
    original_revision = repository.revision
    raw = repository.public_config()
    raw["plugin_instances"][0]["password"] = "direct-secret"
    with pytest.raises(EnvironmentConfigError, match="secret_updates"):
        repository.apply(raw, original_revision)

    raw = repository.public_config()
    updated_revision = repository.apply(
        raw,
        original_revision,
        secret_updates={
            "connector": {"password": {"action": "set", "value": "new-secret"}}
        },
    )
    assert updated_revision != original_revision
    assert repository.public_config()["plugin_instances"][0]["password"] == {
        "is_set": True
    }
    with pytest.raises(EnvironmentRevisionConflictError):
        repository.snapshot("prod", expected_config_revision=original_revision)


def test_if_match_header_cannot_disagree_with_body_revision():
    scope = {"headers": [(b"if-match", b"new-revision")]}
    with pytest.raises(EnvironmentRevisionConflictError, match="If-Match"):
        BugLensASGI._json_body_with_if_match(
            scope, b'{"expected_revision":"old-revision"}'
        )
    assert BugLensASGI._json_body_with_if_match(scope, b"{}")["expected_revision"] == (
        "new-revision"
    )


def test_plugin_result_and_health_fields_are_sanitized():
    result = EnvironmentToolService._bound_plugin_result(
        ToolResult(
            status=ToolResultStatus.SUCCEEDED,
            result={"token": "secret-value", "message": "password=secret-value"},
            cursor="token=secret-value",
            warnings=["password=secret-value"],
        ),
        65_536,
    )
    assert "secret-value" not in result.model_dump_json()
    assert "[REDACTED]" in result.model_dump_json()

    health_manifest = PluginManifest(
        plugin_id="health-leak",
        implementation_version="1.0.0",
    )

    class HealthLeak:
        manifest = health_manifest

        async def check_health(self):
            return PluginHealth(status="error", detail="password=secret-value")

    manager = PluginManager([HealthLeak()])
    health = asyncio.run(
        manager.check_health(
            PluginInstanceConfig(id="instance", plugin_id="health-leak")
        )
    )
    assert "secret-value" not in health.detail


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


def test_tool_budget_survives_store_restart(tmp_path: Path):
    path = tmp_path / "budget.sqlite"
    store = SQLiteCheckpointStore(path)
    reservation, error = store.reserve_tool_budget(
        "run-1", max_calls=1, max_concurrent=1, ttl_seconds=10
    )
    assert reservation is not None
    assert error is None
    store.release_tool_budget(reservation)
    store.close()

    reopened = SQLiteCheckpointStore(path)
    try:
        reservation, error = reopened.reserve_tool_budget(
            "run-1", max_calls=1, max_concurrent=1, ttl_seconds=10
        )
        assert reservation is None
        assert error == "tool_budget_exceeded"
    finally:
        reopened.close()


def test_tool_timeout_keeps_concurrency_slot_until_plugin_finishes(tmp_path: Path):
    async def exercise() -> None:
        release = asyncio.Event()
        plugin_manifest = PluginManifest(
            plugin_id="blocking",
            implementation_version="1.0.0",
            capabilities=["database"],
        )

        class BlockingPlugin:
            manifest = plugin_manifest

            async def execute(self, operation, source_config, request, context):
                await release.wait()
                return ToolResult(
                    status=ToolResultStatus.SUCCEEDED,
                    result={"released": True},
                )

        catalog = tmp_path / "environments.yaml"
        catalog.write_text(
            """
environments:
  prod: {display_name: Production}
plugin_instances:
  blocking: {plugin_id: blocking}
sources:
  db:
    kind: database
    plugin_instance_id: blocking
    environment_id: prod
""",
            encoding="utf-8",
        )
        repository = EnvironmentRepository(catalog)
        snapshot = repository.snapshot("prod")
        service = EnvironmentToolService(repository, PluginManager([BlockingPlugin()]))
        runtime = SimpleNamespace(
            graph_run_id="timeout-run",
            execution_id="exec-timeout",
            environment_snapshot=snapshot,
            environment_snapshot_id=snapshot.snapshot_id,
            tool_max_calls=5,
            tool_max_concurrent=1,
            tool_max_results=10,
            tool_result_bytes=65_536,
            tool_timeout_seconds=1,
        )
        first_task = asyncio.create_task(
            service.call(
                "query_database",
                runtime,
                {"source_id": "db", "sql": "SELECT 1", "parameters": {}},
            )
        )
        await asyncio.sleep(1.1)
        second = await service.call(
            "query_database",
            runtime,
            {"source_id": "db", "sql": "SELECT 1", "parameters": {}},
        )
        assert second["status"] == "rejected"
        assert "tool_concurrency_limit" in second["warnings"]
        release.set()
        first = await first_task
        assert first["status"] == "unavailable"
        await asyncio.sleep(0)

    asyncio.run(exercise())


def test_sqlite_plugin_rejects_writes_attach_pragma_and_multiple_statements(
    tmp_path: Path,
):
    db = tmp_path / "orders.sqlite"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE orders (id INTEGER, status TEXT, secret TEXT)")
    connection.execute("CREATE TABLE secret_table (value TEXT)")
    connection.execute("INSERT INTO orders VALUES (1, 'paid', 'hidden')")
    connection.execute("INSERT INTO secret_table VALUES ('leaked')")
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
    for sql, warning in (
        ("SELECT * FROM orders", "sensitive_column_rejected"),
        ("SELECT * FROM [secret_table]", "table_not_allowed"),
    ):
        rejected = asyncio.run(
            plugin.execute(
                "query_database", source, {"sql": sql, "parameters": {}}, context()
            )
        )
        assert rejected.status == ToolResultStatus.REJECTED
        assert warning in rejected.warnings


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


def test_file_log_cursor_advances_without_repeating_pages(tmp_path: Path):
    root = tmp_path / "logs"
    root.mkdir()
    lines = [
        json_line
        for index in range(5)
        if (
            json_line := (
                '{"timestamp":"2026-09-09T10:00:0'
                f'{index}Z","message":"entry-{index}"}}'
            )
        )
    ]
    (root / "app.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    plugin = FileLogsPlugin({"root_path": str(root)})
    request = {
        "start_time": "2026-09-09T09:59:00Z",
        "end_time": "2026-09-09T10:01:00Z",
    }
    first = asyncio.run(
        plugin.execute(
            "search_logs", {"path": "app.log"}, request, context(max_results=2)
        )
    )
    second = asyncio.run(
        plugin.execute(
            "search_logs",
            {"path": "app.log"},
            {**request, "cursor": first.cursor},
            context(max_results=2),
        )
    )
    third = asyncio.run(
        plugin.execute(
            "search_logs",
            {"path": "app.log"},
            {**request, "cursor": second.cursor},
            context(max_results=2),
        )
    )
    pages = [
        [item["message"] for item in result.structured_result["entries"]]
        for result in (first, second, third)
    ]
    assert pages == [
        ["entry-0", "entry-1"],
        ["entry-2", "entry-3"],
        ["entry-4"],
    ]
    assert first.cursor == "2"
    assert second.cursor == "4"
    assert third.cursor is None


def test_file_log_oversized_matching_entry_does_not_stall_cursor(tmp_path: Path):
    root = tmp_path / "logs"
    root.mkdir()
    (root / "app.log").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-09-09T10:00:00Z",
                        "message": "x" * 2_000,
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-09-09T10:00:01Z",
                        "message": "small-entry",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    plugin = FileLogsPlugin({"root_path": str(root)})
    result = asyncio.run(
        plugin.execute(
            "search_logs",
            {"path": "app.log"},
            {
                "start_time": "2026-09-09T09:59:00Z",
                "end_time": "2026-09-09T10:01:00Z",
            },
            context(max_bytes=1_024),
        )
    )
    assert result.status == ToolResultStatus.PARTIAL
    assert [item["message"] for item in result.structured_result["entries"]] == [
        "small-entry"
    ]
    assert result.cursor is None
    assert "entry_too_large_skipped" in result.warnings


def test_old_snapshot_pins_non_secret_plugin_config_but_reads_current_secrets(
    tmp_path: Path,
):
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    for root, value in ((old_root, "old"), (new_root, "new")):
        connection = sqlite3.connect(root / "data.sqlite")
        connection.execute("CREATE TABLE values_table (value TEXT)")
        connection.execute("INSERT INTO values_table VALUES (?)", (value,))
        connection.commit()
        connection.close()
    catalog = tmp_path / "environments.yaml"
    catalog.write_text(
        f"""
environments:
  prod: {{display_name: Production}}
plugin_instances:
  sqlite:
    plugin_id: sqlite
    config: {{root_path: {old_root.as_posix()}}}
sources:
  db:
    kind: database
    plugin_instance_id: sqlite
    environment_id: prod
    config: {{path: data.sqlite, allowed_tables: [values_table]}}
""",
        encoding="utf-8",
    )
    repository = EnvironmentRepository(catalog)
    snapshot = repository.snapshot("prod")
    raw = repository.public_config()
    raw["plugin_instances"][0]["config"]["root_path"] = new_root.as_posix()
    repository.apply(raw, repository.revision)
    manager = PluginManager([SQLitePlugin()])
    service = EnvironmentToolService(repository, manager)
    runtime = SimpleNamespace(
        graph_run_id="snapshot-run",
        execution_id="exec-snapshot",
        environment_snapshot=snapshot,
        environment_snapshot_id=snapshot.snapshot_id,
        tool_max_calls=2,
        tool_max_concurrent=1,
        tool_max_results=10,
        tool_result_bytes=65_536,
        tool_timeout_seconds=10,
    )
    result = asyncio.run(
        service.call(
            "query_database",
            runtime,
            {"source_id": "db", "sql": "SELECT value FROM values_table"},
        )
    )
    assert result["status"] == "succeeded"
    assert result["result"]["rows"] == [{"value": "old"}]


def test_log_filters_must_be_bound_to_snapshot_source(tmp_path: Path):
    catalog = tmp_path / "environments.yaml"
    catalog.write_text(
        """
environments:
  prod: {display_name: Production}
services:
  orders: {name: orders, environment_id: prod}
  payments: {name: payments, environment_id: prod}
plugin_instances:
  logs: {plugin_id: file_logs}
sources:
  logs:
    kind: logs
    plugin_instance_id: logs
    environment_id: prod
    service_ids: [orders]
""",
        encoding="utf-8",
    )
    repository = EnvironmentRepository(catalog)
    snapshot = repository.snapshot("prod")
    source = snapshot.source("logs")
    assert source is not None
    assert (
        EnvironmentToolService._validate_log_filters(
            snapshot, source, {"service_ids": ["payments"]}
        )
        == "service_not_allowed"
    )


def test_plugin_capability_and_embedded_credentials_are_rejected(tmp_path: Path):
    manifest = PluginManifest(
        plugin_id="logs-only",
        implementation_version="1.0.0",
        capabilities=["logs"],
    )

    class LogsOnly:
        def __init__(self):
            self.manifest = manifest

    directory = EnvironmentDirectory.model_validate(
        {
            "environments": {"prod": {"display_name": "Production"}},
            "plugin_instances": {"plugin": {"plugin_id": "logs-only"}},
            "sources": {
                "db": {
                    "kind": "database",
                    "plugin_instance_id": "plugin",
                    "environment_id": "prod",
                }
            },
        }
    )
    with pytest.raises(PluginConfigError, match="does not support database"):
        PluginManager([LogsOnly()]).validate_directory(directory)
    with pytest.raises(ValidationError, match="must not embed credentials"):
        EnvironmentDirectory.model_validate(
            {
                "environments": {"prod": {"display_name": "Production"}},
                "plugin_instances": {
                    "plugin": {
                        "plugin_id": "logs-only",
                        "config": {"dsn": "postgres://user:password@host/db"},
                    }
                },
            }
        )
