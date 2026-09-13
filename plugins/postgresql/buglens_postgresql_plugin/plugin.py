"""Fresh read-only PostgreSQL transactions, AST policy and bounded cursors."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import date, datetime
from decimal import Decimal
from fnmatch import fnmatchcase
from typing import Any

from buglens_plugin_api import (
    ExecutionContext,
    PluginHealth,
    PluginManifest,
    SourceReference,
    ToolResult,
    ToolResultStatus,
)

from .sql_policy import IDENTIFIER, guarded_query

manifest = PluginManifest(
    plugin_id="postgresql",
    implementation_version="1.0.0",
    display_name="PostgreSQL 数据库",
    category="database",
    description="通过独立只读事务查看表结构、查询 PostgreSQL 诊断证据。",
    capabilities=["database", "database.describe.v1", "database.query.v1"],
    instance_config_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["host", "database"],
        "properties": {
            "host": {"type": "string", "minLength": 1, "maxLength": 512},
            "port": {
                "type": "integer",
                "minimum": 1,
                "maximum": 65535,
                "default": 5432,
            },
            "database": {"type": "string", "minLength": 1, "maxLength": 128},
            "sslmode": {
                "type": "string",
                "enum": ["require", "verify-ca", "verify-full", "prefer", "disable"],
                "default": "require",
            },
            "sslrootcert": {"type": "string", "maxLength": 4096},
        },
    },
    source_config_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema": {
                "type": "string",
                "pattern": IDENTIFIER.pattern,
                "default": "public",
            },
            "allowed_tables": {"type": "array", "items": {"type": "string"}},
            "denied_columns": {"type": "array", "items": {"type": "string"}},
            "table_pattern": {"type": "string", "maxLength": 256},
        },
    },
)


def _json_value(value):
    if isinstance(value, (date, datetime, Decimal)):
        return str(value)
    if isinstance(value, bytes):
        return "[binary]"
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return (
        value
        if value is None or isinstance(value, (str, int, float, bool))
        else str(value)
    )


class PostgreSQLPlugin:
    manifest = manifest

    def __init__(self, instance_config: dict[str, Any] | None = None):
        self.instance_config = dict(instance_config or {})

    def validate_config(self, instance_config, source_config=None):
        for key in ("host", "database"):
            value = instance_config.get(key)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError(f"PostgreSQL {key} is required")
        if source_config is not None and not IDENTIFIER.fullmatch(
            source_config.get("schema", "public")
        ):
            raise ValueError("invalid PostgreSQL schema")

    def _connect(self, timeout: float):
        import psycopg

        config = self.instance_config
        kwargs = {
            key: config[key]
            for key in ("host", "port", "sslrootcert")
            if config.get(key) is not None
        }
        kwargs.update(
            dbname=config["database"],
            user=config.get("username"),
            password=config.get("password"),
            sslmode=config.get("sslmode", "require"),
            connect_timeout=max(1, math.ceil(timeout)),
            application_name="BugLens-read-only",
        )
        connection = psycopg.connect(**kwargs)
        try:
            connection.read_only = True
            milliseconds = max(1, int(timeout * 1000))
            connection.execute(
                "SELECT pg_catalog.set_config('statement_timeout', %s, true), "
                "pg_catalog.set_config('lock_timeout', %s, true), "
                "pg_catalog.set_config('search_path', 'pg_catalog', true)",
                (str(milliseconds), str(min(1000, milliseconds))),
            )
        except Exception:
            connection.close()
            raise
        return connection

    async def check_health(self):
        def check():
            try:
                connection = self._connect(5)
                try:
                    connection.execute("SELECT 1").fetchone()
                finally:
                    connection.rollback()
                    connection.close()
                return PluginHealth(
                    status="ok", detail="PostgreSQL database is readable"
                )
            except Exception:
                return PluginHealth(
                    status="error", detail="PostgreSQL database is unavailable"
                )

        return await asyncio.to_thread(check)

    async def execute(
        self, operation, source_config, request, context: ExecutionContext
    ):
        return await asyncio.to_thread(
            self._execute_sync, operation, source_config, request, context
        )

    def _execute_sync(self, operation, source_config, request, context):
        if operation not in {"query_database", "describe_database"}:
            return ToolResult(
                status=ToolResultStatus.REJECTED, warnings=["unsupported_operation"]
            )
        if context.remaining_seconds <= 0:
            return ToolResult(
                status=ToolResultStatus.UNAVAILABLE, warnings=["database_timeout"]
            )
        try:
            if operation == "query_database":
                sql, values = guarded_query(
                    request.get("sql"), request.get("parameters") or {}, source_config
                )
            else:
                schema = source_config.get("schema", "public")
                if request.get("schema") not in (None, schema):
                    raise ValueError("schema_not_allowed")
                sql = "SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema = $1 ORDER BY table_name, ordinal_position"
                values = (schema,)
        except (ValueError, TypeError, KeyError) as exc:
            code = str(exc) if isinstance(exc, ValueError) else "invalid_query"
            return ToolResult(status=ToolResultStatus.REJECTED, warnings=[code])
        connection = None
        try:
            from psycopg import RawServerCursor

            connection = self._connect(min(5, context.remaining_seconds))
            rows: list[dict[str, Any]] = []
            truncated = False
            denied = set(source_config.get("denied_columns", []))
            with RawServerCursor(connection, "buglens_readonly") as cursor:
                cursor.execute(sql, values)
                columns = [column.name for column in cursor.description]
                returned_columns = (
                    columns
                    if operation == "describe_database"
                    else [name for name in columns if name not in denied]
                )
                if (
                    len(json.dumps({"columns": returned_columns, "rows": []}).encode())
                    > context.max_bytes
                ):
                    return ToolResult(
                        status=ToolResultStatus.PARTIAL,
                        structured_result={"columns": [], "rows": []},
                        truncated=True,
                        warnings=["result_limit_reached"],
                    )
                while True:
                    if context.remaining_seconds <= 0:
                        truncated = True
                        break
                    row = cursor.fetchone()
                    if row is None:
                        break
                    record = {
                        key: _json_value(value)
                        for key, value in zip(columns, row)
                        if operation == "describe_database" or key not in denied
                    }
                    if operation == "describe_database":
                        name = record["table_name"]
                        allowed = source_config.get("allowed_tables", [])
                        if (
                            (
                                allowed
                                and name not in allowed
                                and f"{values[0]}.{name}" not in allowed
                            )
                            or record["column_name"] in denied
                            or not fnmatchcase(
                                name.casefold(),
                                (
                                    request.get("table_pattern")
                                    or source_config.get("table_pattern")
                                    or "*"
                                ).casefold(),
                            )
                        ):
                            continue
                    if (
                        len(rows) >= context.max_results
                        or len(
                            json.dumps(
                                {"columns": returned_columns, "rows": [*rows, record]},
                                ensure_ascii=False,
                            ).encode()
                        )
                        > context.max_bytes
                    ):
                        truncated = True
                        break
                    rows.append(record)
            return ToolResult(
                status=ToolResultStatus.PARTIAL
                if truncated
                else ToolResultStatus.SUCCEEDED,
                structured_result={
                    "columns": returned_columns,
                    "rows": rows,
                },
                truncated=truncated,
                warnings=["result_limit_reached"] if truncated else [],
                source_references=[
                    SourceReference(
                        source_id="postgresql",
                        locator=f"query:{hashlib.sha256(sql.encode()).hexdigest()[:24]}",
                    )
                ],
            )
        except Exception:
            return ToolResult(
                status=ToolResultStatus.UNAVAILABLE, warnings=["database_unavailable"]
            )
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
                finally:
                    try:
                        connection.close()
                    except Exception:
                        pass

    def close(self):
        pass


def plugin_factory(instance_config=None):
    return PostgreSQLPlugin(instance_config)


plugin_factory.__buglens_manifest__ = manifest
