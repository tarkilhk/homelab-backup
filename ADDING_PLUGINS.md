## Adding New Backup Plugins (Agent-Ready)

This document is a complete, step-by-step guide for humans and coding agents to add new backup plugins to this repository. It is optimized for autonomy, determinism, and scale (70+ plugins). Follow it exactly.

### What you are building
- **Goal**: A plugin that can 1) validate its config, 2) test connectivity, 3) perform a backup into a deterministic artifact path, and 4) optionally restore and report status.
- **Runtime**: Plugins are Python async classes discovered dynamically by the backend. They are invoked by the scheduler during runs.
- **UI**: The frontend renders plugin config forms from a `schema.json` provided by each plugin.

### Authoritative interfaces in this repo
- Backend base class: `backend/app/core/plugins/base.py`
- Loader and discovery: `backend/app/core/plugins/loader.py`
- Example plugin: `backend/app/plugins/pihole/plugin.py`
- Plugin schema API: `backend/app/api/plugins.py`
- Frontend config UI: `frontend/src/pages/Targets.tsx`

Important: The canonical contract is defined by `base.py` and the working example plugin below.

```startLine:8:endLine:80:backend/app/core/plugins/base.py
@dataclass
class BackupContext:
    job_id: str
    target_id: str
    config: Dict[str, Any]
    metadata: Optional[Dict[str, Any]] = None

@dataclass
class RestoreContext:
    job_id: str
    source_target_id: str
    destination_target_id: str
    config: Dict[str, Any]
    artifact_path: str
    metadata: Optional[Dict[str, Any]] = None

class BackupPlugin(ABC):
    def __init__(self, name: str, version: str = "1.0.0"):
        self.name = name
        self.version = version

    @abstractmethod
    async def validate_config(self, config: Dict[str, Any]) -> bool: ...

    @abstractmethod
    async def test(self, config: Dict[str, Any]) -> bool: ...

    @abstractmethod
    async def backup(self, context: BackupContext) -> Dict[str, Any]: ...  # must return {"artifact_path": str}

    @abstractmethod
    async def restore(self, context: RestoreContext) -> Dict[str, Any]: ...

    @abstractmethod
    async def get_status(self, context: BackupContext) -> Dict[str, Any]: ...
```

### How plugins are discovered and executed
```startLine:38:endLine:118:backend/app/core/plugins/loader.py
def _discover_plugins() -> Dict[str, Type[BackupPlugin]]:
    # Scans backend/app/plugins/* directories that contain __init__.py
    # Imports app.plugins.<key> and/or app.plugins.<key>.plugin
    # Collects subclasses of BackupPlugin and registers them under the folder name (key)

def get_plugin(name: str) -> BackupPlugin:
    # Returns an instance via registry lookup (instantiated with name=name)
```

```startLine:164:endLine:199:backend/app/core/scheduler.py
# During a job run, the scheduler:
# 1) builds BackupContext with job_id, target_id, config, metadata (contains target_slug)
# 2) resolves plugin by target.plugin_name
# 3) calls plugin.backup(context) in a dedicated thread/loop
# 4) expects a dict with {"artifact_path": "/backups/..."}
```

### Files each plugin must provide
- `backend/app/plugins/<plugin_key>/__init__.py`
  - Re-export your class via `__all__` so discovery finds it quickly.
- `backend/app/plugins/<plugin_key>/plugin.py`
  - Define `class <Something>Plugin(BackupPlugin)` and implement the required methods.
- `backend/app/plugins/<plugin_key>/schema.json`
  - JSON Schema used by the UI to render the config form for targets.
- Optional: `README.md`, test fixtures, or helper modules alongside `plugin.py` if needed.

### Naming and key conventions
- **Folder name = plugin key** (e.g., `pihole`). This is the string used in `Target.plugin_name` and in API routes like `/plugins/<key>/schema`.
- **Class name** should end with `Plugin` (e.g., `PiHolePlugin`). Discovery prefers such names if multiple classes exist.
- **Version string**: Provide a stable string. It is shown in list endpoints and UI.

### Artifact and directory conventions
- Write backup artifacts under: `/backups/<target_slug>/<YYYY-MM-DD>/...`.
- Use a timestamp in filenames for uniqueness, e.g., `service-export-YYYYmmddTHHMMSS.ext`.
- Return `{"artifact_path": "/backups/..."}` from `backup`.
- **Sidecar metadata**: After creating the artifact, call `write_backup_sidecar(artifact_path, self, context, logger=self._logger)` from `app.core.plugins.sidecar` to write a JSON sidecar file (`<artifact_path>.meta.json`) containing plugin name, target slug, and creation timestamp. This enables disaster recovery scenarios where backups can be restored even without database records.

### JSON schema expectations (frontend rendering behavior)
- The UI fetches `/plugins/<key>/schema` and, if found, renders simple inputs from `properties`.
- Supported property types: `string`, `number`, `integer`, `boolean`.
- Recognized `format`: `uri` (renders a URL input). There is no special password input type; treat secrets as strings.
- `default` is used as a placeholder/hint in the UI; it is not auto-saved unless a value is provided.
- Include `title` for readable labels; include `required` for required fields.

Minimal example:

```json
{
  "type": "object",
  "required": ["base_url", "token"],
  "properties": {
    "base_url": { "type": "string", "format": "uri", "title": "Base URL", "default": "http://service.local" },
    "token": { "type": "string", "title": "API Token", "default": "your token" }
  }
}
```

## Step-by-step: Adding a new plugin

Follow these steps in order. For 70+ plugins, this process is designed to be repeated reliably by agents.

### 0) Research the target (do this first)
- **Identify the export mechanism**: native backup API, downloadable archive, database dump, or filesystem snapshot.
- **Authentication**: method (token, basic, OAuth), login flow, CSRF, session cookies, TLS requirements.
- **Endpoints**: exact URL paths for auth and export; required headers.
- **Formats**: file type (zip, json, sql), size expectations.
- **Limits**: rate limits, pagination, max export size, timeouts, long-running jobs.
- **Non-destructive test**: an endpoint to validate credentials/connectivity without side effects.
- **Failure modes**: common status codes and error messages.

Document your findings in comments inside `plugin.py` (top-level docstring) and in commit messages.

### 1) Scaffold files and folder
Create a new folder using the plugin key (lowercase, no spaces):

```bash
mkdir -p backend/app/plugins/<plugin_key>
printf '%s\n' 'from .plugin import <ClassName>Plugin' '' '__all__ = ["<ClassName>Plugin"]' > backend/app/plugins/<plugin_key>/__init__.py
cat > backend/app/plugins/<plugin_key>/schema.json <<'JSON'
{
  "type": "object",
  "required": [],
  "properties": {}
}
JSON
cat > backend/app/plugins/<plugin_key>/plugin.py <<'PY'
from __future__ import annotations

from typing import Any, Dict
import os
from datetime import datetime, timezone
import logging

import httpx
from app.core.plugins.base import BackupPlugin, BackupContext, RestoreContext


class <ClassName>Plugin(BackupPlugin):
    """<Service> backup plugin.

    Research summary:
    - Auth: <describe>
    - Export: <describe>
    - Endpoints: <list>
    """

    def __init__(self, name: str, version: str = "0.1.0") -> None:
        super().__init__(name=name, version=version)
        self._logger = logging.getLogger(__name__)

    async def validate_config(self, config: Dict[str, Any]) -> bool:
        # Enforce required keys and simple shapes only
        return isinstance(config, dict) and all(k in config for k in [
            # "base_url", "token", ...
        ])

    async def test(self, config: Dict[str, Any]) -> bool:
        # Implement a non-destructive connectivity/auth check
        if not await self.validate_config(config):
            return False
        try:
            # Example outline (update as needed):
            # base_url = str(config["base_url"]).rstrip("/")
            # async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            #     resp = await client.get(f"{base_url}/health", headers={...})
            #     return resp.status_code // 100 == 2
            return True
        except Exception as exc:
            self._logger.warning("test_failed | error=%s", exc)
            return False

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        # Prepare directories per convention
        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        base_dir = os.path.join("/backups", target_slug, today)
        os.makedirs(base_dir, exist_ok=True)

        timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        artifact_path = os.path.join(base_dir, f"<plugin_key>-backup-{timestamp}.bin")

        cfg = context.config or {}
        # Implement export and write artifact_path
        # ...

        # Write sidecar metadata for disaster recovery
        from app.core.plugins.sidecar import write_backup_sidecar
        write_backup_sidecar(artifact_path, self, context, logger=self._logger)

        # Must return artifact path
        return {"artifact_path": artifact_path}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        return {"status": "not_implemented"}

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        return {"status": "ok"}
PY
```

Replace `<plugin_key>` and `<ClassName>` appropriately. Example: `pihole` and `PiHole`.

### 2) Define the schema.json
Populate required fields and user-friendly labels. Example for a token-based HTTP API:

```json
{
  "type": "object",
  "required": ["base_url", "token"],
  "properties": {
    "base_url": { "type": "string", "format": "uri", "title": "Base URL", "default": "http://service.local" },
    "token": { "type": "string", "title": "API Token", "default": "your token" },
    "verify_tls": { "type": "boolean", "title": "Verify TLS", "default": true }
  }
}
```

Guidelines:
- Keep it flat (simple key/value). Complex nested schemas won’t render richly in the current UI.
- Avoid putting secrets into `default`. Use descriptive hints (e.g., "your token").

### 3) Implement validate_config and test
- `validate_config`: lightweight presence/type checks only.
- `test`: must be non-destructive. Use small timeouts (`timeout=10.0`), follow redirects if appropriate, handle non-2xx as failures, and never log secrets.

**CRITICAL: Return True vs Raise Exceptions**

The `test()` method signature is `-> bool`, but **you MUST raise exceptions for failures** to provide meaningful error messages to users. The API endpoint catches exceptions and returns them as error messages.

- **Return `True`** ONLY when the test succeeds completely.
- **Raise exceptions** for ALL failures:
  - `ValueError` for invalid configuration
  - `FileNotFoundError` for missing resources (containers, files, etc.)
  - `ConnectionError` for network/connection failures
  - `RuntimeError` for driver/library issues or HTTP errors

Example pattern (see `JellyfinPlugin.test` or `PostgreSQLPlugin.test`):

```python
async def test(self, config: Dict[str, Any]) -> bool:
    if not await self.validate_config(config):
        raise ValueError("Invalid configuration: base_url and api_key are required")
    
    base_url = str(config.get("base_url", "")).rstrip("/")
    api_key = str(config.get("api_key", ""))
    url = f"{base_url}/api/status"
    
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"X-Api-Key": api_key})
            if resp.status_code != 200:
                raise RuntimeError(f"API returned status {resp.status_code}")
            data = resp.json()
            if not data.get("version"):
                raise ValueError("API response missing version field")
            return True
    except httpx.HTTPError as exc:
        raise ConnectionError(f"Failed to connect to server: {exc}") from exc
```

**Why exceptions instead of `return False`?** The API endpoint (`backend/app/api/plugins.py`) catches exceptions and returns `{"ok": False, "error": str(exc)}` to the frontend. If you return `False`, users only see a generic "Connection test failed" message.

### 4) Implement backup
- Build the directory `/backups/<target_slug>/<YYYY-MM-DD>/`.
- Perform export (HTTP download, DB dump, etc.).
- Write the artifact to a deterministic filename with timestamp.
- Return `{ "artifact_path": "..." }`.

Example patterns (see `PiHolePlugin.backup`):

```startLine:92:endLine:121:backend/app/plugins/pihole/plugin.py
async def backup(self, context: BackupContext) -> Dict[str, Any]:
    meta = context.metadata or {}
    target_slug = meta.get("target_slug") or str(context.target_id)
    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    base_dir = os.path.join("/backups", target_slug, today)
    os.makedirs(base_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
    artifact_path = os.path.join(base_dir, f"pihole-teleporter-{timestamp}.zip")
```

Error-handling guidance:
- Use `httpx.AsyncClient` with `timeout` and `follow_redirects=True` unless disallowed.
- Call `resp.raise_for_status()` for HTTP requests; log structured info without secrets.
- If no content or missing fields, raise a `RuntimeError` with a short message.

### 5) Optional: restore and get_status
- If the target supports restore, implement it; otherwise return `{"status": "not_implemented"}`.
- `get_status` can return `{"status": "ok"}` if no richer status is available.

### 6) Tests (write first whenever possible)
The repository includes `pytest` and `pytest-asyncio`. Write tests under `backend/tests/`.

Minimum tests to include:
- `test_<plugin_key>_validate_config.py`: positive/negative cases.
- `test_<plugin_key>_test.py`: uses network mocking to simulate success/failure.
- `test_<plugin_key>_backup.py`: uses temporary directory and mock HTTP to produce an artifact; ensures the returned `artifact_path` exists and is non-empty.

Recommended mocking approach: `httpx.MockTransport` or monkeypatch `httpx.AsyncClient` inside your plugin to use a custom transport. Example:

```python
import asyncio
import os
import json
from datetime import datetime, timezone
import httpx
import pytest
from app.core.plugins.base import BackupContext
from app.plugins.<plugin_key>.plugin import <ClassName>Plugin


@pytest.mark.asyncio
async def test_test_success(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        # Return a simple JSON that your plugin expects
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    # Monkeypatch AsyncClient to force our transport
    orig_client = httpx.AsyncClient
    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", _client)

    plugin = <ClassName>Plugin(name="<plugin_key>")
    ok = await plugin.test({"base_url": "http://example.local"})
    assert ok is True


@pytest.mark.asyncio
async def test_test_failure_raises_exception(monkeypatch):
    """Test that failures raise exceptions with specific error messages."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)  # Simulate failure

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient
    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", _client)

    plugin = <ClassName>Plugin(name="<plugin_key>")
    # Expect an exception, not False
    with pytest.raises((RuntimeError, ConnectionError), match=".*"):
        await plugin.test({"base_url": "http://example.local"})


@pytest.mark.asyncio
async def test_backup_writes_artifact(tmp_path, monkeypatch):
    # Force transport to return bytes for the export endpoint
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/export"):
            return httpx.Response(200, content=b"data")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient
    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", _client)

    plugin = <ClassName>Plugin(name="<plugin_key>")
    ctx = BackupContext(
        job_id="1",
        target_id="1",
        config={"base_url": "http://example.local"},
        metadata={"target_slug": "target-slug"},
    )
    # Monkeypatch output base if your implementation needs it; otherwise, verify artifact exists
    result = await plugin.backup(ctx)
    artifact_path = result.get("artifact_path")
    assert artifact_path and os.path.isabs(artifact_path)
```

Run tests for the backend:

```bash
cd backend
pytest -q
```

### 7) Verify discovery and schema via API
With the backend running, check:

```bash
# List plugins (should include your key)
curl -s http://localhost:8000/plugins | jq

# Fetch your schema (must exist)
curl -s http://localhost:8000/plugins/<plugin_key>/schema | jq

# Test connectivity with a sample config
curl -s -X POST http://localhost:8000/plugins/<plugin_key>/test \
  -H 'content-type: application/json' \
  -d '{"base_url": "http://service.local", "token": "example"}'
```

### 8) Create a target in the UI
- Open the Targets page.
- Select your plugin from the dropdown.
- The form should render from your `schema.json`.
- Click Test; expect success under the mocked or real environment.
- Save target.

## Quality checklist (must pass)
- **Structure**: Files under `backend/app/plugins/<plugin_key>/` with `__init__.py`, `plugin.py`, `schema.json`.
- **Discovery**: Plugin appears in `/plugins` list.
- **Schema**: `/plugins/<plugin_key>/schema` returns your JSON schema.
- **Test**: `POST /plugins/<plugin_key>/test` returns `{ "ok": true }` with valid config (under mocked or real conditions).
- **Backup**: `backup()` writes artifact to `/backups/<slug>/<YYYY-MM-DD>/...` and returns `{"artifact_path": "..."}`.
- **Logs**: No secrets in logs; use short, structured messages.
- **Types**: All functions typed; no `any` where avoidable.
- **Lints/Tests**: `pytest` passes; no linter/type errors introduced.

## Patterns and recommendations
- **HTTP services**: prefer `httpx.AsyncClient`, `timeout=10–30s`, `follow_redirects=True` if needed. Validate JSON shapes defensively.
- **CLI-based exports**: if you must call external tools, prefer `asyncio.create_subprocess_exec` with timeouts. Capture stdout to write artifacts. Ensure deterministic filenames.
- **Large downloads**: stream to disk if needed; write to a temp file and `os.replace` to final path for atomicity.
- **Retries**: Keep logic simple; the scheduler handles retries at a higher level. Only retry when the service has a documented transient failure mode.
- **Secrets**: Never echo tokens/passwords. Redact in logs.
- **Testing**: Prefer transport-level mocks to avoid network.

## Common pitfalls
- Returning the wrong shape from `backup()` (must include `artifact_path`).
- Forgetting to create `__init__.py` or to export your class in `__all__`.
- Missing or malformed `schema.json` (frontend falls back to raw JSON textarea).
- Logging secrets.
- Writing artifacts outside `/backups/...` or using non-deterministic paths.

## Example: Pi-hole plugin (reference)
See `backend/app/plugins/pihole/plugin.py` for a complete, working example using session + CSRF followed by a zip download. Key lines:

```startLine:121:endLine:176:backend/app/plugins/pihole/plugin.py
# 1) Authenticate, capture CSRF
# 2) GET teleporter with CSRF header; expect zip content
# 3) Write to /backups/<slug>/<date>/pihole-teleporter-<ts>.zip
```

## Acceptance criteria for autonomous agents
- Provide a PR that contains only the new plugin folder and tests (plus optional docs).
- Include a brief `plugin.py` docstring summarizing research.
- Ensure discovery, test endpoint, and artifact path behavior are correct.
- Include passing tests demonstrating validate/test/backup behavior with mocked IO.

## Maintenance
- If target API changes, update `test` to reflect the minimal success shape.
- Keep versions updated in your plugin `__init__`.
- When adding new inputs, update both `schema.json` and validate/test logic.
