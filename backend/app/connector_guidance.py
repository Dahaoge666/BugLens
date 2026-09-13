"""Reference templates and pinned, user-level connector context for SDK nodes."""

from typing import Any

from pydantic import BaseModel

from .capabilities import CapabilityRegistry
from .environment import ConnectorUsage, ConnectorUsageExample
from .models import InvestigationInput, NativeDiagnosisInput


def usage_template(manifest: Any) -> ConnectorUsage:
    if manifest.plugin_id == "connector":
        return ConnectorUsage()
    capabilities = manifest.capabilities
    specs = [
        spec
        for spec in CapabilityRegistry.default().all()
        if spec.operation != "describe_environment"
        and (
            spec.id in capabilities
            or any(kind in capabilities for kind in spec.source_kinds)
        )
    ]
    requests = {
        "describe_database": {},
        "query_database": {"sql": "SELECT 1", "parameters": {}},
        "search_logs": {
            "start_time": "2026-09-13T10:00:00+08:00",
            "end_time": "2026-09-13T10:15:00+08:00",
            "text_query": "ERROR",
        },
        "inspect_host": {"check": "system"},
        "search_knowledge": {"query": "连接超时 排查"},
        "search_traffic": {
            "start_time": "2026-09-13T10:00:00+08:00",
            "end_time": "2026-09-13T10:15:00+08:00",
            "text_query": "timeout",
        },
    }
    return ConnectorUsage(
        name=manifest.display_name or manifest.plugin_id,
        description=manifest.description or "读取此数据源中的诊断证据。",
        when_to_use="当问题涉及此服务的"
        + "、".join(sorted({kind for spec in specs for kind in spec.source_kinds}))
        + "数据时使用。",
        instructions="先通过 describe_environment 确认 source_id 与可用能力；使用此数据源对应的已注册只读工具。数据库先查看结构，再使用单条 SELECT 和绑定参数；日志和流量必须传入带时区的绝对 start_time、end_time，并按问题时间、关键词与 request_id 缩小范围。示例时间仅为格式示范，调用时替换为本次故障时间。工具返回的 evidence ID 可引用为证据，空结果或连接失败不能证明故障原因。",
        examples=[
            ConnectorUsageExample(
                operation=spec.operation, request=requests[spec.operation]
            )
            for spec in specs
        ],
    )


def with_connector_guides(payload: BaseModel, runtime: Any) -> BaseModel:
    """Attach reference data to model input, never to privileged instructions."""
    if not isinstance(payload, (InvestigationInput, NativeDiagnosisInput)):
        return payload
    snapshot = getattr(runtime, "environment_snapshot", None)
    guides = snapshot.connector_guides() if snapshot is not None else []
    return payload.model_copy(update={"connector_guides": guides})
