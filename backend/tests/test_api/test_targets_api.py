from __future__ import annotations

from fastapi.testclient import TestClient


def test_targets_crud(client: TestClient) -> None:
    # Create
    create_payload = {
        "name": "Test Database",
        "plugin_name": "dummy",
        "plugin_config_json": "{}",
    }
    r = client.post("/api/v1/targets/", json=create_payload)
    assert r.status_code == 201, r.text
    target = r.json()
    assert target["id"] > 0
    assert target["slug"] == "test-database"

    target_id = target["id"]

    # List
    r = client.get("/api/v1/targets/")
    assert r.status_code == 200
    items = r.json()
    assert isinstance(items, list)
    assert any(item["id"] == target_id for item in items)

    # Get by id
    r = client.get(f"/api/v1/targets/{target_id}")
    assert r.status_code == 200
    assert r.json()["id"] == target_id

    # Update
    r = client.put(f"/api/v1/targets/{target_id}", json={"name": "Renamed Database"})
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed Database"

    # Delete
    r = client.delete(f"/api/v1/targets/{target_id}")
    assert r.status_code == 204

    # 404 after delete
    r = client.get(f"/api/v1/targets/{target_id}")
    assert r.status_code == 404


def test_targets_test_endpoint(client: TestClient, monkeypatch) -> None:
    # Create a target
    r = client.post(
        "/api/v1/targets/",
        json={"name": "T1", "plugin_name": "dummy", "plugin_config_json": "{\"a\":1}"},
    )
    assert r.status_code == 201
    tid = r.json()["id"]

    # Monkeypatch endpoint-local get_plugin
    from app.core.plugins import loader as plugins_loader

    class _DummyPlugin:
        def __init__(self, name: str) -> None:
            self.name = name
        async def test(self, cfg):  # type: ignore[no-untyped-def]
            return cfg.get("a") == 1

    monkeypatch.setattr(plugins_loader, "get_plugin", lambda key: _DummyPlugin(key))

    # ok true
    r = client.post(f"/api/v1/targets/{tid}/test")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Update target to make test false - API provides default error message
    r = client.put(f"/api/v1/targets/{tid}", json={"plugin_config_json": "{\"a\":2}"})
    assert r.status_code == 200

    r = client.post(f"/api/v1/targets/{tid}/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "Connection test failed"

    # Invalid JSON -> 400
    r = client.put(f"/api/v1/targets/{tid}", json={"plugin_config_json": "{BAD JSON"})
    assert r.status_code == 200
    r = client.post(f"/api/v1/targets/{tid}/test")
    assert r.status_code == 400

    # Raises ValueError -> ok false + specific error message
    class _ValueErrorPlugin:
        def __init__(self, name: str) -> None:
            self.name = name
        async def test(self, cfg):  # type: ignore[no-untyped-def]
            raise ValueError("Invalid configuration: base_url and api_key are required")

    r = client.put(f"/api/v1/targets/{tid}", json={"plugin_name": "vaultwarden", "plugin_config_json": "{}"})
    assert r.status_code == 200
    monkeypatch.setattr(plugins_loader, "get_plugin", lambda key: _ValueErrorPlugin(key))
    r = client.post(f"/api/v1/targets/{tid}/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "Invalid configuration: base_url and api_key are required"

    # Raises FileNotFoundError -> ok false + specific error message
    class _FileNotFoundPlugin:
        def __init__(self, name: str) -> None:
            self.name = name
        async def test(self, cfg):  # type: ignore[no-untyped-def]
            raise FileNotFoundError("Container 'xyz' not found")

    monkeypatch.setattr(plugins_loader, "get_plugin", lambda key: _FileNotFoundPlugin(key))
    r = client.post(f"/api/v1/targets/{tid}/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "Container 'xyz' not found"

    # Raises ConnectionError -> ok false + specific error message
    class _ConnectionErrorPlugin:
        def __init__(self, name: str) -> None:
            self.name = name
        async def test(self, cfg):  # type: ignore[no-untyped-def]
            raise ConnectionError("Failed to connect to server: connection refused")

    monkeypatch.setattr(plugins_loader, "get_plugin", lambda key: _ConnectionErrorPlugin(key))
    r = client.post(f"/api/v1/targets/{tid}/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "Failed to connect to server: connection refused"

    # Unknown plugin on target -> 404
    r = client.put(f"/api/v1/targets/{tid}", json={"plugin_name": "missing"})
    assert r.status_code == 200
    monkeypatch.setattr(plugins_loader, "get_plugin", lambda key: (_ for _ in ()).throw(KeyError("nope")))
    r = client.post(f"/api/v1/targets/{tid}/test")
    assert r.status_code == 404


def test_targets_has_schedule_computed(client: TestClient) -> None:
    # Create a target T1
    r = client.post(
        "/api/v1/targets/",
        json={"name": "T1", "plugin_name": "dummy", "plugin_config_json": "{}"},
    )
    assert r.status_code == 201
    t1 = r.json()

    # Initially no jobs -> schedules endpoint returns empty list
    r = client.get(f"/api/v1/targets/{t1['id']}/schedules")
    assert r.status_code == 200
    assert r.json() == []

    # Create a DIRECT tag attachment and a job for that tag
    r = client.post("/api/v1/targets/%d/tags" % t1["id"], json={"tag_names": ["backup"]})
    assert r.status_code == 200

    # Fetch created tag id via API (black-box test)
    r = client.get("/api/v1/tags/")
    assert r.status_code == 200
    tag_list = r.json()
    tag = next(t for t in tag_list if t["slug"] == "backup")

    # Create an enabled job for that tag
    r = client.post(
        "/api/v1/jobs/",
        json={"tag_id": tag["id"], "name": "Nightly", "schedule_cron": "* * * * *", "enabled": True},
    )
    assert r.status_code == 201, r.text

    # List schedules via dedicated endpoint -> includes job name
    r = client.get(f"/api/v1/targets/{t1['id']}/schedules")
    assert r.status_code == 200
    assert r.json() == ["Nightly"]


