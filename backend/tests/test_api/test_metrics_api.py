"""Tests for metrics API endpoint."""

from __future__ import annotations

import tempfile
import time
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from app.api.metrics import _sanitize_label_value
from app.core.plugins.base import BackupPlugin, BackupContext, RestoreContext


def wait_for_run_completion(
    client: TestClient,
    run_id: int,
    *,
    timeout_sec: float = 2.0,
    interval_sec: float = 0.05,
) -> Dict[str, Any]:
    """Poll the run until it leaves the running state or timeout."""
    deadline = time.monotonic() + timeout_sec
    last_payload: Dict[str, Any] | None = None
    while time.monotonic() < deadline:
        r = client.get(f"/api/v1/runs/{run_id}")
        if r.status_code == 200:
            payload = r.json()
            last_payload = payload
            if payload.get("status") != "running":
                return payload
        time.sleep(interval_sec)
    raise AssertionError(f"Run {run_id} did not complete. Last payload={last_payload}")


class TestMetricsSanitization:
    """Test label value sanitization for Prometheus."""
    
    def test_sanitize_label_value_basic(self):
        """Test basic sanitization of label values."""
        assert _sanitize_label_value("normal text") == "normal text"
        assert _sanitize_label_value("text with 'quotes'") == "text with 'quotes'"
    
    def test_sanitize_label_value_escapes(self):
        """Test escaping of backslashes and quotes."""
        assert _sanitize_label_value("back\\slash") == "back\\\\slash"
        assert _sanitize_label_value('double"quote') == 'double\\\\\\"quote'
    
    def test_sanitize_label_value_length_limit(self):
        """Test that very long values are truncated."""
        long_text = "a" * 300
        result = _sanitize_label_value(long_text)
        assert len(result) == 200
        assert result.endswith("a")


class TestMetricsEndpoint:
    """Test the /metrics endpoint."""
    
    def test_metrics_endpoint_empty(self, client: TestClient):
        """Test metrics endpoint with no data."""
        response = client.get("/metrics")
        assert response.status_code == 200
        content = response.text
        
        # Should return valid Prometheus format
        assert content.endswith("\n")
        assert "# HELP job_success_total" in content
        assert "# HELP job_failure_total" in content
        assert "# HELP last_run_timestamp" in content
        
        # Should have no metric lines (no jobs)
        lines = content.split("\n")
        metric_lines = [l for l in lines if l.startswith("job_") or l.startswith("last_run_")]
        assert len(metric_lines) == 0

    def test_metrics_endpoint_with_job_runs(self, client: TestClient, monkeypatch: pytest.MonkeyPatch):
        """Test metrics endpoint with actual job runs."""
        # Create a success plugin for testing
        class _SuccessPlugin(BackupPlugin):
            async def validate_config(self, config: Dict[str, Any]) -> bool:
                return True
            async def test(self, config: Dict[str, Any]) -> bool:
                return True
            async def backup(self, context: BackupContext) -> Dict[str, Any]:
                fd, path = tempfile.mkstemp(prefix="backup-test-", suffix=".txt")
                return {"artifact_path": path}
            async def restore(self, context: RestoreContext) -> Dict[str, Any]:
                return {"ok": True}
            async def get_status(self, context: BackupContext) -> Dict[str, Any]:
                return {"ok": True}

        import app.core.scheduler as sched
        monkeypatch.setattr(sched, "get_plugin", lambda name: _SuccessPlugin(name))
        
        # Create target and job
        target_payload = {
            "name": "Metrics Test Service",
            "plugin_name": "dummy",
            "plugin_config_json": "{}",
        }
        r = client.post("/api/v1/targets/", json=target_payload)
        assert r.status_code == 201
        
        # Get auto-tag
        r = client.get("/api/v1/tags/")
        assert r.status_code == 200
        tags = r.json()
        tag_id = next(t["id"] for t in tags if t.get("display_name") == "Metrics Test Service")
        
        # Create job
        job_payload = {
            "tag_id": tag_id,
            "name": "Metrics Job",
            "schedule_cron": "0 2 * * *",
            "enabled": True,
        }
        r = client.post("/api/v1/jobs/", json=job_payload)
        assert r.status_code == 201
        job_id = r.json()["id"]
        
        # Run job successfully (async)
        r = client.post(f"/api/v1/jobs/{job_id}/run")
        assert r.status_code == 200
        payload = r.json()
        assert payload["status"] in {"running", "success"}
        run = (
            wait_for_run_completion(client, payload["id"])
            if payload["status"] == "running"
            else payload
        )
        assert run["status"] == "success"
        
        # Check metrics
        response = client.get("/metrics")
        assert response.status_code == 200
        content = response.text
        
        # Should have metrics for the job
        assert 'job_success_total{job_id="1",job_name="Metrics Job"} 1' in content
        assert 'job_failure_total{job_id="1",job_name="Metrics Job"} 0' in content
        assert 'last_run_timestamp{job_id="1",job_name="Metrics Job"}' in content

    def test_metrics_endpoint_with_failed_job(self, client: TestClient, monkeypatch: pytest.MonkeyPatch):
        """Test metrics endpoint with failed job runs."""
        # Create a failing plugin for testing
        class _FailingPlugin(BackupPlugin):
            async def validate_config(self, config: Dict[str, Any]) -> bool:
                return True
            async def test(self, config: Dict[str, Any]) -> bool:
                return True
            async def backup(self, context: BackupContext) -> Dict[str, Any]:
                raise RuntimeError("Intentional failure for testing")
            async def restore(self, context: RestoreContext) -> Dict[str, Any]:
                return {"ok": True}
            async def get_status(self, context: BackupContext) -> Dict[str, Any]:
                return {"ok": True}

        import app.core.scheduler as sched
        monkeypatch.setattr(sched, "get_plugin", lambda name: _FailingPlugin(name))
        
        # Create target and job
        target_payload = {
            "name": "Failed Metrics Service",
            "plugin_name": "dummy",
            "plugin_config_json": "{}",
        }
        r = client.post("/api/v1/targets/", json=target_payload)
        assert r.status_code == 201
        
        # Get auto-tag
        r = client.get("/api/v1/tags/")
        assert r.status_code == 200
        tags = r.json()
        tag_id = next(t["id"] for t in tags if t.get("display_name") == "Failed Metrics Service")
        
        # Create job
        job_payload = {
            "tag_id": tag_id,
            "name": "Failed Job",
            "schedule_cron": "0 2 * * *",
            "enabled": True,
        }
        r = client.post("/api/v1/jobs/", json=job_payload)
        assert r.status_code == 201
        job_id = r.json()["id"]
        
        # Run job and expect failure (async)
        r = client.post(f"/api/v1/jobs/{job_id}/run")
        assert r.status_code == 200
        payload = r.json()
        assert payload["status"] in {"running", "failed"}
        run = (
            wait_for_run_completion(client, payload["id"])
            if payload["status"] == "running"
            else payload
        )
        assert run["status"] == "failed"
        
        # Check metrics
        response = client.get("/metrics")
        assert response.status_code == 200
        content = response.text
        
        # Should have failure metrics
        assert 'job_success_total{job_id="1",job_name="Failed Job"} 0' in content
        assert 'job_failure_total{job_id="1",job_name="Failed Job"} 1' in content
        assert 'last_run_timestamp{job_id="1",job_name="Failed Job"}' in content

    def test_metrics_endpoint_with_special_characters(self, client: TestClient, monkeypatch: pytest.MonkeyPatch):
        """Test metrics endpoint with special characters in job names."""
        # Create a success plugin for testing
        class _SuccessPlugin(BackupPlugin):
            async def validate_config(self, config: Dict[str, Any]) -> bool:
                return True
            async def test(self, config: Dict[str, Any]) -> bool:
                return True
            async def backup(self, context: BackupContext) -> Dict[str, Any]:
                fd, path = tempfile.mkstemp(prefix="backup-test-", suffix=".txt")
                return {"artifact_path": path}
            async def restore(self, context: RestoreContext) -> Dict[str, Any]:
                return {"ok": True}
            async def get_status(self, context: BackupContext) -> Dict[str, Any]:
                return {"ok": True}

        import app.core.scheduler as sched
        monkeypatch.setattr(sched, "get_plugin", lambda name: _SuccessPlugin(name))
        
        # Create target and job with special characters
        target_payload = {
            'name': 'Special "Characters" & \\Backslashes',
            "plugin_name": "dummy",
            "plugin_config_json": "{}",
        }
        r = client.post("/api/v1/targets/", json=target_payload)
        assert r.status_code == 201
        
        # Get auto-tag
        r = client.get("/api/v1/tags/")
        assert r.status_code == 200
        tags = r.json()
        tag_id = next(t["id"] for t in tags if 'Special "Characters"' in t.get("display_name", ""))
        
        # Create job with special characters in name
        job_payload = {
            "tag_id": tag_id,
            "name": 'Job with "quotes" and \\backslashes',
            "schedule_cron": "0 2 * * *",
            "enabled": True,
        }
        r = client.post("/api/v1/jobs/", json=job_payload)
        assert r.status_code == 201
        job_id = r.json()["id"]
        
        # Run job successfully (async)
        r = client.post(f"/api/v1/jobs/{job_id}/run")
        assert r.status_code == 200
        payload = r.json()
        assert payload["status"] in {"running", "success"}
        run = (
            wait_for_run_completion(client, payload["id"])
            if payload["status"] == "running"
            else payload
        )
        assert run["status"] == "success"
        
        # Check metrics - should properly escape special characters
        response = client.get("/metrics")
        assert response.status_code == 200
        content = response.text
        
        # Should properly escape special characters in labels
        assert 'job_success_total{job_id="1",job_name="Job with \\\\\\"quotes\\\\\\" and \\\\backslashes"}' in content
