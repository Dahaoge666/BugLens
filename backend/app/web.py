"""Small HTTP + SSE adapter; all business work remains in ApplicationService."""

from __future__ import annotations

import json
import secrets
from urllib.parse import parse_qs

from pydantic import TypeAdapter

from .admin import AdminApplicationService, ConfigMutation
from .application import ApplicationService
from .config import ConfigNotWritableError, ConfigRevisionConflictError
from .infra import (
    FencingTokenError,
    InvalidRunStatusError,
    LeaseConflictError,
    PendingRequestMismatchError,
    RevisionConflictError,
    RunNotFoundError,
)
from .protocol.commands import AgentCommand
from .security import sanitize_data


class BugLensASGI:
    def __init__(
        self,
        service: ApplicationService,
        admin: AdminApplicationService | None = None,
        cors_origin: str | None = None,
        admin_token: str | None = None,
    ) -> None:
        self.service = service
        self.admin = admin or AdminApplicationService(service)
        self.cors_origin = cors_origin
        self.admin_token = admin_token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        method = scope["method"]
        path = scope["path"].rstrip("/")
        parts = path.split("/")
        try:
            body = b""
            while True:
                message = await receive()
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break
            if method == "OPTIONS" and self.cors_origin:
                return await self._json(send, 204, {})
            if self.admin_token and (
                path == "/v1/admin" or path.startswith("/v1/admin/")
            ):
                supplied = self._authorization(scope)
                if supplied is None or not secrets.compare_digest(
                    supplied, self.admin_token
                ):
                    return await self._json(
                        send,
                        401,
                        {
                            "code": "admin_auth_required",
                            "message": "管理 API 需要 Bearer Token",
                        },
                        extra_headers=[(b"www-authenticate", b"Bearer")],
                    )
            if method == "POST" and path == "/v1/runs":
                command = TypeAdapter(AgentCommand).validate_json(body)
                return await self._events(send, self.service.send(command))
            if method == "POST" and len(parts) == 5 and parts[1:3] == ["v1", "runs"]:
                command = TypeAdapter(AgentCommand).validate_json(body)
                return await self._events(send, self.service.send(command))

            if method == "GET" and path == "/v1/admin/bootstrap/status":
                return await self._json(
                    send, 200, self.admin.bootstrap_status().model_dump(mode="json")
                )
            if method == "GET" and path == "/v1/admin/health":
                return await self._json(
                    send, 200, self.admin.health().model_dump(mode="json")
                )
            if method == "GET" and path == "/v1/admin/version":
                return await self._json(
                    send, 200, self.admin.version().model_dump(mode="json")
                )
            if method == "GET" and path == "/v1/admin/capabilities":
                return await self._json(
                    send, 200, self.admin.capabilities().model_dump(mode="json")
                )
            if method == "GET" and path == "/v1/admin/config":
                return await self._json(
                    send, 200, self.admin.config().model_dump(mode="json")
                )
            if method == "POST" and path == "/v1/admin/config/validate":
                mutation = ConfigMutation.model_validate_json(body)
                return await self._json(
                    send,
                    200,
                    self.admin.validate_config(mutation).model_dump(mode="json"),
                )
            if method in {"PUT", "PATCH"} and path == "/v1/admin/config":
                mutation = ConfigMutation.model_validate_json(body)
                return await self._json(
                    send, 200, self.admin.apply_config(mutation).model_dump(mode="json")
                )
            if method == "GET" and path == "/v1/admin/runs":
                query = self._query(scope)
                return await self._json(
                    send,
                    200,
                    self.admin.runs(
                        lifecycle_status=self._query_value(query, "lifecycle_status"),
                        outcome=self._query_value(query, "outcome"),
                        current_node=self._query_value(query, "node"),
                        profile=self._query_value(query, "profile"),
                        query=self._query_value(query, "q"),
                        offset=self._query_int(query, "offset", 0),
                        limit=self._query_int(query, "limit", 50),
                    ).model_dump(mode="json"),
                )
            if method == "GET" and path == "/v1/admin/sessions":
                query = self._query(scope)
                return await self._json(
                    send,
                    200,
                    self.admin.sessions(
                        run_id=self._query_value(query, "run_id"),
                        node=self._query_value(query, "node"),
                        status=self._query_value(query, "status"),
                        offset=self._query_int(query, "offset", 0),
                        limit=self._query_int(query, "limit", 50),
                    ).model_dump(mode="json"),
                )
            if (
                method == "GET"
                and len(parts) == 6
                and parts[1:4] == ["v1", "admin", "runs"]
                and parts[5] == "executions"
            ):
                query = self._query(scope)
                records = self.admin.node_executions(
                    run_id=parts[4],
                    node=self._query_value(query, "node"),
                    execution_id=self._query_value(query, "execution_id"),
                    status=self._query_value(query, "status"),
                    limit=self._query_int(query, "limit", 100),
                )
                return await self._json(
                    send,
                    200,
                    {
                        "items": [record.model_dump(mode="json") for record in records],
                        "total": len(records),
                    },
                )
            if (
                method == "GET"
                and len(parts) == 6
                and parts[1:4] == ["v1", "admin", "runs"]
                and parts[5] == "tools"
            ):
                query = self._query(scope)
                records = self.admin.tool_executions(
                    run_id=parts[4],
                    node_execution_id=self._query_value(query, "node_execution_id"),
                    sdk_tool_call_id=self._query_value(query, "sdk_tool_call_id"),
                    limit=self._query_int(query, "limit", 100),
                )
                return await self._json(
                    send,
                    200,
                    {
                        "items": [record.model_dump(mode="json") for record in records],
                        "total": len(records),
                    },
                )
            if (
                method == "GET"
                and len(parts) == 6
                and parts[1:4] == ["v1", "admin", "runs"]
                and parts[5] == "sessions"
            ):
                query = self._query(scope)
                return await self._json(
                    send,
                    200,
                    self.admin.sessions(
                        run_id=parts[4],
                        node=self._query_value(query, "node"),
                        status=self._query_value(query, "status"),
                        offset=self._query_int(query, "offset", 0),
                        limit=self._query_int(query, "limit", 50),
                    ).model_dump(mode="json"),
                )
            if method == "GET" and len(parts) == 4 and parts[1:3] == ["v1", "runs"]:
                view = await self.service.get_run(parts[3])
                return await self._json(send, 200, view.model_dump(mode="json"))
            if (
                method == "GET"
                and len(parts) == 5
                and parts[1:3] == ["v1", "runs"]
                and parts[4] == "events"
            ):
                query = parse_qs(scope.get("query_string", b"").decode())
                after = int(query.get("after", ["0"])[0])
                return await self._events(send, self.service.events(parts[3], after))
            return await self._json(send, 404, {"code": "not_found"})
        except Exception as exc:
            status = 400
            if isinstance(exc, RunNotFoundError):
                status = 404
            elif isinstance(
                exc,
                (
                    ConfigRevisionConflictError,
                    ConfigNotWritableError,
                    RevisionConflictError,
                    InvalidRunStatusError,
                    PendingRequestMismatchError,
                    LeaseConflictError,
                    FencingTokenError,
                ),
            ):
                status = 409
            return await self._json(
                send,
                status,
                {
                    "code": getattr(exc, "code", "validation_failed"),
                    "message": str(sanitize_data(str(exc))),
                },
            )

    async def _events(self, send, events) -> None:
        iterator = events.__aiter__()
        try:
            first = await iterator.__anext__()
        except StopAsyncIteration:
            # A history request may legitimately have no events after the
            # requested sequence. Keep the response an empty, valid SSE stream
            # instead of turning it into a JSON error after routing succeeded.
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": self._headers(
                        [
                            (b"content-type", b"text/event-stream"),
                            (b"cache-control", b"no-cache"),
                        ]
                    ),
                }
            )
            return await send(
                {"type": "http.response.body", "body": b"", "more_body": False}
            )
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": self._headers(
                    [
                        (b"content-type", b"text/event-stream"),
                        (b"cache-control", b"no-cache"),
                    ]
                ),
            }
        )
        for event in [first]:
            payload = (
                f"id: {event.sequence}\ndata: {event.model_dump_json()}\n\n"
            ).encode()
            await send(
                {"type": "http.response.body", "body": payload, "more_body": True}
            )
        async for event in iterator:
            payload = (
                f"id: {event.sequence}\ndata: {event.model_dump_json()}\n\n"
            ).encode()
            await send(
                {"type": "http.response.body", "body": payload, "more_body": True}
            )
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _json(
        self,
        send,
        status: int,
        payload: dict,
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        data = (
            b"" if status == 204 else json.dumps(payload, ensure_ascii=False).encode()
        )
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": self._headers(
                    [(b"content-type", b"application/json"), *(extra_headers or [])]
                ),
            }
        )
        await send({"type": "http.response.body", "body": data})

    def _headers(self, headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
        if self.cors_origin:
            headers.extend(
                [
                    (b"access-control-allow-origin", self.cors_origin.encode()),
                    (
                        b"access-control-allow-methods",
                        b"GET, POST, PUT, PATCH, OPTIONS",
                    ),
                    (
                        b"access-control-allow-headers",
                        b"content-type, accept, authorization",
                    ),
                    (b"access-control-expose-headers", b"content-type"),
                ]
            )
        return headers

    @staticmethod
    def _authorization(scope) -> str | None:
        for key, value in scope.get("headers", []):
            if key.lower() != b"authorization":
                continue
            scheme, _, token = value.decode("latin-1").partition(" ")
            if scheme.lower() != "bearer" or not token:
                return None
            return token
        return None

    @staticmethod
    def _query(scope) -> dict[str, list[str]]:
        return parse_qs(scope.get("query_string", b"").decode())

    @staticmethod
    def _query_value(query: dict[str, list[str]], name: str) -> str | None:
        values = query.get(name)
        return values[0] if values else None

    @staticmethod
    def _query_int(query: dict[str, list[str]], name: str, default: int) -> int:
        value = BugLensASGI._query_value(query, name)
        parsed = default if value is None else int(value)
        if name == "offset":
            return max(0, parsed)
        return max(1, min(parsed, 100))


def create_app(
    service: ApplicationService,
    cors_origin: str | None = None,
    admin_token: str | None = None,
) -> BugLensASGI:
    return BugLensASGI(service, cors_origin=cors_origin, admin_token=admin_token)
