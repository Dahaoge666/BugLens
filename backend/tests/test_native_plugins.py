"""Exercise native driver policies and bounded reads without remote services."""

import asyncio
import io
import json
import stat
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from buglens_plugin_api import ExecutionContext, ToolResultStatus
from buglens_postgresql_plugin import PostgreSQLPlugin
from buglens_postgresql_plugin.sql_policy import bind_sql, guarded_query
from buglens_ssh_logs_plugin import SSHLogsPlugin
from buglens_ssh_plugin import SSHPlugin


def context(**limits):
    return ExecutionContext(
        execution_id="test",
        run_id="run",
        environment_snapshot_id="snapshot",
        plugin_instance_id="instance",
        deadline=datetime.now(UTC) + timedelta(seconds=2),
        **limits,
    )


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "SELECT 1; SELECT 2",
        "WITH removed AS (DELETE FROM orders RETURNING id) SELECT id FROM removed",
        "SELECT id INTO copy FROM orders",
        "SELECT id FROM orders FOR UPDATE",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT lo_export(1, '/tmp/leak')",
        "SELECT app.lower('x')",
        "SELECT set_config('search_path', 'app', false)",
        "SELECT id FROM generate_series(1, 10)",
        "SELECT id FROM other.orders",
        "SELECT id FROM secrets",
        "SELECT 'x'::app.custom_type",
        "SELECT 1 OPERATOR(app.+) 2",
        "SELECT id FROM secrets WHERE EXISTS (WITH secrets AS (SELECT id FROM orders) SELECT 1 FROM secrets)",
    ],
)
def test_postgresql_rejects_writes_external_functions_and_table_bypasses(sql):
    with pytest.raises(ValueError):
        guarded_query(sql, {}, {"allowed_tables": ["orders"]})


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders",
        "SELECT o.* FROM orders o",
        "SELECT password AS visible FROM orders",
        "SELECT o FROM orders o",
        "SELECT json_agg(o) FROM orders o",
    ],
)
def test_postgresql_denied_columns_cannot_be_read_by_alias_or_whole_row(sql):
    with pytest.raises(ValueError, match="column_not_allowed"):
        guarded_query(sql, {}, {"denied_columns": ["password"]})


def test_postgresql_binding_preserves_literals_comments_and_casts():
    sql = "SELECT :id::int, %(id)s, ':ignored', $$:ignored$$ /* :ignored /* nested */ */ -- :ignored\n"
    bound, values = bind_sql(sql, {"id": "1' OR true--"})
    assert bound.startswith("SELECT $1::int, $1, ':ignored', $$:ignored$$")
    assert values == ("1' OR true--",)
    with pytest.raises(ValueError, match="missing_parameter"):
        bind_sql("SELECT :missing", {})
    with pytest.raises(ValueError, match="named_parameters_required"):
        bind_sql("SELECT $1", {})
    normalized, _ = guarded_query(
        "WITH recent AS (SELECT id FROM orders) SELECT count(*) FROM recent",
        {},
        {"schema": "sales", "allowed_tables": ["orders"]},
    )
    assert "sales.orders" in normalized and "pg_catalog.count(*)" in normalized


class FakeConnection:
    def __init__(self):
        self.read_only = False
        self.calls = []
        self.closed = self.rolled_back = False

    def execute(self, sql, parameters=None):
        assert self.read_only, "transaction must be read-only before its first SQL"
        self.calls.append((sql, parameters))
        return SimpleNamespace(fetchone=lambda: (1,))

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_postgresql_uses_native_binds_fresh_readonly_transactions_and_row_limits(
    monkeypatch,
):
    import psycopg

    connections, credentials, queries = [], [], []

    def connect(**kwargs):
        credentials.append(kwargs)
        result = FakeConnection()
        connections.append(result)
        return result

    class Cursor:
        def __init__(self, connection, name):
            assert connection.read_only
            self.description = [SimpleNamespace(name="id")]
            self.rows = iter([(1,), (2,), (3,)])
            self.fetches = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            assert self.fetches == 3

        def execute(self, sql, values):
            queries.append((sql, values))

        def fetchone(self):
            self.fetches += 1
            return next(self.rows, None)

    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(psycopg, "RawServerCursor", Cursor)
    for name in ("orders", "billing"):
        plugin = PostgreSQLPlugin(
            {
                "host": "db",
                "database": "app",
                "username": name,
                "password": name + "-secret",
            }
        )
        result = asyncio.run(
            plugin.execute(
                "query_database",
                {"allowed_tables": ["orders"]},
                {"sql": "SELECT id FROM orders WHERE id=:id", "parameters": {"id": 1}},
                context(max_results=2),
            )
        )
        assert result.status == ToolResultStatus.PARTIAL
        assert result.structured_result["rows"] == [{"id": 1}, {"id": 2}]
        assert name + "-secret" not in result.model_dump_json()
    assert [item["user"] for item in credentials] == ["orders", "billing"]
    assert all(item["sslmode"] == "require" for item in credentials)
    assert all(item.closed and item.rolled_back for item in connections)
    assert all("statement_timeout" in item.calls[0][0] for item in connections)
    assert queries[0] == ("SELECT id FROM public.orders WHERE id = $1", (1,))


def test_postgresql_rejects_before_opening_a_connection(monkeypatch):
    plugin = PostgreSQLPlugin({"host": "db", "database": "app"})
    monkeypatch.setattr(
        plugin, "_connect", lambda _: pytest.fail("rejected query must not connect")
    )
    result = asyncio.run(
        plugin.execute(
            "query_database", {}, {"sql": "SELECT lo_export(1, '/tmp/a')"}, context()
        )
    )
    assert result.status == ToolResultStatus.REJECTED
    result = asyncio.run(
        plugin.execute(
            "describe_database", {"schema": "sales"}, {"schema": "other"}, context()
        )
    )
    assert result.status == ToolResultStatus.REJECTED


def test_postgresql_metadata_filters_tables_columns_patterns_and_output_bytes(
    monkeypatch,
):
    import psycopg

    connections = []
    oversized = False

    def connect(**kwargs):
        connection = FakeConnection()
        connections.append(connection)
        return connection

    class Cursor:
        def __init__(self, connection, name):
            assert connection.read_only
            self.description = [
                SimpleNamespace(name=key)
                for key in ("table_name", "column_name", "data_type")
            ]
            self.rows = iter(
                [
                    ("Orders", "id", "字" * 2000 if oversized else "integer"),
                    ("Orders", "password", "text"),
                    ("Other", "id", "integer"),
                    ("Orders", "status", "text"),
                ]
            )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, values):
            assert "information_schema.columns" in sql and values == ("public",)

        def fetchone(self):
            return next(self.rows, None)

    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(psycopg, "RawServerCursor", Cursor)
    plugin = PostgreSQLPlugin({"host": "db", "database": "app"})
    config = {
        "allowed_tables": ["public.Orders"],
        "denied_columns": ["password", "column_name"],
    }
    result = asyncio.run(
        plugin.execute(
            "describe_database",
            config,
            {"table_pattern": "ORD*"},
            context(max_results=1),
        )
    )
    assert result.status == ToolResultStatus.PARTIAL
    assert result.structured_result["rows"] == [
        {"table_name": "Orders", "column_name": "id", "data_type": "integer"}
    ]
    oversized = True
    result = asyncio.run(
        plugin.execute("describe_database", config, {}, context(max_bytes=1024))
    )
    assert (
        result.status == ToolResultStatus.PARTIAL
        and result.structured_result["rows"] == []
    )
    assert len(json.dumps(result.structured_result).encode()) <= 1024
    assert all(
        connection.closed and connection.rolled_back for connection in connections
    )


class FakeChannel:
    def __init__(self, data=b"Linux\n"):
        self.data, self.closed = data, False

    def set_combine_stderr(self, value):
        assert value is True

    def settimeout(self, value):
        assert value > 0

    def recv_ready(self):
        return bool(self.data)

    def exit_status_ready(self):
        return not self.data

    def recv_exit_status(self):
        assert self.exit_status_ready()
        return 0

    def recv(self, size):
        result, self.data = self.data[:size], self.data[size:]
        return result

    def close(self):
        self.closed = True


def test_ssh_allows_only_enabled_fixed_checks_and_closes_connections(monkeypatch):
    import buglens_ssh_plugin.plugin as module

    channel, calls = FakeChannel(b"Linux\n" * 1000), []
    client = SimpleNamespace(close=lambda: calls.append("closed"))

    def execute(command, **kwargs):
        calls.append((command, kwargs))
        return (
            io.BytesIO(),
            SimpleNamespace(channel=channel, close=lambda: None),
            io.BytesIO(),
        )

    client.exec_command = execute
    monkeypatch.setattr(module, "connect_ssh", lambda config, timeout: client)
    plugin = SSHPlugin({"host": "host"})
    for request in (
        {"check": []},
        {"check": "disk"},
        {"check": "system", "command": "rm -rf /"},
        {"check": "reboot"},
    ):
        assert (
            asyncio.run(
                plugin.execute(
                    "inspect_host", {"checks": ["system"]}, request, context()
                )
            ).status
            == ToolResultStatus.REJECTED
        )
    assert calls == []
    result = asyncio.run(
        plugin.execute(
            "inspect_host",
            {"checks": ["system"]},
            {"check": "system"},
            context(max_results=2, max_bytes=1024),
        )
    )
    assert result.status == ToolResultStatus.PARTIAL
    assert len(result.structured_result["lines"]) == 2
    assert len(json.dumps(result.structured_result).encode()) <= 1024
    assert calls[0][0] == "uname -a" and calls[0][1]["get_pty"] is False
    assert channel.closed and calls[-1] == "closed"


def test_ssh_host_key_policy_never_automatically_trusts_hosts(monkeypatch):
    import paramiko
    from buglens_ssh_plugin.connection import connect_ssh

    calls = []

    class Client:
        def load_system_host_keys(self):
            calls.append("system-keys")

        def load_host_keys(self, path):
            calls.append(path)

        def set_missing_host_key_policy(self, policy):
            assert isinstance(policy, paramiko.RejectPolicy)

        def connect(self, **kwargs):
            calls.append(kwargs)

        def close(self):
            pass

    monkeypatch.setattr(paramiko, "SSHClient", Client)
    connect_ssh(
        {
            "host": "host",
            "username": "reader",
            "password": "private",
            "known_hosts": "keys",
        },
        1,
    )
    assert calls[:2] == ["system-keys", "keys"]
    assert calls[2]["username"] == "reader" and calls[2]["password"] == "private"
    assert calls[2]["timeout"] == 1


class FakeSFTP:
    def __init__(self, files, symlinks=()):
        self.files, self.symlinks, self.opens = files, symlinks, []
        self.closed = False
        self.channel = FakeChannel()

    def get_channel(self):
        return self.channel

    def normalize(self, path):
        return path

    def lstat(self, path):
        mode = (
            stat.S_IFLNK
            if path in self.symlinks
            else stat.S_IFREG
            if path in self.files
            else stat.S_IFDIR
        )
        return SimpleNamespace(st_mode=mode)

    def listdir_iter(self, path, read_aheads):
        assert read_aheads == 1
        return iter(
            SimpleNamespace(filename=name.rsplit("/", 1)[1], st_mode=stat.S_IFREG)
            for name in sorted(self.files)
            if name.rsplit("/", 1)[0] == path
        )

    def open(self, path, mode):
        assert mode == "rb"
        self.opens.append(path)
        return io.BytesIO(self.files[path])

    def close(self):
        self.closed = True


LOG_REQUEST = {
    "start_time": "2026-09-13T00:00:00Z",
    "end_time": "2026-09-13T01:00:00Z",
    "text_query": "timeout",
    "levels": ["error"],
}


def remote_plugin(monkeypatch, files, symlinks=()):
    import buglens_ssh_logs_plugin.plugin as module

    sftp = FakeSFTP(files, symlinks)
    client = SimpleNamespace(open_sftp=lambda: sftp, close=lambda: None)
    monkeypatch.setattr(module, "connect_ssh", lambda config, timeout: client)
    return SSHLogsPlugin({"host": "host", "root_path": "/logs"}), sftp


def test_ssh_logs_searches_rotations_and_pages_with_shared_readonly_parser(monkeypatch):
    line = (
        json.dumps(
            {
                "timestamp": "2026-09-13T00:10:00Z",
                "level": "ERROR",
                "message": "timeout",
                "service_id": "orders",
            }
        ).encode()
        + b"\n"
    )
    plugin, sftp = remote_plugin(
        monkeypatch, {"/logs/app.log": line, "/logs/app.log.1": line}
    )
    first = asyncio.run(
        plugin.execute(
            "search_logs", {"path": "app.log"}, LOG_REQUEST, context(max_results=1)
        )
    )
    assert first.status == ToolResultStatus.PARTIAL and first.cursor == "1"
    assert first.structured_result["entries"][0]["message"] == "timeout"
    second = asyncio.run(
        plugin.execute(
            "search_logs",
            {"path": "app.log"},
            {**LOG_REQUEST, "cursor": first.cursor},
            context(max_results=1),
        )
    )
    assert second.status == ToolResultStatus.SUCCEEDED
    assert (
        second.structured_result["entries"][0]["reference"]
        != first.structured_result["entries"][0]["reference"]
    )
    assert sftp.closed and all(path.startswith("/logs/") for path in sftp.opens)


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../secret",
        "nested/../secret",
        "**/*.log",
        "*/app.log",
        "a\\b.log",
        ".",
    ],
)
def test_ssh_logs_rejects_path_escape_before_connecting(monkeypatch, path):
    import buglens_ssh_logs_plugin.plugin as module

    monkeypatch.setattr(
        module, "connect_ssh", lambda *_: pytest.fail("invalid path must not connect")
    )
    plugin = SSHLogsPlugin({"host": "host", "root_path": "/logs"})
    assert (
        asyncio.run(
            plugin.execute("search_logs", {"path": path}, LOG_REQUEST, context())
        ).status
        == ToolResultStatus.REJECTED
    )


def test_ssh_logs_rejects_symlink_and_caps_scanned_bytes(monkeypatch):
    plugin, sftp = remote_plugin(
        monkeypatch, {"/logs/app.log": b"x" * 20000}, ["/logs/app.log"]
    )
    assert (
        asyncio.run(
            plugin.execute("search_logs", {"path": "app.log"}, LOG_REQUEST, context())
        ).status
        == ToolResultStatus.REJECTED
    )
    assert sftp.opens == [] and sftp.closed
    plugin, _ = remote_plugin(monkeypatch, {"/logs/app.log": b"x" * 20000})
    result = asyncio.run(
        plugin.execute(
            "search_logs",
            {"path": "app.log"},
            LOG_REQUEST,
            context(max_scan_bytes=1024),
        )
    )
    assert (
        result.status == ToolResultStatus.PARTIAL
        and "scan_limit_reached" in result.warnings
    )
    assert "1024 bytes" in result.source_references[0].locator


def test_host_gateway_registers_evidence_and_rejects_unknown_checks(
    monkeypatch, tmp_path
):
    import buglens_ssh_plugin.plugin as module
    import yaml

    from app.environment import EnvironmentRepository
    from app.plugins import (
        EnvironmentToolRegistry,
        EnvironmentToolService,
        PluginManager,
    )

    channel = FakeChannel()
    client = SimpleNamespace(close=lambda: None)
    client.exec_command = lambda *args, **kwargs: (
        io.BytesIO(),
        SimpleNamespace(channel=channel, close=lambda: None),
        io.BytesIO(),
    )
    monkeypatch.setattr(module, "connect_ssh", lambda *_: client)
    catalog = tmp_path / "environments.yaml"
    catalog.write_text(
        yaml.safe_dump(
            {
                "environments": {"prod": {"display_name": "Production"}},
                "services": {"orders": {"name": "Orders", "environment_id": "prod"}},
                "plugin_instances": {
                    "host": {
                        "plugin_id": "ssh",
                        "environment_id": "prod",
                        "service_id": "orders",
                        "config": {"host": "host"},
                        "password": "private-password",
                    }
                },
                "sources": {
                    "host-info": {
                        "kind": "host",
                        "environment_id": "prod",
                        "plugin_instance_id": "host",
                        "service_ids": ["orders"],
                        "config": {"checks": ["system"]},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    repository = EnvironmentRepository(catalog)
    manager = PluginManager([SSHPlugin()])
    manager.validate_directory(repository.directory())
    service = EnvironmentToolService(repository, manager)
    snapshot = repository.snapshot("prod")
    runtime = SimpleNamespace(
        graph_run_id="run",
        execution_id="test",
        environment_snapshot=snapshot,
        environment_snapshot_id=snapshot.snapshot_id,
        tool_max_calls=5,
        tool_max_concurrent=2,
        tool_max_results=200,
        tool_result_bytes=65536,
        tool_timeout_seconds=2,
    )
    rejected = asyncio.run(
        service.call(
            "inspect_host", runtime, {"source_id": "host-info", "check": "reboot"}
        )
    )
    assert rejected["status"] == "rejected"
    result = asyncio.run(
        service.call(
            "inspect_host", runtime, {"source_id": "host-info", "check": "system"}
        )
    )
    assert result["status"] == "succeeded" and len(result["evidence"]) == 1
    assert "private-password" not in json.dumps(result)
    metadata = EnvironmentToolRegistry(service).audit_metadata(
        "inspect_host", runtime, {"source_id": "host-info", "check": "system"}
    )
    assert metadata["plugin_id"] == "ssh" and metadata["operation"] == "inspect_host"
    manager.close()


def test_ssh_logs_rejects_invalid_time_before_connecting(monkeypatch):
    import buglens_ssh_logs_plugin.plugin as module

    monkeypatch.setattr(
        module, "connect_ssh", lambda *_: pytest.fail("invalid time must not connect")
    )
    plugin = SSHLogsPlugin({"host": "host", "root_path": "/logs"})
    result = asyncio.run(
        plugin.execute("search_logs", {"path": "app.log"}, {}, context())
    )
    assert (
        result.status == ToolResultStatus.REJECTED
        and "absolute_time_required" in result.warnings
    )


@pytest.mark.parametrize("failure", ["open", "read"])
def test_ssh_logs_remote_failures_are_unavailable_not_empty_success(
    monkeypatch, failure
):
    plugin, sftp = remote_plugin(monkeypatch, {"/logs/app.log": b"log"})

    class BrokenFile(io.BytesIO):
        def read(self, size):
            raise TimeoutError("private host detail")

    def open_file(path, mode):
        if failure == "open":
            raise PermissionError("private host detail")
        return BrokenFile()

    monkeypatch.setattr(sftp, "open", open_file)
    result = asyncio.run(
        plugin.execute("search_logs", {"path": "app.log"}, LOG_REQUEST, context())
    )
    assert (
        result.status == ToolResultStatus.UNAVAILABLE
        and result.structured_result is None
    )
    assert "private host detail" not in result.model_dump_json() and sftp.closed
