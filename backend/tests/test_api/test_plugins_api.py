from __future__ import annotations

import pytest


def test_plugins_test_endpoint(client, monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr(plugins_api, "get_plugin", lambda key: (_ for _ in ()).throw(KeyError("nope")))
    r = client.post("/api/v1/plugins/unknown/test", json={})
    assert r.status_code == 404


