"""SSH diagnostics use a fixed command catalog, never a model shell command."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from buglens_plugin_api import (
    ExecutionContext,
    PluginHealth,
    PluginManifest,
    SourceReference,
    ToolResult,
    ToolResultStatus,
)

from .connection import SSH_PROPERTIES, connect_ssh, validate_connection

CHECKS = {
    "system": "uname -a",
    "uptime": "uptime",
    "disk": "df -Pk",
    "memory": "cat /proc/meminfo",
    "processes": "ps -eo pid,ppid,comm,pcpu,pmem",
}
manifest = PluginManifest(
    plugin_id="ssh",
    implementation_version="1.0.0",
    display_name="SSH 主机诊断",
    category="host",
    description="通过固定只读检查读取 Linux 主机系统、负载、磁盘、内存与进程信息。",
    capabilities=["host", "host.inspect.v1"],
    instance_config_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["host"],
        "properties": SSH_PROPERTIES,
    },
    source_config_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "checks": {
                "type": "array",
                "title": "允许的主机检查",
                "description": "通常保持默认；系统、负载、磁盘和内存均为只读检查。",
                "minItems": 1,
                "maxItems": 5,
                "uniqueItems": True,
                "items": {"type": "string", "enum": list(CHECKS)},
                "default": ["system", "uptime", "disk", "memory"],
            },
        },
    },
)


class SSHPlugin:
    manifest = manifest

    def __init__(self, instance_config: dict[str, Any] | None = None):
        self.instance_config = dict(instance_config or {})

    def validate_config(self, instance_config, source_config=None):
        validate_connection(instance_config)
        if source_config is not None and (
            not source_config.get("checks", list(CHECKS))
            or set(source_config.get("checks", list(CHECKS))) - CHECKS.keys()
        ):
            raise ValueError("unsupported SSH host check")

    async def check_health(self):
        def check():
            try:
                client = connect_ssh(self.instance_config, 5)
                client.close()
                return PluginHealth(status="ok", detail="SSH host is reachable")
            except Exception:
                return PluginHealth(
                    status="error",
                    detail="SSH host is unavailable; check credentials and known_hosts",
                )

        return await asyncio.to_thread(check)

    async def execute(
        self, operation, source_config, request, context: ExecutionContext
    ):
        return await asyncio.to_thread(
            self._execute_sync, operation, source_config, request, context
        )

    def _execute_sync(self, operation, source_config, request, context):
        check = request.get("check", "system")
        if (
            operation != "inspect_host"
            or not isinstance(check, str)
            or check not in CHECKS
            or check not in source_config.get("checks", list(CHECKS))
            or set(request) - {"check", "max_results", "max_bytes"}
        ):
            return ToolResult(
                status=ToolResultStatus.REJECTED, warnings=["host_check_not_allowed"]
            )
        if context.remaining_seconds <= 0:
            return ToolResult(
                status=ToolResultStatus.UNAVAILABLE, warnings=["ssh_timeout"]
            )
        client = None
        try:
            client = connect_ssh(
                self.instance_config, min(5, context.remaining_seconds)
            )
            stdin, stdout, stderr = client.exec_command(
                CHECKS[check], timeout=context.remaining_seconds, get_pty=False
            )
            stdin.close()
            channel = stdout.channel
            try:
                raw = b""
                channel.set_combine_stderr(True)
                while len(raw) <= context.max_bytes and context.remaining_seconds > 0:
                    if not channel.recv_ready():
                        if channel.exit_status_ready() or channel.closed:
                            break
                        time.sleep(min(0.01, context.remaining_seconds))
                        continue
                    chunk = channel.recv(min(65_536, context.max_bytes + 1 - len(raw)))
                    if not chunk:
                        break
                    raw += chunk
                truncated = (
                    len(raw) > context.max_bytes or context.remaining_seconds <= 0
                )
                lines = (
                    raw[: context.max_bytes]
                    .decode("utf-8", errors="replace")
                    .splitlines()
                )
                if len(lines) > context.max_results:
                    truncated = True
                    lines = lines[: context.max_results]
                while (
                    lines
                    and len(
                        json.dumps(
                            {"check": check, "lines": lines}, ensure_ascii=False
                        ).encode()
                    )
                    > context.max_bytes
                ):
                    truncated = True
                    lines.pop()
                if not truncated and (
                    not channel.exit_status_ready() or channel.recv_exit_status() != 0
                ):
                    return ToolResult(
                        status=ToolResultStatus.UNAVAILABLE,
                        warnings=["host_check_unavailable"],
                    )
                return ToolResult(
                    status=ToolResultStatus.PARTIAL
                    if truncated
                    else ToolResultStatus.SUCCEEDED,
                    structured_result={"check": check, "lines": lines},
                    truncated=truncated,
                    warnings=["result_limit_reached"] if truncated else [],
                    source_references=[
                        SourceReference(source_id="ssh", locator=f"host-check:{check}")
                    ],
                )
            finally:
                channel.close()
                stdout.close()
                stderr.close()
        except Exception:
            return ToolResult(
                status=ToolResultStatus.UNAVAILABLE, warnings=["ssh_unavailable"]
            )
        finally:
            if client is not None:
                client.close()

    def close(self):
        pass


def plugin_factory(instance_config=None):
    return SSHPlugin(instance_config)


plugin_factory.__buglens_manifest__ = manifest
