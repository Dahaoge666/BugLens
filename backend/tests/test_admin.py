import asyncio
from pathlib import Path

import pytest

from app.admin import AdminApplicationService, ConfigMutation
from app.bootstrap import build_local_service
from app.config import DEFAULT_PROFILE, ConfigRepository, Settings
from app.web import create_app


def test_config_revision_and_atomic_profile_update(tmp_path: Path):
    path = tmp_path / "buglens.yaml"
    repository = ConfigRepository(path)
    candidate = {**DEFAULT_PROFILE, "config_version": "default-v2"}

    revision = repository.revision
    resolved = repository.apply_profile("default", candidate, revision)

    assert resolved.config_version == "default-v2"
    assert repository.revision != revision
    assert ConfigRepository(path).resolve("default").config_version == "default-v2"

    with pytest.raises(ValueError, match="configuration revision changed"):
        repository.apply_profile("default", candidate, revision)


def test_admin_views_and_session_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    service, store, runner = build_local_service(
        Settings(session_db=tmp_path / "state.db", config_path=tmp_path / "config.yaml")
    )
    try:
        admin = AdminApplicationService(service)
        assert admin.bootstrap_status().setup_required is True
        assert admin.runs().total == 0

        store.db.executescript(
            """
            CREATE TABLE agent_sessions (
                session_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE agent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                message_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO agent_sessions(session_id) VALUES ('run-1:analyze');
            INSERT INTO agent_messages(session_id, message_data) VALUES ('run-1:analyze', '{}');
            """
        )
        store.db.commit()
        sessions = admin.sessions()
        assert sessions.total == 1
        assert sessions.items[0].node == "analyze"
        assert sessions.items[0].message_count == 1
    finally:
        runner.close()
        store.close()


def test_admin_config_validation_does_not_mutate(tmp_path: Path):
    service, store, runner = build_local_service(
        Settings(session_db=tmp_path / "state.db", config_path=tmp_path / "config.yaml")
    )
    try:
        admin = AdminApplicationService(service)
        current = admin.config()
        mutation = ConfigMutation(
            profile="default",
            config={**current.profiles["default"], "config_version": "default-v3"},
            expected_revision=current.revision,
        )
        validation = admin.validate_config(mutation)
        assert validation.valid is True
        assert admin.config().revision == current.revision
    finally:
        runner.close()
        store.close()


def test_web_admin_health_and_cors(tmp_path: Path):
    service, store, runner = build_local_service(
        Settings(session_db=tmp_path / "state.db")
    )
    try:
        app = create_app(service, cors_origin="http://localhost:5173")

        async def call(method: str, path: str):
            sent: list[dict] = []
            received = False

            async def receive():
                nonlocal received
                if received:
                    return {"type": "http.disconnect"}
                received = True
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                sent.append(message)

            await app(
                {
                    "type": "http",
                    "method": method,
                    "path": path,
                    "query_string": b"",
                },
                receive,
                send,
            )
            return sent

        health = asyncio.run(call("GET", "/v1/admin/health"))
        assert health[0]["status"] == 200
        assert b'"status"' in health[1]["body"]
        assert (b"access-control-allow-origin", b"http://localhost:5173") in health[0][
            "headers"
        ]

        options = asyncio.run(call("OPTIONS", "/v1/admin/config"))
        assert options[0]["status"] == 204
    finally:
        runner.close()
        store.close()
