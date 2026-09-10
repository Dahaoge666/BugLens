"""Controlled connector transports.

All transports implement the same small async boundary.  They are deliberately
not exposed to the Agent directly: :mod:`app.plugins` and the capability
gateway apply target authorization, budgets, result bounds, Evidence and audit
before/after invoking one of these transports.

The process transports use a JSON-over-stdio protocol.  SSH is a process
transport whose process happens to be the local ``ssh`` client; the remote
command is fixed by configuration and never supplied by the model.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from buglens_plugin_api import (
    ExecutionContext,
    PluginHealth,
    ToolResult,
    ToolResultStatus,
)


class TransportError(RuntimeError):
    """A transport failed without exposing process/connector details."""


class TransportTimeout(TransportError):
    """A transport exceeded the deadline supplied by the core."""


class ConnectorTransport(Protocol):
    kind: str

    async def execute(
        self,
        operation: str,
        source_config: dict[str, Any],
        request: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult: ...

    async def check_health(self) -> PluginHealth: ...

    def close(self) -> None: ...


class DriverTransport:
    """Adapt an existing BugLens ``ToolPlugin`` to the transport boundary."""

    kind = "driver"

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    async def execute(
        self,
        operation: str,
        source_config: dict[str, Any],
        request: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        executor = getattr(self.plugin, "execute", None)
        if not callable(executor):
            raise TransportError("driver does not implement execute")
        result = executor(operation, source_config, request, context)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ToolResult):
            return result
        return ToolResult.model_validate(result)

    async def check_health(self) -> PluginHealth:
        checker = getattr(self.plugin, "check_health", None)
        if not callable(checker):
            return PluginHealth(status="degraded", detail="health check unavailable")
        result = checker()
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, PluginHealth):
            return result
        return PluginHealth.model_validate(result)

    def close(self) -> None:
        closer = getattr(self.plugin, "close", None)
        if callable(closer):
            closer()


@dataclass(frozen=True)
class ProcessTransportConfig:
    """Fixed executable configuration for CLI and remote probe processes."""

    command: str
    args: tuple[str, ...] = ()
    max_output_bytes: int = 1_114_112
    health_operation: str | None = "__health__"

    def __post_init__(self) -> None:
        command = str(self.command).strip()
        # Paths may contain spaces on Windows.  A command containing whitespace
        # is still safe when passed as one argv item, so only reject an
        # embedded NUL; shell parsing is never used.
        if not command or "\x00" in command:
            raise ValueError("transport command is invalid")
        if len(self.args) > 64 or any("\x00" in str(item) for item in self.args):
            raise ValueError("transport arguments are invalid")
        if self.max_output_bytes < 1_024 or self.max_output_bytes > 8 * 1024 * 1024:
            raise ValueError("transport output limit is invalid")


class JsonProcessTransport:
    """Invoke a fixed JSON-over-stdio connector without a shell."""

    kind = "cli"
    protocol = "buglens-tool/v1"

    def __init__(self, config: ProcessTransportConfig) -> None:
        self.config = config
        self._processes: set[asyncio.subprocess.Process] = set()

    def _argv(self) -> list[str]:
        return [self.config.command, *self.config.args]

    async def execute(
        self,
        operation: str,
        source_config: dict[str, Any],
        request: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        payload = {
            "protocol": self.protocol,
            "operation": operation,
            "source_config": source_config,
            "request": request,
            "context": context.model_dump(mode="json"),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > context.max_bytes * 4:
            return ToolResult(
                status=ToolResultStatus.REJECTED,
                warnings=["transport_request_too_large"],
            )
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *self._argv(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._environment(),
            )
            self._processes.add(process)
            try:
                stdout, _stderr = await asyncio.wait_for(
                    process.communicate(encoded),
                    timeout=max(0.1, context.remaining_seconds),
                )
            except asyncio.TimeoutError as exc:
                await self._terminate(process)
                raise TransportTimeout("connector process timed out") from exc
            if len(stdout) > self.config.max_output_bytes:
                await self._terminate(process)
                return ToolResult(
                    status=ToolResultStatus.PARTIAL,
                    truncated=True,
                    warnings=["transport_output_limit"],
                )
            if process.returncode not in (0, None):
                return ToolResult(
                    status=ToolResultStatus.UNAVAILABLE,
                    warnings=[f"transport_exit_{process.returncode}"],
                )
            try:
                result = json.loads(stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return ToolResult(
                    status=ToolResultStatus.UNAVAILABLE,
                    warnings=["transport_output_invalid"],
                )
            if (
                isinstance(result, dict)
                and "result" in result
                and "status" not in result
            ):
                result = result["result"]
            try:
                return ToolResult.model_validate(result)
            except (TypeError, ValueError):
                return ToolResult(
                    status=ToolResultStatus.UNAVAILABLE,
                    warnings=["transport_result_invalid"],
                )
        except FileNotFoundError as exc:
            raise TransportError("connector executable unavailable") from exc
        except OSError as exc:
            raise TransportError("connector process failed to start") from exc
        finally:
            if process is not None:
                self._processes.discard(process)

    def _environment(self) -> dict[str, str]:
        # The subprocess receives a minimal environment.  Connector-specific
        # secrets must be configured through the connector itself, never by
        # interpolating model arguments into a shell command.
        allowed = {"PATH", "SystemRoot", "TEMP", "TMP", "LANG", "LC_ALL"}
        return {key: value for key, value in os.environ.items() if key in allowed}

    async def check_health(self) -> PluginHealth:
        if not self.config.health_operation:
            return PluginHealth(status="degraded", detail="health check unavailable")
        context = ExecutionContext(
            execution_id="health",
            run_id="health",
            environment_snapshot_id="health",
            plugin_instance_id="health",
            deadline=datetime.now(UTC) + timedelta(seconds=5),
            max_results=1,
            max_bytes=4_096,
            max_scan_files=1,
            max_scan_bytes=4_096,
        )
        try:
            result = await self.execute(self.config.health_operation, {}, {}, context)
        except TransportError:
            return PluginHealth(status="error", detail="connector health check failed")
        if result.status in {ToolResultStatus.SUCCEEDED, ToolResultStatus.PARTIAL}:
            return PluginHealth(status="ok", detail="connector is available")
        return PluginHealth(status="error", detail="connector health check failed")

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.kill()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass

    def close(self) -> None:
        # ``Process.kill`` is synchronous; the event loop will reap the child
        # on its next turn.  This method is intentionally safe at shutdown.
        for process in list(self._processes):
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        self._processes.clear()


class CliTransport(JsonProcessTransport):
    """Run a preconfigured local connector executable."""

    kind = "cli"

    def __init__(
        self,
        command: str | ProcessTransportConfig,
        args: list[str] | tuple[str, ...] = (),
        *,
        max_output_bytes: int = 1_114_112,
    ) -> None:
        config = (
            command
            if isinstance(command, ProcessTransportConfig)
            else ProcessTransportConfig(
                command=command,
                args=tuple(args),
                max_output_bytes=max_output_bytes,
            )
        )
        super().__init__(config)


@dataclass(frozen=True)
class SshTransportConfig:
    host: str
    command: str
    user: str | None = None
    port: int = 22
    known_hosts: str | None = None
    identity_file: str | None = None
    ssh_command: str = "ssh"

    def __post_init__(self) -> None:
        for label, value in (("host", self.host), ("command", self.command)):
            if not str(value).strip() or "\x00" in str(value):
                raise ValueError(f"ssh {label} is invalid")
        if any(ch.isspace() for ch in str(self.host)):
            raise ValueError("ssh host must be a single hostname")
        if self.user is not None and (
            not self.user.strip() or any(ch.isspace() for ch in self.user)
        ):
            raise ValueError("ssh user is invalid")
        if not 1 <= self.port <= 65_535:
            raise ValueError("ssh port is invalid")


class SshTransport(JsonProcessTransport):
    """Execute the fixed remote BugLens probe over the local SSH client."""

    kind = "ssh"

    def __init__(
        self,
        config: SshTransportConfig | str,
        *,
        command: str | None = None,
        user: str | None = None,
        port: int = 22,
        known_hosts: str | None = None,
        identity_file: str | None = None,
        ssh_command: str = "ssh",
        max_output_bytes: int = 1_114_112,
    ) -> None:
        if isinstance(config, str):
            if command is None:
                raise ValueError("ssh command is required")
            config = SshTransportConfig(
                host=config,
                command=command,
                user=user,
                port=port,
                known_hosts=known_hosts,
                identity_file=identity_file,
                ssh_command=ssh_command,
            )
        destination = f"{config.user}@{config.host}" if config.user else config.host
        ssh_args = [
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-p",
            str(config.port),
        ]
        if config.known_hosts:
            ssh_args.extend(["-o", f"UserKnownHostsFile={config.known_hosts}"])
        if config.identity_file:
            ssh_args.extend(["-i", config.identity_file])
        # ``command`` is one fixed remote executable string.  It is passed as
        # one argv item, never assembled from model input.
        ssh_args.extend([destination, config.command])
        super().__init__(
            ProcessTransportConfig(
                command=config.ssh_command,
                args=tuple(ssh_args),
                max_output_bytes=max_output_bytes,
            )
        )


class McpTransport:
    """Call an allowlisted MCP tool and normalize its result.

    The transport uses the Agents SDK MCP clients when available, but does not
    attach the server directly to an Agent.  This keeps all calls behind
    BugLens' target, budget, audit and Evidence pipeline.
    """

    kind = "mcp"

    def __init__(
        self,
        *,
        url: str | None = None,
        command: str | None = None,
        args: list[str] | tuple[str, ...] = (),
        headers: dict[str, str] | None = None,
        tool_map: dict[str, str] | None = None,
        timeout_seconds: float = 10.0,
        credentials: dict[str, str | None] | None = None,
    ) -> None:
        if not url and not command:
            raise ValueError("MCP transport requires url or command")
        if url and command:
            raise ValueError("MCP transport accepts either url or command")
        if url and any(ch.isspace() for ch in url):
            raise ValueError("MCP URL is invalid")
        self.url = url
        self.command = command
        self.args = tuple(str(item) for item in args)
        self.headers = dict(headers or {})
        credential_values = credentials or {}
        token = credential_values.get("token")
        if token and "authorization" not in {
            str(key).casefold() for key in self.headers
        }:
            self.headers["Authorization"] = f"Bearer {token}"
        self.tool_map = dict(tool_map or {})
        self.timeout_seconds = max(0.1, min(float(timeout_seconds), 120.0))
        self._server: Any | None = None
        self._server_lock = asyncio.Lock()
        self._tool_names: set[str] | None = None

    async def _ensure_server(self) -> Any:
        async with self._server_lock:
            if self._server is not None:
                return self._server
            try:
                from agents.mcp import (
                    MCPServerStdio,
                    MCPServerStreamableHttp,
                )
            except ImportError as exc:  # pragma: no cover - dependency is pinned
                raise TransportError("MCP transport is unavailable") from exc
            if self.url:
                server = MCPServerStreamableHttp(
                    params={
                        "url": self.url,
                        "headers": self.headers,
                        "timeout": self.timeout_seconds,
                    },
                    cache_tools_list=True,
                    name="BugLens MCP connector",
                )
            else:
                if not self.command:
                    raise TransportError("MCP command is missing")
                server = MCPServerStdio(
                    params={
                        "command": self.command,
                        "args": list(self.args),
                    },
                    cache_tools_list=True,
                    name="BugLens MCP stdio connector",
                )
            try:
                await server.connect()
                tools = await server.list_tools()
            except Exception as exc:
                try:
                    await server.cleanup()
                except Exception:
                    pass
                raise TransportError("MCP connector unavailable") from exc
            self._server = server
            self._tool_names = {
                str(getattr(tool, "name", ""))
                for tool in tools
                if getattr(tool, "name", None)
            }
            return server

    def _mapped_tool(self, operation: str) -> str:
        return self.tool_map.get(operation, operation)

    async def execute(
        self,
        operation: str,
        source_config: dict[str, Any],
        request: dict[str, Any],
        context: ExecutionContext,
    ) -> ToolResult:
        server = await self._ensure_server()
        tool_name = self._mapped_tool(operation)
        if self._tool_names is not None and tool_name not in self._tool_names:
            return ToolResult(
                status=ToolResultStatus.REJECTED,
                warnings=["mcp_tool_not_allowlisted"],
            )
        # MCP tool arguments intentionally contain only the normalized request.
        # Source-specific selectors should be bound to the configured MCP
        # instance/tool mapping; never let an MCP server reinterpret a model
        # supplied connection location as authority.
        del source_config
        try:
            result = await asyncio.wait_for(
                server.call_tool(tool_name, request),
                timeout=max(
                    0.1,
                    min(context.remaining_seconds, self.timeout_seconds),
                ),
            )
        except asyncio.TimeoutError as exc:
            raise TransportTimeout("MCP connector timed out") from exc
        except Exception as exc:
            raise TransportError("MCP connector call failed") from exc
        return self._result_from_mcp(result)

    @staticmethod
    def _result_from_mcp(value: Any) -> ToolResult:
        def field(name: str, default: Any = None) -> Any:
            if isinstance(value, dict):
                return value.get(name, default)
            return getattr(value, name, default)

        if bool(field("isError", False)) or bool(field("is_error", False)):
            return ToolResult(
                status=ToolResultStatus.UNAVAILABLE,
                warnings=["mcp_tool_failed"],
            )
        structured = field("structuredContent")
        if structured is None:
            structured = field("structured_content")
        if structured is None:
            content = field("content") or []
            pieces: list[Any] = []
            for item in content:
                text = (
                    item.get("text")
                    if isinstance(item, dict)
                    else getattr(item, "text", None)
                )
                if text is not None:
                    try:
                        pieces.append(json.loads(text))
                    except (TypeError, ValueError):
                        pieces.append(str(text))
                elif isinstance(item, dict):
                    pieces.append(item)
            if len(pieces) == 1:
                structured = pieces[0]
            elif pieces:
                structured = {"content": pieces}
        if isinstance(structured, dict) and structured.get("status") in {
            status.value for status in ToolResultStatus
        }:
            try:
                return ToolResult.model_validate(structured)
            except (TypeError, ValueError):
                return ToolResult(
                    status=ToolResultStatus.UNAVAILABLE,
                    warnings=["mcp_result_invalid"],
                )
        return ToolResult(status=ToolResultStatus.SUCCEEDED, result=structured)

    async def check_health(self) -> PluginHealth:
        try:
            await self._ensure_server()
        except TransportError:
            return PluginHealth(status="error", detail="MCP connector is unavailable")
        return PluginHealth(status="ok", detail="MCP connector is available")

    def close(self) -> None:
        server = self._server
        self._server = None
        self._tool_names = None
        if server is None:
            return
        # Cleanup is async in the SDK.  Schedule it when a loop is running;
        # otherwise the next process teardown will close the stdio child.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def cleanup() -> None:
            try:
                await server.cleanup()
            except Exception:
                # Shutdown must not turn a completed diagnosis into an
                # unhandled task exception when a connector already exited.
                pass

        loop.create_task(cleanup())


def build_transport(
    config: Any,
    *,
    credentials: dict[str, str | None] | None = None,
) -> ConnectorTransport:
    """Construct a transport from an ``app.environment.TransportConfig``."""

    if isinstance(config, dict):
        get = config.get
        kind = str(get("type", "driver") or "driver").lower()
        payload = dict(get("config", {}) or {})
    else:

        def get(name: str, default: Any = None) -> Any:
            return getattr(config, name, default)

        kind = str(get("type", "driver") or "driver").lower()
        payload = dict(get("config", {}) or {})
    if kind == "cli":
        return CliTransport(
            ProcessTransportConfig(
                command=str(get("command") or payload.get("command", "")),
                args=tuple(get("args") or payload.get("args", []) or ()),
                max_output_bytes=int(
                    get("max_output_bytes")
                    or payload.get("max_output_bytes", 1_114_112)
                ),
            )
        )
    if kind == "ssh":
        return SshTransport(
            SshTransportConfig(
                host=str(get("host") or payload.get("host", "")),
                command=str(get("remote_command") or payload.get("remote_command", "")),
                user=get("user") or payload.get("user"),
                port=int(get("port") or payload.get("port", 22)),
                known_hosts=get("known_hosts") or payload.get("known_hosts"),
                identity_file=get("identity_file") or payload.get("identity_file"),
                ssh_command=str(
                    get("ssh_command")
                    or payload.get("ssh_command")
                    or get("command")
                    or "ssh"
                ),
            )
        )
    if kind == "mcp":
        return McpTransport(
            url=get("url") or payload.get("url"),
            command=get("command") or payload.get("command"),
            args=get("args") or payload.get("args", []),
            headers=get("headers") or payload.get("headers", {}),
            tool_map=get("tool_map") or payload.get("tool_map", {}),
            timeout_seconds=float(
                get("timeout_seconds") or payload.get("timeout_seconds", 10)
            ),
            credentials=credentials,
        )
    if kind == "driver":
        raise ValueError("driver transport must be created from a plugin instance")
    raise ValueError(f"unsupported transport type: {kind}")


__all__ = [
    "CliTransport",
    "ConnectorTransport",
    "DriverTransport",
    "JsonProcessTransport",
    "McpTransport",
    "ProcessTransportConfig",
    "SshTransport",
    "SshTransportConfig",
    "TransportError",
    "TransportTimeout",
    "build_transport",
]
