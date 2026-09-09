"""Deterministic, read-only SQLite connector.

The connector opens a fresh URI ``mode=ro`` connection for every operation and
uses both SQL policy checks and SQLite's authorizer callback.  It never accepts
an arbitrary shell command or returns a database connection to the caller.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from buglens_plugin_api import (
    ExecutionContext,
    PluginHealth,
    PluginManifest,
    SourceReference,
    ToolResult,
    ToolResultStatus,
)

manifest = PluginManifest(
    plugin_id="sqlite",
    implementation_version="1.0.1",
    api_major=1,
    api_version="1",
    capabilities=["database", "read_only_sql", "schema_description"],
    instance_config_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "root_path": {"type": "string", "minLength": 1},
            "health_path": {"type": "string", "minLength": 1},
        },
    },
    source_config_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "allowed_tables": {"type": "array", "items": {"type": "string"}},
            "denied_columns": {"type": "array", "items": {"type": "string"}},
            "table_pattern": {"type": "string", "maxLength": 256},
        },
    },
    health_check=True,
)


_FORBIDDEN_FIRST_WORDS = {
    "alter",
    "analyze",
    "attach",
    "call",
    "commit",
    "copy",
    "create",
    "delete",
    "detach",
    "do",
    "drop",
    "insert",
    "load",
    "pragma",
    "reindex",
    "release",
    "replace",
    "rollback",
    "savepoint",
    "vacuum",
    "update",
}
_FORBIDDEN_TOKENS = re.compile(
    r"\b(?:attach|detach|load_extension|vacuum|pragma|create|alter|drop|"
    r"insert|update|delete|replace|merge|call|do|copy|begin|commit|rollback|"
    r"savepoint|release)\b",
    re.IGNORECASE,
)
_TABLE_REFERENCE = re.compile(
    r"\b(?:from|join|into|update|table)\s+([\"'`]?)([A-Za-z_][\w.$-]*)\1",
    re.IGNORECASE,
)


class SQLitePlugin:
    manifest = manifest

    def __init__(self, instance_config: dict[str, Any] | None = None) -> None:
        self.instance_config = dict(instance_config or {})

    def configure(self, instance_config: dict[str, Any]) -> None:
        self.instance_config = dict(instance_config)

    def validate_config(
        self,
        instance_config: dict[str, Any],
        source_config: dict[str, Any] | None = None,
    ) -> None:
        root_path = instance_config.get("root_path")
        if root_path is not None and (not isinstance(root_path, str) or not root_path):
            raise ValueError("sqlite root_path must be a non-empty string")
        if source_config is None:
            return
        path = source_config.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("sqlite source path is required")
        if root_path:
            self._resolve_path(path, root_path)

    async def check_health(self) -> PluginHealth:
        return await asyncio.to_thread(self._check_health_sync)

    def _check_health_sync(self) -> PluginHealth:
        health_path = self.instance_config.get("health_path")
        if not health_path:
            return PluginHealth(status="ok", detail="SQLite plugin is loaded")
        try:
            path = self._resolve_path(
                str(health_path), self.instance_config.get("root_path")
            )
            connection = self._connect(path)
            try:
                connection.execute("SELECT 1").fetchone()
            finally:
                connection.close()
            return PluginHealth(status="ok", detail="SQLite database is readable")
        except Exception:
            return PluginHealth(status="error", detail="SQLite database is unavailable")

    async def execute(
        self,
        operation: str,
        source_config: dict[str, Any],
        request: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        return await asyncio.to_thread(
            self._execute_sync, operation, source_config, request, context
        )

    def _execute_sync(
        self,
        operation: str,
        source_config: dict[str, Any],
        request: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        started = time.monotonic()
        schema = request.get("schema")
        if schema not in (None, "", "main"):
            return self._rejected("schema_not_allowed")
        try:
            path = self._resolve_path(
                str(source_config.get("path", "")),
                self.instance_config.get("root_path"),
            )
        except ValueError:
            return ToolResult(
                status=ToolResultStatus.REJECTED,
                warnings=["source_path_rejected"],
            )
        if operation == "describe_database":
            result = self._describe(path, source_config, request, context)
        elif operation == "query_database":
            result = self._query(path, source_config, request, context)
        else:
            result = ToolResult(
                status=ToolResultStatus.REJECTED,
                warnings=["unsupported_operation"],
            )
        if result.elapsed_ms is None:
            result = result.model_copy(
                update={"elapsed_ms": int((time.monotonic() - started) * 1000)}
            )
        return result

    def close(self) -> None:
        return None

    def _resolve_path(self, raw_path: str, root_path: str | None) -> Path:
        if not raw_path or "\x00" in raw_path:
            raise ValueError("invalid sqlite path")
        raw = Path(raw_path)
        if root_path:
            root = Path(root_path).expanduser().resolve()
            candidate = (
                (root / raw).resolve() if not raw.is_absolute() else raw.resolve()
            )
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise ValueError("sqlite path escapes configured root") from exc
        else:
            candidate = raw.expanduser().resolve()
        if not candidate.is_file():
            raise ValueError("sqlite database file does not exist")
        return candidate

    def _connect(
        self,
        path: Path,
        source_config: dict[str, Any] | None = None,
        denied_reason: list[str] | None = None,
    ) -> sqlite3.Connection:
        uri = f"file:{path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        if source_config is None:
            connection.set_authorizer(self._authorizer)
        else:
            connection.set_authorizer(
                self._policy_authorizer(
                    source_config,
                    denied_reason if denied_reason is not None else [],
                )
            )
        return connection

    def _policy_authorizer(
        self,
        source_config: dict[str, Any],
        denied_reason: list[str],
    ) -> Any:
        allowed = self._allowed_tables(source_config)
        denied_columns = {
            str(item).casefold()
            for item in source_config.get("denied_columns", [])
            if item
        }

        def authorize(
            action: int,
            arg1: str | None,
            arg2: str | None,
            _database: str | None,
            origin: str | None,
        ) -> int:
            base = self._authorizer(action, arg1, arg2)
            if base != sqlite3.SQLITE_OK:
                denied_reason[:] = ["read_only_statement_rejected"]
                return base
            if action != sqlite3.SQLITE_READ:
                return sqlite3.SQLITE_OK
            table = str(arg1 or "").casefold()
            column = str(arg2 or "").casefold()
            view = str(origin or "").casefold()
            if column and column in denied_columns:
                denied_reason[:] = ["sensitive_column_rejected"]
                return sqlite3.SQLITE_DENY
            if allowed and table not in allowed and view not in allowed:
                denied_reason[:] = ["table_not_allowed"]
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        return authorize

    @staticmethod
    def _authorizer(
        action: int, arg1: str | None, arg2: str | None, *_args: Any
    ) -> int:
        denied = {
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_INDEX,
            sqlite3.SQLITE_CREATE_TEMP_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
            sqlite3.SQLITE_CREATE_TEMP_VIEW,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_INDEX,
            sqlite3.SQLITE_DROP_TEMP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_TRIGGER,
            sqlite3.SQLITE_DROP_TEMP_VIEW,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_DETACH,
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_TRANSACTION,
            sqlite3.SQLITE_SAVEPOINT,
            sqlite3.SQLITE_PRAGMA,
        }
        if action in denied:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and str(arg2 or arg1 or "").casefold() in {
            "load_extension",
            "fts3_tokenizer",
        }:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def _describe(
        self,
        path: Path,
        source_config: dict[str, Any],
        request: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        allowed = self._allowed_tables(source_config)
        pattern = request.get("table_pattern") or source_config.get("table_pattern")
        if pattern is not None and (not isinstance(pattern, str) or len(pattern) > 256):
            return self._rejected("table_pattern_rejected")
        connection = None
        try:
            connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            rows = connection.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
            ).fetchall()
            tables = []
            truncated = False
            max_results = min(context.max_results, 1_000)
            max_bytes = min(context.max_bytes, 1_048_576)
            denied_columns = {
                str(item).casefold()
                for item in source_config.get("denied_columns", [])
                if item
            }
            for name, kind in rows:
                if context.remaining_seconds <= 0:
                    return ToolResult(
                        status=(
                            ToolResultStatus.PARTIAL
                            if tables
                            else ToolResultStatus.UNAVAILABLE
                        ),
                        structured_result={"tables": tables} if tables else None,
                        truncated=bool(tables),
                        warnings=["query_timeout"],
                    )
                if allowed and str(name).casefold() not in allowed:
                    continue
                if pattern and not fnmatchcase(
                    str(name).casefold(), str(pattern).casefold()
                ):
                    continue
                columns = [
                    {"name": row[1], "type": row[2] or ""}
                    for row in connection.execute(
                        f"PRAGMA table_info({self._quote_identifier(name)})"
                    ).fetchall()
                    if str(row[1]).casefold() not in denied_columns
                ]
                candidate = {"name": name, "type": kind, "columns": columns}
                encoded = json.dumps(
                    {"tables": [*tables, candidate]},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                if len(tables) >= max_results or len(encoded) > max_bytes:
                    truncated = True
                    break
                tables.append(candidate)
            return ToolResult(
                status=(
                    ToolResultStatus.PARTIAL
                    if truncated
                    else ToolResultStatus.SUCCEEDED
                ),
                structured_result={"tables": tables},
                truncated=truncated,
                cursor=str(len(tables)) if truncated else None,
                source_references=[
                    SourceReference(source_id="sqlite", locator=f"{path.name}:schema")
                ],
            )
        except sqlite3.Error:
            return ToolResult(
                status=ToolResultStatus.UNAVAILABLE,
                warnings=["database_unavailable"],
            )
        finally:
            if connection is not None:
                connection.close()

    def _query(
        self,
        path: Path,
        source_config: dict[str, Any],
        request: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        sql = request.get("sql")
        parameters = request.get("parameters", {})
        if not isinstance(sql, str) or not sql.strip():
            return self._rejected("sql_required")
        policy_error = self._validate_sql(sql, source_config, parameters)
        if policy_error:
            return self._rejected(policy_error)
        if not isinstance(parameters, dict) or any(
            not isinstance(value, (str, int, float, bool)) and value is not None
            for value in parameters.values()
        ):
            return self._rejected("parameters_must_be_json_scalars")
        connection = None
        timed_out = False
        denied_reason: list[str] = []

        def progress() -> int:
            nonlocal timed_out
            if context.remaining_seconds <= 0:
                timed_out = True
                return 1
            return 0

        try:
            connection = self._connect(path, source_config, denied_reason)
            connection.set_progress_handler(progress, 1_000)
            cursor = connection.execute(sql, parameters)
            columns = [description[0] for description in cursor.description or []]
            rows: list[dict[str, Any]] = []
            truncated = False
            max_rows = min(context.max_results, 1_000)
            max_bytes = min(context.max_bytes, 1_048_576)
            for row in cursor:
                if len(rows) >= max_rows:
                    truncated = True
                    break
                candidate = {
                    column: _json_value(row[index])
                    for index, column in enumerate(columns)
                }
                encoded = json.dumps(
                    {"columns": columns, "rows": [*rows, candidate]},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if len(encoded.encode("utf-8")) > max_bytes:
                    truncated = True
                    break
                rows.append(candidate)
            if timed_out:
                return ToolResult(
                    status=(
                        ToolResultStatus.PARTIAL
                        if rows
                        else ToolResultStatus.UNAVAILABLE
                    ),
                    structured_result=(
                        {"columns": columns, "rows": rows} if rows else None
                    ),
                    truncated=bool(rows),
                    warnings=["query_timeout"],
                )
            return ToolResult(
                status=ToolResultStatus.PARTIAL
                if truncated
                else ToolResultStatus.SUCCEEDED,
                structured_result={"columns": columns, "rows": rows},
                truncated=truncated,
                cursor=str(len(rows)) if truncated else None,
                source_references=[
                    SourceReference(source_id="sqlite", locator=f"{path.name}:query")
                ],
            )
        except TimeoutError:
            return ToolResult(
                status=ToolResultStatus.UNAVAILABLE, warnings=["query_timeout"]
            )
        except sqlite3.OperationalError as exc:
            message = str(exc).casefold()
            warning = (
                "query_timeout"
                if "interrupt" in message
                else denied_reason[0]
                if denied_reason
                else "query_error"
            )
            return self._rejected(warning)
        except sqlite3.Error:
            return self._rejected(denied_reason[0] if denied_reason else "query_error")
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _allowed_tables(source_config: dict[str, Any]) -> set[str]:
        allowed: set[str] = set()
        for item in source_config.get("allowed_tables", []):
            if item:
                value = str(item).casefold()
                allowed.add(value)
                allowed.add(value.split(".")[-1])
        return allowed

    def _validate_sql(
        self, sql: str, source_config: dict[str, Any], parameters: Any
    ) -> str | None:
        statements = _split_statements(sql)
        if len(statements) != 1:
            return "multiple_statements_not_allowed"
        statement = statements[0].strip()
        first = re.match(r"([A-Za-z]+)", statement)
        if first and first.group(1).casefold() in _FORBIDDEN_FIRST_WORDS:
            return "read_only_statement_rejected"
        if _FORBIDDEN_TOKENS.search(statement):
            return "read_only_statement_rejected"
        if re.search(r"\bfor\s+(?:update|share)\b", statement, re.IGNORECASE):
            return "locking_query_rejected"
        if re.search(r"(^|[^:])\?(?:\d*)|%s", statement):
            return "named_parameters_required"
        if not isinstance(parameters, dict):
            return "parameters_must_be_object"
        denied_columns = {
            str(item).casefold() for item in source_config.get("denied_columns", [])
        }
        words = {item.casefold() for item in re.findall(r"[A-Za-z_][\w$]*", statement)}
        if denied_columns.intersection(words):
            return "sensitive_column_rejected"
        allowed = self._allowed_tables(source_config)
        if allowed:
            referenced = {
                match[1].strip("\"'`").casefold()
                for match in _TABLE_REFERENCE.findall(statement)
            }
            if any(
                item not in allowed and item.split(".")[-1] not in allowed
                for item in referenced
            ):
                return "table_not_allowed"
        return None

    @staticmethod
    def _quote_identifier(value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    @staticmethod
    def _rejected(code: str) -> ToolResult:
        return ToolResult(
            status=ToolResultStatus.REJECTED,
            structured_result={"error": "read-only database request rejected"},
            warnings=[code],
        )


def _split_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    line_comment = False
    block_comment = False
    index = 0
    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 2
                continue
            index += 1
            continue
        if quote:
            buffer.append(char)
            if char == quote:
                if next_char == quote:
                    buffer.append(next_char)
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char == "-" and next_char == "-":
            line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            index += 2
            continue
        if char in "'\"`":
            quote = char
            buffer.append(char)
        elif char == ";":
            if "".join(buffer).strip():
                statements.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
        index += 1
    if "".join(buffer).strip():
        statements.append("".join(buffer))
    return statements


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {
            "type": "bytes",
            "sha256": hashlib.sha256(value).hexdigest(),
            "bytes": len(value),
        }
    return str(value)


def plugin_factory(instance_config: dict[str, Any] | None = None) -> SQLitePlugin:
    return SQLitePlugin(instance_config)


plugin_factory.__buglens_manifest__ = manifest


__all__ = ["SQLitePlugin", "manifest", "plugin_factory"]
