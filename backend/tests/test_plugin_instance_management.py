"""Environment integrations have independent configuration and credentials."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from buglens_plugin_api import PluginManifest
from pydantic import ValidationError

from app.admin import (
    AdminApplicationService,
    PluginInstanceMutation,
    PluginInstanceRevision,
    ServiceMutation,
)
from app.environment import (
    EnvironmentConfigError,
    EnvironmentDirectory,
    EnvironmentRepository,
    EnvironmentRevisionConflictError,
)
from app.plugins import EnvironmentToolService, PluginConfigError, PluginManager
from app.web import BugLensASGI


class IsolatedPlugin:
    manifest = PluginManifest(
        plugin_id="isolated",
        implementation_version="1.0.0",
        capabilities=["logs"],
    )

    def __init__(self):
        self.instance_config = {}
        self.cache = {"items": []}
        self.closed = False

    def validate_config(self, config, source_config=None):
        if config.get("invalid"):
            raise ValueError("invalid connection")

    def close(self):
        self.closed = True


@pytest.fixture
def integration_admin(tmp_path):
    path = tmp_path / "environments.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "environments": {
                    "prod": {"display_name": "Production"},
                    "stage": {"display_name": "Staging"},
                },
                "services": {"orders": {"name": "orders", "environment_id": "prod"}},
                "plugin_instances": {
                    "prod-a": {
                        "plugin_id": "isolated",
                        "environment_id": "prod",
                        "config": {"root": "a", "api_key": "nested-a"},
                        "password": "password-a",
                    },
                    "prod-b": {
                        "plugin_id": "isolated",
                        "environment_id": "prod",
                        "config": {"root": "b"},
                        "password": "password-b",
                    },
                    "stage-a": {
                        "plugin_id": "isolated",
                        "environment_id": "stage",
                        "config": {"root": "stage"},
                        "password": "password-stage",
                    },
                },
                "sources": {
                    "logs-a": {
                        "kind": "logs",
                        "environment_id": "prod",
                        "plugin_instance_id": "prod-a",
                        "config": {"path": "a.log"},
                    },
                    "logs-b": {
                        "kind": "logs",
                        "environment_id": "prod",
                        "plugin_instance_id": "prod-b",
                        "config": {"path": "b.log"},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    repository = EnvironmentRepository(path)
    manager = PluginManager([IsolatedPlugin()])
    tools = EnvironmentToolService(repository, manager)
    service = SimpleNamespace(
        environments=repository, runtime=SimpleNamespace(environment_tools=tools)
    )
    try:
        yield AdminApplicationService(service), repository, manager
    finally:
        manager.close()


def mutation_for(admin, instance_id="prod-a"):
    view = admin.environment_config()
    return PluginInstanceMutation(
        expected_revision=view.revision,
        instance=next(
            item
            for item in view.config["plugin_instances"]
            if item["id"] == instance_id
        ),
        sources=[
            item
            for item in view.config["sources"]
            if item["plugin_instance_id"] == instance_id
        ],
    )


def test_repeated_integrations_save_and_delete_only_the_selected_instance(
    integration_admin,
):
    admin, repository, _ = integration_admin
    original = repository.directory().model_dump(mode="python")
    snapshot = repository.snapshot("prod")
    mutation = mutation_for(admin)
    mutation.instance["config"]["root"] = "changed"
    mutation.sources[0]["config"]["path"] = "changed.log"
    mutation.secret_updates = {"password": {"action": "set", "value": "rotated-a"}}
    saved = admin.save_plugin_instance(mutation, instance_id="prod-a")
    updated = repository.directory()
    assert updated.plugin_instances[0].password == "rotated-a"
    assert updated.plugin_instances[0].config["api_key"] == "nested-a"
    assert len(updated.plugin_instances[1:]) == 2
    assert [
        item.model_dump(mode="python") for item in updated.plugin_instances[1:]
    ] == original["plugin_instances"][1:]
    assert (
        next(item for item in updated.sources if item.id == "logs-b").model_dump(
            mode="python"
        )
        == original["sources"][1]
    )
    assert snapshot.plugin_instance("prod-a").config["root"] == "a"
    assert snapshot.source("logs-a").config["path"] == "a.log"
    assert "rotated-a" not in json.dumps(saved.model_dump(mode="json"))
    deleted = admin.delete_plugin_instance(
        "prod-a", PluginInstanceRevision(expected_revision=saved.revision)
    )
    assert [item["id"] for item in deleted.config["plugin_instances"]] == [
        "prod-b",
        "stage-a",
    ]
    assert [item["id"] for item in deleted.config["sources"]] == ["logs-b"]
    assert deleted.config["services"] == original["services"]
    assert (
        EnvironmentRepository(repository.path).directory().plugin_instances[0].password
        == "password-b"
    )


def test_new_integration_of_same_plugin_starts_without_other_credentials(
    integration_admin,
):
    admin, repository, _ = integration_admin
    mutation = PluginInstanceMutation(
        expected_revision=repository.revision,
        instance={
            "id": "prod-c",
            "plugin_id": "isolated",
            "environment_id": "prod",
            "config": {"root": "c"},
        },
        sources=[],
        secret_updates={"token": {"action": "set", "value": "token-c"}},
    )
    admin.save_plugin_instance(mutation)
    created = repository.directory().plugin_instances[-1]
    assert created.password is None
    assert created.token == "token-c"
    assert created.config == {"root": "c"}
    clear = mutation_for(admin, "prod-a")
    clear.secret_updates = {"password": {"action": "clear"}}
    admin.save_plugin_instance(clear, instance_id="prod-a")
    assert repository.directory().plugin_instances[0].password is None
    assert repository.directory().plugin_instances[1].password == "password-b"


@pytest.mark.parametrize(
    "change",
    [
        "foreign-instance",
        "foreign-environment",
        "foreign-source-id",
        "identity",
        "invalid-config",
        "foreign-secret",
    ],
)
def test_instance_update_rejects_cross_instance_changes_atomically(
    integration_admin, change
):
    admin, repository, _ = integration_admin
    before = repository.path.read_bytes()
    mutation = mutation_for(admin)
    if change == "foreign-instance":
        mutation.sources[0]["plugin_instance_id"] = "prod-b"
    elif change == "foreign-environment":
        mutation.sources[0]["environment_id"] = "stage"
    elif change == "foreign-source-id":
        mutation.sources[0]["id"] = "logs-b"
    elif change == "identity":
        mutation.instance["environment_id"] = "stage"
        mutation.sources[0]["environment_id"] = "stage"
    elif change == "invalid-config":
        mutation.instance["config"]["invalid"] = True
    else:
        mutation.secret_updates = {"prod-b": {"password": {"action": "clear"}}}
    with pytest.raises(EnvironmentConfigError):
        admin.save_plugin_instance(mutation, instance_id="prod-a")
    assert repository.path.read_bytes() == before


def test_stale_revision_cannot_overwrite_or_delete_an_instance(integration_admin):
    admin, repository, _ = integration_admin
    stale = mutation_for(admin)
    new = mutation_for(admin, "prod-b")
    new.instance["config"]["root"] = "new-b"
    admin.save_plugin_instance(new, instance_id="prod-b")
    before = repository.path.read_bytes()
    with pytest.raises(EnvironmentRevisionConflictError):
        admin.save_plugin_instance(stale, instance_id="prod-a")
    with pytest.raises(EnvironmentRevisionConflictError):
        admin.delete_plugin_instance(
            "prod-a", PluginInstanceRevision(expected_revision=stale.expected_revision)
        )
    assert repository.path.read_bytes() == before


def test_plugin_runtime_mutable_state_and_reconfiguration_are_isolated(
    integration_admin,
):
    admin, repository, manager = integration_admin
    first, second = repository.directory().plugin_instances[:2]
    a = manager.instance(first)
    b = manager.instance(second)
    assert a is not b
    a.cache["items"].append("only-a")
    assert b.cache["items"] == []
    assert a.instance_config["password"] == "password-a"
    assert b.instance_config["password"] == "password-b"
    mutation = mutation_for(admin)
    mutation.instance["config"]["root"] = "new-a"
    admin.save_plugin_instance(mutation, instance_id="prod-a")
    assert manager.instance(repository.directory().plugin_instances[0]) is not a
    assert a.closed
    assert manager.instance(second) is b
    assert not b.closed


@pytest.mark.parametrize("bound", [False, True])
def test_catalog_rejects_sharing_an_instance_across_environments(bound):
    instance = {"plugin_id": "isolated"}
    if bound:
        instance["environment_id"] = "prod"
    with pytest.raises(ValidationError, match="mixes environments"):
        EnvironmentDirectory.model_validate(
            {
                "environments": {
                    "prod": {"display_name": "Prod"},
                    "stage": {"display_name": "Stage"},
                },
                "plugin_instances": {"shared": instance},
                "sources": {
                    environment: {
                        "kind": "logs",
                        "plugin_instance_id": "shared",
                        "environment_id": environment,
                    }
                    for environment in ["prod", "stage"]
                },
            }
        )


def test_old_unscoped_instance_is_inferred_from_its_source():
    directory = EnvironmentDirectory.model_validate(
        {
            "environments": {
                "prod": {"display_name": "Prod"},
                "stage": {"display_name": "Stage"},
            },
            "plugin_instances": {"legacy": {"plugin_id": "isolated"}},
            "sources": {
                "logs": {
                    "kind": "logs",
                    "plugin_instance_id": "legacy",
                    "environment_id": "prod",
                }
            },
        }
    )
    assert directory.plugin_instances[0].environment_id == "prod"


def test_service_management_preserves_integrations_and_rejects_rebinding(
    integration_admin,
):
    admin, repository, _ = integration_admin
    original = repository.directory().model_dump(mode="python")
    created = admin.save_service(
        ServiceMutation(
            expected_revision=repository.revision,
            service={"id": "billing", "name": "Billing", "environment_id": "prod"},
        )
    )
    admin.save_service(
        ServiceMutation(
            expected_revision=created.revision,
            service={
                "id": "billing",
                "name": "Billing API",
                "environment_id": "prod",
                "aliases": ["payments"],
            },
        ),
        service_id="billing",
    )
    current = repository.directory().model_dump(mode="python")
    assert current["plugin_instances"] == original["plugin_instances"]
    assert current["sources"] == original["sources"]
    before = repository.path.read_bytes()
    with pytest.raises(EnvironmentConfigError):
        admin.save_service(
            ServiceMutation(
                expected_revision=repository.revision,
                service={"id": "billing", "name": "Billing", "environment_id": "stage"},
            ),
            service_id="billing",
        )
    with pytest.raises(EnvironmentRevisionConflictError):
        admin.save_service(
            ServiceMutation(
                expected_revision=created.revision,
                service={"id": "new", "name": "New", "environment_id": "prod"},
            )
        )
    assert repository.path.read_bytes() == before


def test_service_bound_instance_requires_owning_service_on_every_source(
    integration_admin,
):
    admin, repository, _ = integration_admin
    mutation = mutation_for(admin)
    mutation.instance["service_id"] = "orders"
    before = repository.path.read_bytes()
    with pytest.raises(EnvironmentConfigError):
        admin.save_plugin_instance(mutation, instance_id="prod-a")
    assert repository.path.read_bytes() == before
    mutation.sources[0]["service_ids"] = ["orders"]
    admin.save_plugin_instance(mutation, instance_id="prod-a")
    assert repository.snapshot("prod").plugin_instance("prod-a").service_id == "orders"
    admin.save_service(
        ServiceMutation(
            expected_revision=repository.revision,
            service={"id": "billing", "name": "Billing", "environment_id": "prod"},
        )
    )
    mutation = mutation_for(admin)
    mutation.instance["service_id"] = "billing"
    mutation.sources[0]["service_ids"] = ["billing"]
    before = repository.path.read_bytes()
    with pytest.raises(EnvironmentConfigError):
        admin.save_plugin_instance(mutation, instance_id="prod-a")
    assert repository.path.read_bytes() == before


def test_catalog_returns_native_plugin_categories(integration_admin):
    from buglens_postgresql_plugin import PostgreSQLPlugin
    from buglens_ssh_logs_plugin import SSHLogsPlugin
    from buglens_ssh_plugin import SSHPlugin

    admin, _, _ = integration_admin
    manager = PluginManager([PostgreSQLPlugin(), SSHPlugin(), SSHLogsPlugin()])
    admin.service.runtime.environment_tools = EnvironmentToolService(
        admin.service.environments, manager
    )
    catalog = admin.plugins()
    assert {item.plugin_id: item.category for item in catalog.items} == {
        "postgresql": "database",
        "ssh": "host",
        "ssh_logs": "logs",
        "connector": "other",
    }
    assert {item.id for item in catalog.categories} >= {"database", "host", "logs"}
    assert all(item.display_name and item.description for item in catalog.items)
    manager.close()


def test_resumed_run_cannot_use_credentials_from_a_rebound_instance(integration_admin):
    admin, repository, manager = integration_admin
    snapshot = repository.snapshot("prod")
    path = repository.path
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["plugin_instances"]["prod-a"]["environment_id"] = "stage"
    raw["sources"]["logs-a"]["environment_id"] = "stage"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    service = EnvironmentToolService(repository, manager)
    with pytest.raises(PluginConfigError, match="no longer enabled"):
        service._current_instance(snapshot, snapshot.source("logs-a"))


@pytest.mark.parametrize(
    "name",
    [
        "environments.example.yaml",
        "environments.sqlite-demo.yaml",
        "environments.staging.yaml",
        "environments.remote.example.yaml",
    ],
)
def test_shipped_catalogs_use_separate_environment_instances(name):
    repository = EnvironmentRepository(Path(__file__).parents[1] / "config" / name)
    assert all(
        instance.environment_id for instance in repository.directory().plugin_instances
    )


def test_http_instance_routes_share_auth_revision_and_redaction(integration_admin):
    admin, repository, _ = integration_admin
    app = BugLensASGI(admin.service, admin=admin, admin_token="admin-test")

    async def request(method, path, payload, authorized=True, if_match=None):
        sent = []

        async def receive():
            return {
                "type": "http.request",
                "body": json.dumps(payload).encode(),
                "more_body": False,
            }

        async def send(message):
            sent.append(message)

        headers = [(b"authorization", b"Bearer admin-test")] if authorized else []
        if if_match:
            headers.append((b"if-match", if_match.encode()))
        await app(
            {"type": "http", "method": method, "path": path, "headers": headers},
            receive,
            send,
        )
        return sent[0]["status"], json.loads(sent[1]["body"])

    payload = {
        "expected_revision": repository.revision,
        "instance": {"id": "prod-c", "plugin_id": "isolated", "environment_id": "prod"},
        "sources": [],
        "secret_updates": {"token": {"action": "set", "value": "token-c"}},
    }
    assert (
        asyncio.run(request("POST", "/v1/admin/plugin-instances", payload, False))[0]
        == 401
    )
    status, saved = asyncio.run(request("POST", "/v1/admin/plugin-instances", payload))
    assert status == 201
    assert "token-c" not in json.dumps(saved)
    assert (
        asyncio.run(
            request(
                "POST",
                "/v1/admin/plugin-instances",
                {**payload, "expected_revision": saved["revision"]},
            )
        )[0]
        == 422
    )
    payload["expected_revision"] = saved["revision"]
    payload["instance"]["config"] = {"root": "c"}
    assert (
        asyncio.run(
            request(
                "PUT",
                "/v1/admin/plugin-instances/prod-c",
                payload,
                if_match="wrong-revision",
            )
        )[0]
        == 409
    )
    status, saved = asyncio.run(
        request("PUT", "/v1/admin/plugin-instances/prod-c", payload)
    )
    assert status == 200
    assert (
        asyncio.run(
            request(
                "DELETE",
                "/v1/admin/plugin-instances/prod-c",
                {"expected_revision": payload["expected_revision"]},
            )
        )[0]
        == 409
    )
    assert (
        asyncio.run(
            request(
                "DELETE",
                "/v1/admin/plugin-instances/prod-c",
                {"expected_revision": saved["revision"]},
            )
        )[0]
        == 200
    )


def test_http_service_routes_validate_identity_auth_and_revision(integration_admin):
    admin, repository, _ = integration_admin
    app = BugLensASGI(admin.service, admin=admin, admin_token="admin-test")

    async def request(method, path, payload, authorized=True, revision=None):
        messages = []

        async def receive():
            return {
                "type": "http.request",
                "body": json.dumps(payload).encode(),
                "more_body": False,
            }

        async def send(message):
            messages.append(message)

        headers = [(b"authorization", b"Bearer admin-test")] if authorized else []
        if revision:
            headers.append((b"if-match", revision.encode()))
        await app(
            {"type": "http", "method": method, "path": path, "headers": headers},
            receive,
            send,
        )
        return messages[0]["status"], json.loads(messages[1]["body"])

    payload = {
        "expected_revision": repository.revision,
        "service": {"id": "billing", "name": "Billing", "environment_id": "prod"},
    }
    assert asyncio.run(request("POST", "/v1/admin/services", payload, False))[0] == 401
    status, saved = asyncio.run(request("POST", "/v1/admin/services", payload))
    assert status == 201 and "password-a" not in json.dumps(saved)
    payload["expected_revision"] = saved["revision"]
    assert asyncio.run(request("POST", "/v1/admin/services", payload))[0] == 422
    assert (
        asyncio.run(
            request("PUT", "/v1/admin/services/billing", payload, revision="stale")
        )[0]
        == 409
    )
    assert asyncio.run(request("PUT", "/v1/admin/services/wrong", payload))[0] == 422
    payload["service"]["name"] = "Billing API"
    status, _ = asyncio.run(request("PUT", "/v1/admin/services/billing", payload))
    assert status == 200 and repository.directory().services[-1].name == "Billing API"
    assert (
        asyncio.run(
            request(
                "PUT",
                "/v1/admin/plugin-instances/prod-c",
                {**payload, "expected_revision": repository.revision},
            )
        )[0]
        == 422
    )
