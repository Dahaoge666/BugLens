"""Built-in read-only exploration tools for autonomous mode.

These connectors back the *autonomous exploration* mode: when a run is started
with ``context.attributes["auto_explore"]`` set, the runtime skips clarification
requests and lets the agents gather public diagnostic context themselves via
read-only, bounded, approval-free tools.

Every tool here obeys the AGENTS.md connector contract: it is read-only,
least-privilege, returns a bounded result, and each execution is given a stable
``tool_execution_id`` by :func:`app.tools._instrument_tool` so the audit trail
can reference it.
"""

from __future__ import annotations

import json
from typing import Any

from .security import sanitize_data
from .tools import ToolRegistry

#: Hard ceiling on a single ``http_get`` response summary in bytes. The runtime
#: also bounds tool results via ``bounded_tool_result``; this is the
#: connector-level defence so a multi-megabyte page never reaches the model.
_MAX_RESPONSE_BYTES = 8_192


async def http_get(url: str) -> str:
    """Perform a read-only HTTP GET and return a bounded, sanitized summary.

    The agent calls this to fetch public diagnostic context (runbooks, status
    pages, docs). The response body is truncated to ``_MAX_RESPONSE_BYTES`` and
    redacted before it is returned, so secrets and oversized payloads never
    enter the model context. The call is approval-free
    (``needs_approval=False``) so it executes immediately in autonomous mode.
    """
    # Imported lazily: ``httpx2`` is a transitive dependency (via the OpenAI
    # SDK) and only required when the connector is actually invoked.
    import httpx2

    async with httpx2.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        response = await client.get(url)
        content_type = response.headers.get("content-type", "")
        body = response.text
        status_code = response.status_code
    return _format_http_response(status_code, content_type, body)


def _format_http_response(status_code: int, content_type: str, body: str) -> str:
    """Render a bounded, sanitized text/JSON summary of an HTTP response body."""
    if "json" in content_type.lower():
        try:
            cleaned = sanitize_data(json.loads(body))
            rendered = json.dumps(cleaned, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            rendered = body
    else:
        rendered = body
    summary = f"HTTP {status_code}\n{rendered}"
    bounded = summary.encode("utf-8", errors="ignore")[:_MAX_RESPONSE_BYTES].decode(
        "utf-8", errors="ignore"
    )
    return str(sanitize_data(bounded))


def build_builtin_tool_registry() -> ToolRegistry:
    """Construct the built-in read-only tool registry for autonomous mode.

    All profiles register ``http_get`` here, but it is only exposed to an agent
    when the resolved profile's :class:`~app.config.ToolPolicy` has
    ``enabled=true`` (filtered by :meth:`ToolRegistry.is_enabled` via the
    per-node ``tools_enabled`` flag). This keeps the default secure: a profile
    must opt in before agents gain any connector capability.
    """
    registry = ToolRegistry(
        enabled=True,
        allowed_nodes={"analyze", "investigate"},
        max_results=20,
        timeout_seconds=30,
    )
    registry.register(
        http_get,
        name="http_get",
        nodes={"analyze", "investigate"},
        needs_approval=False,
    )
    return registry


__all__: list[Any] = ["build_builtin_tool_registry", "http_get"]
