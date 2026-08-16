from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.plugins.invoiceninja.plugin import InvoiceNinjaPlugin


def test_profilarr_discovery_exposes_flat_mode_aware_schema(client: TestClient) -> None:
    plugins_response = client.get("/api/v1/plugins/")
    assert plugins_response.status_code == 200
    profilarr = next(item for item in plugins_response.json() if item["key"] == "profilarr")
    assert profilarr == {
        "key": "profilarr",
        "name": "profilarr",
        "version": "0.2.1",
        "restore_capability": "automatic",
    }

    schema_response = client.get("/api/v1/plugins/profilarr/schema")
    assert schema_response.status_code == 200
    schema = schema_response.json()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["mode"]
    assert set(schema["properties"]) == {
        "mode",
        "database_path",
        "repository_path",
        "restore_directory",
    }
    assert schema["properties"]["mode"] == {
        "type": "string",
        "title": "Mode",
        "enum": ["source", "restore_destination"],
        "default": "source",
    }
    assert schema["properties"]["database_path"]["default"] == ("/sources/profilarr/profilarr.db")
    assert schema["properties"]["repository_path"]["default"] == ("/sources/profilarr/db")
    assert "default" not in schema["properties"]["restore_directory"]
    required_by_mode = {
        branch["if"]["properties"]["mode"]["const"]: branch["then"]["required"]
        for branch in schema["allOf"]
    }
    assert required_by_mode == {
        "source": ["database_path", "repository_path"],
        "restore_destination": ["restore_directory"],
    }


def test_invoice_ninja_public_discovery_schema_and_connectivity(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "invoice-api-test-token-must-not-escape"

    async def successful_probe(
        _self: InvoiceNinjaPlugin,
        config: dict[str, object],
    ) -> tuple[str, str]:
        assert config["token"] == secret
        return "Synthetic company", "Synthetic user"

    monkeypatch.setattr(InvoiceNinjaPlugin, "_probe", successful_probe)
    plugins_response = client.get("/api/v1/plugins/")
    assert plugins_response.status_code == 200
    entry = next(item for item in plugins_response.json() if item["key"] == "invoiceninja")
    assert entry["restore_capability"] == "partial"

    schema_response = client.get("/api/v1/plugins/invoiceninja/schema")
    assert schema_response.status_code == 200
    assert schema_response.json()["required"] == ["base_url", "token"]

    config = {"base_url": "https://invoice.local", "token": secret}
    success = client.post("/api/v1/plugins/invoiceninja/test", json=config)
    assert success.status_code == 200
    assert success.json() == {"ok": True}

    async def failed_probe(
        _self: InvoiceNinjaPlugin,
        _config: dict[str, object],
    ) -> tuple[str, str]:
        raise ConnectionError("Invoice Ninja synthetic connection failure")

    monkeypatch.setattr(InvoiceNinjaPlugin, "_probe", failed_probe)
    failure = client.post("/api/v1/plugins/invoiceninja/test", json=config)
    assert failure.status_code == 200
    assert failure.json() == {
        "ok": False,
        "error": "Invoice Ninja synthetic connection failure",
    }
    assert secret not in failure.text


def test_plugins_test_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Validate /plugins/{key}/test returns ok true/false and handles errors/404s."""

    # Monkeypatch the endpoint-local get_plugin symbol
    class _DummyPlugin:
        def __init__(self, name: str) -> None:
            self.name = name

        async def test(self, cfg):  # type: ignore[no-untyped-def]
            return True

    import app.api.plugins as plugins_api

    monkeypatch.setattr(plugins_api, "get_plugin", lambda key: _DummyPlugin(key))

    # ok true
    r = client.post("/api/v1/plugins/dummy/test", json={"k": 1})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # ok false - plugin returns False, API provides default error message
    class _FalsePlugin(_DummyPlugin):
        async def test(self, cfg):  # type: ignore[no-untyped-def]
            return False

    monkeypatch.setattr(plugins_api, "get_plugin", lambda key: _FalsePlugin(key))
    r = client.post("/api/v1/plugins/dummy/test", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "Connection test failed"

    # raises RuntimeError -> ok false + error message preserved
    class _RaisingPlugin(_DummyPlugin):
        async def test(self, cfg):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    monkeypatch.setattr(plugins_api, "get_plugin", lambda key: _RaisingPlugin(key))
    r = client.post("/api/v1/plugins/dummy/test", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "boom"

    # raises ValueError -> ok false + error message preserved
    class _ValueErrorPlugin(_DummyPlugin):
        async def test(self, cfg):  # type: ignore[no-untyped-def]
            raise ValueError("Invalid configuration: base_url and api_key are required")

    monkeypatch.setattr(plugins_api, "get_plugin", lambda key: _ValueErrorPlugin(key))
    r = client.post("/api/v1/plugins/dummy/test", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "Invalid configuration: base_url and api_key are required"

    # raises FileNotFoundError -> ok false + error message preserved
    class _FileNotFoundPlugin(_DummyPlugin):
        async def test(self, cfg):  # type: ignore[no-untyped-def]
            raise FileNotFoundError("Container 'xyz' not found")

    monkeypatch.setattr(plugins_api, "get_plugin", lambda key: _FileNotFoundPlugin(key))
    r = client.post("/api/v1/plugins/dummy/test", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "Container 'xyz' not found"

    # raises ConnectionError -> ok false + error message preserved
    class _ConnectionErrorPlugin(_DummyPlugin):
        async def test(self, cfg):  # type: ignore[no-untyped-def]
            raise ConnectionError("Failed to connect to PostgreSQL database: connection refused")

    monkeypatch.setattr(plugins_api, "get_plugin", lambda key: _ConnectionErrorPlugin(key))
    r = client.post("/api/v1/plugins/dummy/test", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "Failed to connect to PostgreSQL database: connection refused"

    # unknown plugin -> 404
    monkeypatch.setattr(
        plugins_api, "get_plugin", lambda key: (_ for _ in ()).throw(KeyError("nope"))
    )
    r = client.post("/api/v1/plugins/unknown/test", json={})
    assert r.status_code == 404
