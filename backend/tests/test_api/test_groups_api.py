from __future__ import annotations

from fastapi.testclient import TestClient


def test_delete_non_empty_group_detaches_targets(client: TestClient) -> None:
    # Create a target
    r = client.post(
        "/api/v1/targets/",
        json={"name": "X", "plugin_name": "dummy", "plugin_config_json": "{}"},
    )
    assert r.status_code == 201
    tid = r.json()["id"]

    # Create a group and add target and tags
    r = client.post("/api/v1/groups/", json={"name": "G"})
    assert r.status_code == 201
    gid = r.json()["id"]

    r = client.post(f"/api/v1/groups/{gid}/targets", json={"target_ids": [tid]})
    assert r.status_code == 200

    r = client.post(f"/api/v1/groups/{gid}/tags", json={"tag_names": ["a", "b"]})
    assert r.status_code == 200

    # Delete the group (should succeed even though it is non-empty)
    r = client.delete(f"/api/v1/groups/{gid}")
    assert r.status_code == 204, r.text

    # Group is gone
    r = client.get(f"/api/v1/groups/{gid}")
    assert r.status_code == 404

    # Target remains and is detached
    r = client.get(f"/api/v1/targets/{tid}")
    assert r.status_code == 200
    assert r.json()["group_id"] is None


