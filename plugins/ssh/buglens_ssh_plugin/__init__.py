"""Read-only SSH host inspection."""

from .plugin import SSHPlugin, manifest, plugin_factory

__all__ = ["SSHPlugin", "manifest", "plugin_factory"]
