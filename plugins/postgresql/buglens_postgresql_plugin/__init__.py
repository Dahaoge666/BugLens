"""Read-only PostgreSQL connector."""

from .plugin import PostgreSQLPlugin, manifest, plugin_factory

__all__ = ["PostgreSQLPlugin", "manifest", "plugin_factory"]
