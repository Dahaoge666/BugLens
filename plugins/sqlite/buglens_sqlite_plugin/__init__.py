"""BugLens read-only SQLite plugin."""

from .plugin import SQLitePlugin, manifest, plugin_factory

__all__ = ["SQLitePlugin", "manifest", "plugin_factory"]
