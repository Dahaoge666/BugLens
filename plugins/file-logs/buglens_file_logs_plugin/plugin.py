"""Read-only text and JSON Lines log searcher with path containment checks."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
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
    plugin_id="file_logs",
    implementation_version="1.0.1",
    api_major=1,
    api_version="1",
    capabilities=["logs", "text", "jsonl", "rotation", "bounded_scan"],
    instance_config_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "root_path": {"type": "string", "minLength": 1},
            "encodings": {"type": "array", "items": {"type": "string"}},
        },
    },
    source_config_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "paths": {"type": "array", "items": {"type": "string"}},
            "glob": {"type": "string", "minLength": 1},
            "timestamp_fields": {"type": "array", "items": {"type": "string"}},
        },
    },
    health_check=True,
)


class FileLogsPlugin:
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
        root = instance_config.get("root_path")
        if not isinstance(root, str) or not root:
            raise ValueError("file log root_path is required")
        root_path = Path(root).expanduser().resolve()
        if not root_path.is_dir():
            raise ValueError("file log root_path must be an existing directory")
        if source_config is not None:
            self._paths(source_config, root_path)

    async def check_health(self) -> PluginHealth:
        return await asyncio.to_thread(self._check_health_sync)

    def _check_health_sync(self) -> PluginHealth:
        root = self.instance_config.get("root_path")
        try:
            if not root or not Path(str(root)).expanduser().resolve().is_dir():
                raise ValueError
            return PluginHealth(status="ok", detail="log directory is readable")
        except Exception:
            return PluginHealth(status="error", detail="log directory is unavailable")

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
        if operation != "search_logs":
            return ToolResult(
                status=ToolResultStatus.REJECTED, warnings=["unsupported_operation"]
            )
        start = _parse_time(request.get("start_time"))
        end = _parse_time(request.get("end_time"))
        if start is None or end is None:
            return ToolResult(
                status=ToolResultStatus.REJECTED, warnings=["absolute_time_required"]
            )
        if end <= start:
            return ToolResult(
                status=ToolResultStatus.REJECTED, warnings=["invalid_time_window"]
            )
        if (end - start).total_seconds() > 24 * 60 * 60:
            return ToolResult(
                status=ToolResultStatus.REJECTED, warnings=["time_window_too_large"]
            )
        try:
            root = (
                Path(str(self.instance_config.get("root_path"))).expanduser().resolve()
            )
            paths = self._paths(source_config, root)
        except ValueError:
            return ToolResult(
                status=ToolResultStatus.REJECTED, warnings=["log_path_rejected"]
            )
        text_query = str(request.get("text_query") or "").casefold()
        levels = {str(item).casefold() for item in request.get("levels", []) if item}
        services = {str(item) for item in request.get("service_ids", []) if item}
        nodes = {str(item) for item in request.get("node_ids", []) if item}
        correlations = {
            str(item) for item in request.get("correlation_ids", []) if item
        }
        start_offset = _safe_offset(request.get("cursor"))
        matched_offset = 0
        records: list[dict[str, Any]] = []
        scanned_files = 0
        scanned_bytes = 0
        truncated = False
        page_break = False
        scan_limit_reached = False
        warnings: list[str] = []
        max_rows = min(context.max_results, 1_000)
        max_bytes = min(context.max_bytes, 1_048_576)
        encodings = self.instance_config.get(
            "encodings", ["utf-8", "utf-8-sig", "latin-1"]
        )
        for path in paths:
            if scanned_files >= context.max_scan_files:
                truncated = True
                page_break = True
                scan_limit_reached = True
                break
            scanned_files += 1
            try:
                stream = path.open("rb")
            except OSError:
                continue
            with stream:
                line_number = 0
                while True:
                    if context.remaining_seconds <= 0:
                        return ToolResult(
                            status=(
                                ToolResultStatus.PARTIAL
                                if records
                                else ToolResultStatus.UNAVAILABLE
                            ),
                            structured_result=(
                                {"entries": records} if records else None
                            ),
                            truncated=bool(records),
                            cursor=str(matched_offset) if records else None,
                            warnings=["log_scan_timeout"],
                        )
                    remaining = context.max_scan_bytes - scanned_bytes
                    if remaining <= 0:
                        truncated = True
                        page_break = True
                        scan_limit_reached = True
                        break
                    read_size = min(remaining, 1_048_577)
                    try:
                        raw_line = stream.readline(read_size)
                    except OSError:
                        break
                    if not raw_line:
                        break
                    scanned_bytes += len(raw_line)
                    line_number += 1
                    line_complete = raw_line.endswith((b"\n", b"\r"))
                    if len(raw_line) >= remaining and not line_complete:
                        truncated = True
                        page_break = True
                        scan_limit_reached = True
                        break
                    if len(raw_line) > 1_048_576:
                        if "oversized_line_skipped" not in warnings:
                            warnings.append("oversized_line_skipped")
                        truncated = True
                        while raw_line and not line_complete:
                            remaining = context.max_scan_bytes - scanned_bytes
                            if remaining <= 0:
                                page_break = True
                                scan_limit_reached = True
                                break
                            raw_line = stream.readline(min(remaining, 65_536))
                            scanned_bytes += len(raw_line)
                            line_complete = raw_line.endswith((b"\n", b"\r"))
                        continue
                    line = _decode(raw_line, encodings).rstrip("\r\n")
                    record = _parse_line(
                        line,
                        path,
                        root,
                        line_number,
                        timestamp_fields=source_config.get("timestamp_fields"),
                    )
                    if record is None or not _matches(
                        record,
                        start=start,
                        end=end,
                        text_query=text_query,
                        levels=levels,
                        services=services,
                        nodes=nodes,
                        correlations=correlations,
                    ):
                        continue
                    if matched_offset < start_offset:
                        matched_offset += 1
                        continue
                    if len(records) >= max_rows:
                        truncated = True
                        page_break = True
                        if "result_limit_reached" not in warnings:
                            warnings.append("result_limit_reached")
                        break
                    candidate = {"entries": [*records, record]}
                    candidate_bytes = len(
                        json.dumps(
                            candidate,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    )
                    if candidate_bytes > max_bytes:
                        truncated = True
                        if records:
                            page_break = True
                            if "result_limit_reached" not in warnings:
                                warnings.append("result_limit_reached")
                            break
                        # A single matching entry must not create an endless
                        # cursor that returns the same entry forever.  It is
                        # explicitly reported as omitted and scanning can
                        # continue to later entries.
                        matched_offset += 1
                        if "entry_too_large_skipped" not in warnings:
                            warnings.append("entry_too_large_skipped")
                        continue
                    records.append(record)
                    matched_offset += 1
            if truncated:
                if not page_break:
                    continue
                break
        if scan_limit_reached and "scan_limit_reached" not in warnings:
            warnings.append("scan_limit_reached")
        return ToolResult(
            status=ToolResultStatus.PARTIAL
            if truncated
            else ToolResultStatus.SUCCEEDED,
            structured_result={"entries": records},
            truncated=truncated,
            cursor=str(matched_offset) if page_break else None,
            source_references=[
                SourceReference(
                    source_id="file_logs",
                    locator=f"{scanned_files} files/{scanned_bytes} bytes",
                )
            ],
            warnings=warnings,
        )

    def _paths(self, source_config: dict[str, Any], root: Path) -> list[Path]:
        values: list[str] = []
        if isinstance(source_config.get("paths"), list):
            values.extend(str(item) for item in source_config["paths"])
        if source_config.get("path"):
            values.append(str(source_config["path"]))
        if source_config.get("glob"):
            values.append(str(source_config["glob"]))
        if not values:
            raise ValueError("log source path is required")
        result: dict[str, Path] = {}
        for value in values:
            if not value or "\x00" in value:
                raise ValueError("invalid log path")
            candidate = Path(value)
            if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
                raise ValueError("log path must stay inside root_path")
            if any(char in value for char in "*?["):
                matches = list(root.glob(value))
            else:
                # A configured base name includes rotated siblings even when
                # the active file has already been rotated away.
                base = root / value
                matches = [base] if base.is_file() else []
                matches.extend(sorted(root.glob(f"{value}.*")))
            for match in matches:
                resolved = match.resolve()
                try:
                    resolved.relative_to(root)
                except ValueError as exc:
                    raise ValueError("log symlink escapes root_path") from exc
                if resolved.is_file():
                    result[str(resolved)] = resolved
        return sorted(result.values(), key=lambda item: str(item))

    def close(self) -> None:
        return None


def _parse_line(
    line: str,
    path: Path,
    root: Path,
    line_number: int,
    *,
    timestamp_fields: Any = None,
) -> dict[str, Any] | None:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        value = json.loads(stripped)
    except (TypeError, ValueError):
        value = {"message": stripped}
    if not isinstance(value, dict):
        value = {"message": str(value)}
    fields = timestamp_fields if isinstance(timestamp_fields, list) else []
    timestamp_candidates = [_field_value(value, str(field)) for field in fields]
    timestamp_candidates.extend(
        [
            value.get("timestamp"),
            value.get("time"),
            value.get("@timestamp"),
            value.get("ts"),
        ]
    )
    timestamp = next(
        (
            parsed
            for candidate in timestamp_candidates
            if (parsed := _parse_time(candidate))
        ),
        None,
    )
    if timestamp is None:
        # Common plain-text format: an ISO-8601 timestamp at the beginning of
        # the line followed by level/message text.
        match = re.search(
            r"(?<!\d)\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})(?!\d)",
            stripped,
        )
        if match:
            timestamp = _parse_time(match.group(0))
    message = value.get("message") or value.get("msg") or stripped
    service_id = (
        value.get("service_id") or value.get("service.name") or value.get("service")
    )
    node_id = value.get("node_id") or value.get("host.name") or value.get("node")
    level = value.get("level") or value.get("severity")
    correlation = (
        value.get("correlation_id")
        or value.get("trace_id")
        or value.get("traceId")
        or value.get("request_id")
    )
    relative = path.relative_to(root).as_posix()
    return {
        "timestamp": timestamp.isoformat() if timestamp else None,
        "level": str(level) if level is not None else None,
        "message": str(message),
        "service_id": str(service_id) if service_id is not None else None,
        "node_id": str(node_id) if node_id is not None else None,
        "correlation_id": str(correlation) if correlation is not None else None,
        "reference": f"{relative}:{line_number}",
    }


def _field_value(value: dict[str, Any], field: str) -> Any:
    current: Any = value
    for part in field.split("."):
        if not part or not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _matches(
    record: dict[str, Any],
    *,
    start: datetime,
    end: datetime,
    text_query: str,
    levels: set[str],
    services: set[str],
    nodes: set[str],
    correlations: set[str],
) -> bool:
    timestamp = _parse_time(record.get("timestamp"))
    if timestamp is None or timestamp < start or timestamp > end:
        return False
    if levels and str(record.get("level") or "").casefold() not in levels:
        return False
    if services and record.get("service_id") not in services:
        return False
    if nodes and record.get("node_id") not in nodes:
        return False
    if correlations and record.get("correlation_id") not in correlations:
        return False
    if (
        text_query
        and text_query not in json.dumps(record, ensure_ascii=False).casefold()
    ):
        return False
    return True


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _decode(raw: bytes, encodings: Any) -> str:
    choices = (
        encodings if isinstance(encodings, list) else ["utf-8", "utf-8-sig", "latin-1"]
    )
    for encoding in choices:
        try:
            return raw.decode(str(encoding))
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _safe_offset(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def plugin_factory(instance_config: dict[str, Any] | None = None) -> FileLogsPlugin:
    return FileLogsPlugin(instance_config)


plugin_factory.__buglens_manifest__ = manifest


__all__ = ["FileLogsPlugin", "manifest", "plugin_factory"]
