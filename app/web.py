"""Small HTTP + SSE adapter; all business work remains in ApplicationService."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

from pydantic import TypeAdapter

from .application import ApplicationService
from .protocol.commands import AgentCommand


class BugLensASGI:
    def __init__(self, service: ApplicationService) -> None:
        self.service = service

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
            if method == "POST" and path == "/v1/runs":
                command = TypeAdapter(AgentCommand).validate_json(body)
                return await self._events(send, self.service.send(command))
            if method == "POST" and len(parts) == 5 and parts[1:3] == ["v1", "runs"]:
                command = TypeAdapter(AgentCommand).validate_json(body)
                return await self._events(send, self.service.send(command))
            if method == "GET" and len(parts) == 4 and parts[1:3] == ["v1", "runs"]:
                view = await self.service.get_run(parts[3])
                return await self._json(send, 200, view.model_dump(mode="json"))
            if method == "GET" and len(parts) == 5 and parts[4] == "events":
                query = parse_qs(scope.get("query_string", b"").decode())
                after = int(query.get("after", ["0"])[0])
                return await self._events(send, self.service.events(parts[3], after))
            return await self._json(send, 404, {"code": "not_found"})
        except Exception as exc:
            return await self._json(
                send,
                400,
                {
                    "code": getattr(exc, "code", "validation_failed"),
                    "message": str(exc),
                },
            )

    async def _events(self, send, events) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/event-stream"),
                    (b"cache-control", b"no-cache"),
                ],
            }
        )
        async for event in events:
            payload = (
                f"id: {event.sequence}\ndata: {event.model_dump_json()}\n\n"
            ).encode()
            await send(
                {"type": "http.response.body", "body": payload, "more_body": True}
            )
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    @staticmethod
    async def _json(send, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": data})


def create_app(service: ApplicationService) -> BugLensASGI:
    return BugLensASGI(service)
