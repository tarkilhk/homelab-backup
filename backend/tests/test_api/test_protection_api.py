from __future__ import annotations

from fastapi.testclient import TestClient


def test_protection_endpoint_reports_every_target(client: TestClient) -> None:
    response = client.post(
        "/api/v1/targets/",
        json={
            "name": "Protection API target",
            "plugin_name": "dummy",
            "plugin_config_json": "{}",
        },
    )
    assert response.status_code == 201
    target_id = response.json()["id"]

    response = client.get("/api/v1/protection/targets")

    assert response.status_code == 200
    assert response.json() == [
        {
            "target_id": target_id,
            "target_name": "Protection API target",
            "target_slug": "protection-api-target",
            "plugin_name": "dummy",
            "covering_jobs": [],
            "latest_attempt": None,
            "latest_success": None,
            "next_run_at": None,
            "consecutive_failures": 0,
            "gap_reason": "not_scheduled",
        }
    ]
