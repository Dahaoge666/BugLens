"""Public, model-free protocol implemented by BugLens tool plugins.

The package intentionally contains no Agents SDK, model client, checkpoint or
BugLens application imports.  A plugin can therefore be installed and tested
independently from the diagnosis service.
"""

from .protocol import (
    PLUGIN_API_MAJOR,
    ExecutionContext,
    PluginHealth,
    PluginManifest,
    SourceReference,
    ToolPlugin,
    ToolResult,
    ToolResultStatus,
)

__all__ = [
    "PLUGIN_API_MAJOR",
    "ExecutionContext",
    "PluginHealth",
    "PluginManifest",
    "SourceReference",
    "ToolPlugin",
    "ToolResult",
    "ToolResultStatus",
]
