"""Search remote files via read-only SFTP; no remote shell or download cache."""

from __future__ import annotations

import asyncio
import stat
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Any

from buglens_file_logs_plugin import FileLogsPlugin
from buglens_file_logs_plugin.plugin import manifest as logs_manifest
from buglens_plugin_api import (
    ExecutionContext,
    PluginHealth,
    PluginManifest,
    SourceReference,
    ToolResult,
    ToolResultStatus,
)
from buglens_ssh_plugin.connection import (
    SSH_PROPERTIES,
    connect_ssh,
    validate_connection,
)

manifest = PluginManifest(
    plugin_id="ssh_logs",
    implementation_version="1.0.0",
    display_name="SSH 日志检索",
    category="logs",
    description="直接通过只读 SFTP 检索远端日志，无需安装远端探针。",
    capabilities=["logs", "logs.search.v1"],
    instance_config_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["host", "root_path"],
        "properties": {
            **SSH_PROPERTIES,
            "root_path": {
                "type": "string",
                "title": "远程日志目录",
                "description": "SSH 主机上的绝对目录，例如 /var/log/orders。",
                "examples": ["/var/log/orders"],
                "minLength": 1,
                "maxLength": 4096,
            },
            "encodings": logs_manifest.instance_config_schema["properties"][
                "encodings"
            ],
        },
    },
    source_config_schema=logs_manifest.source_config_schema,
)


def selectors(config):
    values = list(config.get("paths", []))
    values.extend(config[key] for key in ("path", "glob") if config.get(key))
    if not values or len(values) > 50:
        raise ValueError("log path is required")
    for value in values:
        path = PurePosixPath(value)
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or "\\" in value
            or path.is_absolute()
            or not path.name
            or ".." in path.parts
            or "**" in value
            or any(char in str(path.parent) for char in "*?[")
            or len(path.parts) > 16
        ):
            raise ValueError(
                "remote log paths must be relative; wildcards are allowed in filenames only"
            )
    return values


def checked_path(sftp, root: PurePosixPath, relative: str):
    current = root
    for part in PurePosixPath(relative).parts:
        current /= part
        attributes = sftp.lstat(str(current))
        if stat.S_ISLNK(attributes.st_mode):
            raise ValueError("remote log symlinks are not allowed")
    normalized = PurePosixPath(sftp.normalize(str(current)))
    if not normalized.is_relative_to(root):
        raise ValueError("remote log path escapes root")
    return normalized


class RemoteReadError(RuntimeError):
    """Do not let the local scanner interpret failed remote reads as EOF."""


class RemoteStream:
    def __init__(self, file, sftp, context, budget):
        self.file, self.sftp, self.context, self.budget = file, sftp, context, budget
        self.buffer = b""

    def readline(self, size):
        while b"\n" not in self.buffer and len(self.buffer) < size:
            if self.context.remaining_seconds <= 0:
                raise RemoteReadError("remote log deadline exceeded")
            remaining = self.context.max_scan_bytes - self.budget["bytes"]
            if remaining <= 0:
                break
            self.sftp.get_channel().settimeout(self.context.remaining_seconds)
            try:
                chunk = self.file.read(min(4096, size - len(self.buffer), remaining))
            except OSError as exc:
                raise RemoteReadError("remote log read failed") from exc
            self.budget["bytes"] += len(chunk)
            if not chunk:
                break
            self.buffer += chunk
        end = self.buffer.find(b"\n", 0, size)
        end = end + 1 if end >= 0 else min(size, len(self.buffer))
        line, self.buffer = self.buffer[:end], self.buffer[end:]
        return line

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.file.close()


class RemotePath:
    def __init__(self, path, root, sftp, context, budget):
        self.path, self.root, self.sftp, self.context, self.budget = (
            path,
            root,
            sftp,
            context,
            budget,
        )

    def open(self, mode):
        if mode != "rb":
            raise ValueError("read-only SFTP")
        self.sftp.get_channel().settimeout(max(0.1, self.context.remaining_seconds))
        try:
            checked_path(self.sftp, self.root, str(self.path.relative_to(self.root)))
            file = self.sftp.open(str(self.path), "rb")
        except OSError as exc:
            raise RemoteReadError("remote log open failed") from exc
        return RemoteStream(file, self.sftp, self.context, self.budget)

    def relative_to(self, root):
        return self.path.relative_to(root)


class SSHLogsPlugin:
    manifest = manifest

    def __init__(self, instance_config: dict[str, Any] | None = None):
        self.instance_config = dict(instance_config or {})

    def validate_config(self, instance_config, source_config=None):
        validate_connection(instance_config)
        root = instance_config.get("root_path")
        if (
            not isinstance(root, str)
            or not root.startswith("/")
            or "\x00" in root
            or ".." in PurePosixPath(root).parts
        ):
            raise ValueError("remote log directory must be absolute")
        if source_config is not None:
            selectors(source_config)

    async def check_health(self):
        def check():
            client = sftp = None
            try:
                client = connect_ssh(self.instance_config, 5)
                sftp = client.open_sftp()
                sftp.get_channel().settimeout(5)
                if not stat.S_ISDIR(
                    sftp.stat(self.instance_config["root_path"]).st_mode
                ):
                    raise ValueError("not a directory")
                next(
                    iter(
                        sftp.listdir_iter(
                            self.instance_config["root_path"], read_aheads=1
                        )
                    ),
                    None,
                )
                return PluginHealth(status="ok", detail="SSH log directory is readable")
            except Exception:
                return PluginHealth(
                    status="error",
                    detail="SSH log directory is unavailable; check credentials, known_hosts and directory permissions",
                )
            finally:
                if sftp is not None:
                    sftp.close()
                if client is not None:
                    client.close()

        return await asyncio.to_thread(check)

    async def execute(
        self, operation, source_config, request, context: ExecutionContext
    ):
        return await asyncio.to_thread(
            self._execute_sync, operation, source_config, request, context
        )

    def _execute_sync(self, operation, source_config, request, context):
        if operation != "search_logs":
            return ToolResult(
                status=ToolResultStatus.REJECTED, warnings=["unsupported_operation"]
            )
        try:
            patterns = selectors(source_config)
        except (ValueError, TypeError):
            return ToolResult(
                status=ToolResultStatus.REJECTED, warnings=["log_path_rejected"]
            )
        scanner = FileLogsPlugin(
            {
                "encodings": self.instance_config.get(
                    "encodings", ["utf-8", "utf-8-sig", "latin-1"]
                )
            }
        )
        validation = scanner.scan_paths(
            source_config, request, context, root=PurePosixPath("/"), paths=[]
        )
        if validation.status == ToolResultStatus.REJECTED:
            return validation
        client = sftp = None
        budget = {"bytes": 0}
        listing_truncated = False
        try:
            if context.remaining_seconds <= 0:
                raise TimeoutError
            client = connect_ssh(
                self.instance_config, min(5, context.remaining_seconds)
            )
            sftp = client.open_sftp()
            sftp.get_channel().settimeout(context.remaining_seconds)
            root = PurePosixPath(sftp.normalize(self.instance_config["root_path"]))
            paths = {}
            visited = 0
            for pattern in patterns:
                sftp.get_channel().settimeout(max(0.1, context.remaining_seconds))
                relative = PurePosixPath(pattern)
                directory = checked_path(sftp, root, str(relative.parent))
                for entry in sftp.listdir_iter(str(directory), read_aheads=1):
                    sftp.get_channel().settimeout(max(0.1, context.remaining_seconds))
                    visited += 1
                    if (
                        visited > max(100, context.max_scan_files * 20)
                        or context.remaining_seconds <= 0
                    ):
                        listing_truncated = True
                        break
                    name = entry.filename
                    if (
                        "/" in name
                        or "\\" in name
                        or name in {".", ".."}
                        or not stat.S_ISREG(entry.st_mode)
                    ):
                        continue
                    matches = fnmatchcase(name, relative.name) or (
                        not any(char in relative.name for char in "*?[")
                        and name.startswith(relative.name + ".")
                    )
                    if not matches:
                        continue
                    path = checked_path(sftp, root, str(relative.parent / name))
                    paths[str(path)] = RemotePath(path, root, sftp, context, budget)
                    if len(paths) > context.max_scan_files:
                        listing_truncated = True
                        break
                if listing_truncated:
                    break
            result = scanner.scan_paths(
                source_config,
                request,
                context,
                root=root,
                paths=[paths[key] for key in sorted(paths)],
            )
            if listing_truncated and result.status in {
                ToolResultStatus.SUCCEEDED,
                ToolResultStatus.PARTIAL,
            }:
                result.status = ToolResultStatus.PARTIAL
                result.truncated = True
                result.warnings.append("remote_listing_limit_reached")
                result.cursor = None
            result.source_references = [
                SourceReference(
                    source_id="ssh_logs",
                    locator=f"remote-logs:{len(paths)} files/{budget['bytes']} bytes",
                )
            ]
            return result
        except ValueError:
            return ToolResult(
                status=ToolResultStatus.REJECTED, warnings=["log_path_rejected"]
            )
        except Exception:
            return ToolResult(
                status=ToolResultStatus.UNAVAILABLE, warnings=["ssh_logs_unavailable"]
            )
        finally:
            if sftp is not None:
                sftp.close()
            if client is not None:
                client.close()

    def close(self):
        pass


def plugin_factory(instance_config=None):
    return SSHLogsPlugin(instance_config)


plugin_factory.__buglens_manifest__ = manifest
