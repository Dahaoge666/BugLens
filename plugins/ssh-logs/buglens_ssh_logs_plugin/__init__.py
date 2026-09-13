"""Read-only remote log connector."""

from .plugin import SSHLogsPlugin, manifest, plugin_factory

__all__ = ["SSHLogsPlugin", "manifest", "plugin_factory"]
