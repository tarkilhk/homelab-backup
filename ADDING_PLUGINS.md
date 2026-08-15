# Adding a backup plugin

This is the canonical implementation contract for every new Homelab Backup
plugin. Follow the steps in order. The runtime interfaces remain the ultimate
source of truth:

- `backend/app/core/plugins/base.py` — contexts and plugin interface
- `backend/app/core/plugins/artifacts.py` — artifact publication and validation
- `backend/app/core/plugins/loader.py` — discovery
- `backend/app/services/restores.py` — restore orchestration and result contract
- `backend/app/api/plugins.py` — discovery, schema, and connectivity API
- `frontend/src/pages/Targets.tsx` — schema-driven target form

Do not copy an old plugin merely because it is similar. Confirm that it still
follows this contract first.

## Completion contract

A new plugin is complete only when all of these are true:

1. Its exact deployed service version and supported backup/restore mechanisms
   are documented from primary sources.
2. Discovery and the flat configuration schema work through `/api/v1/plugins`.
3. `test()` is non-destructive, returns `True` only on complete success, and
   raises a useful exception for every failure.
4. `backup()` publishes one usable, non-empty artifact through
   `create_backup_artifact()` or `write_backup_bytes()` and returns its absolute
   `artifact_path`. The helper writes the required sidecar atomically.
5. `restore()` validates the vendor payload, restores to an isolated
   destination, and returns an honest terminal status.
6. The plugin declares `restore_capability` as `automatic` or `partial`.
   `partial` means the strongest available verification cannot prove complete
   restoration. New backup-only plugins are outside the product contract.
7. Automated tests cover configuration, connectivity failures, artifact
   validation, restore behavior, and important failure paths.
8. `get_status()` implements the abstract runtime interface and reports only a
   state it actually checked; it must not manufacture an unconditional healthy
   result when the service cannot be observed.
9. Two consecutive local backup-to-restore drills pass against the exact
   deployed service version, with independent size, hash, content, and
   readiness evidence.

Production validation is limited to non-destructive connectivity checks and
backup/export triggers after deployment. Every production restore is forbidden.

## 1. Research the service boundary

Record the following before writing code:

- exact deployed image and application version;
- authoritative state: database, files, object storage, keys, and configuration;
- vendor-supported export and restore workflow;
- consistency requirements, including quiescing or service downtime;
- non-destructive authentication/connectivity check;
- archive format and required members;
- async-job polling, timeouts, restart, and readiness behavior;
- minimum filesystem, network, socket, or API privileges;
- secrets that may appear in requests, artifacts, URLs, or logs.

Stop and ask when consistency requires production downtime, a safe restore
contract is unavailable, authoritative state is unclear, broad privileges are
required, or compatibility behavior would be necessary.

## 2. Define the public seams and write one red test

Use vertical test slices. The normal seams are:

- plugin methods for vendor protocol and artifact behavior;
- loader functions for discovery;
- `/api/v1/plugins/` and `/api/v1/plugins/<key>/schema` for API behavior;
- `RestoreService` when orchestration behavior changes.

Prefer `httpx.MockTransport` for HTTP protocols and controlled subprocess or
Docker fakes for local system boundaries. Assert observable results rather than
private calls. Make one test fail for the next behavior, implement only that
behavior, and repeat.

## 3. Add the plugin package and schema

Create:

```text
backend/app/plugins/<plugin_key>/
├── __init__.py
├── plugin.py
└── schema.json
```

`__init__.py` re-exports exactly one concrete class:

```python
from .plugin import ExamplePlugin

__all__ = ["ExamplePlugin"]
```

The folder name is the stable plugin key. A schema stays flat because the
Targets UI renders simple properties:

```json
{
  "type": "object",
  "required": ["base_url", "api_key"],
  "properties": {
    "base_url": {
      "type": "string",
      "format": "uri",
      "title": "Base URL",
      "default": "http://service.local"
    },
    "api_key": {
      "type": "string",
      "title": "API Key"
    }
  }
}
```

Defaults are hints, not persisted values. Never place a credential in a
default. Validate required values as the documented type; avoid coercing absent
values into strings such as `"None"`.

## 4. Implement configuration and connectivity

`validate_config()` performs deterministic shape validation. `test()` exercises
the smallest real, non-destructive path that proves the configured mechanism is
usable. It follows this result contract:

- success: return `True`;
- invalid input: raise `ValueError`;
- missing local resource: raise `FileNotFoundError`;
- network failure: raise `ConnectionError`;
- vendor, driver, command, authentication, or response failure: raise
  `RuntimeError` with a concise user-facing message.

The API exposes exception text to the target form, so redact URLs and messages
that may contain passwords, tokens, cookies, signatures, or query credentials.

HTTP clients default to `follow_redirects=False`. If the vendor requires a
redirect, validate its destination origin before resending any secret-bearing
header. Give every request, poll loop, subprocess, and Docker operation a
deadline and ensure timed-out or cancelled child work has stopped.

## 5. Publish the artifact transactionally

For an in-memory export, validate its vendor format and then publish it:

```python
from app.core.plugins.artifacts import write_backup_bytes

artifact_path = write_backup_bytes(
    self,
    context,
    payload,
    prefix="example-export",
    suffix=".zip",
)
return {"artifact_path": artifact_path}
```

For a streamed export or CLI dump, write only to the temporary path yielded by
the transactional helper:

```python
from app.core.plugins.artifacts import create_backup_artifact

with create_backup_artifact(
    self,
    context,
    prefix="example-export",
    suffix=".tar.gz",
) as artifact:
    await stream_export_to(artifact.temporary_path)
    validate_vendor_archive(artifact.temporary_path)

return {"artifact_path": str(artifact.final_path)}
```

On normal exit the helper verifies a non-empty regular file, flushes it,
atomically renames it, and writes `<artifact_path>.meta.json`. On failure it
removes partial output. Direct writes to the final path and direct sidecar calls
are outside the plugin contract.

Vendor validation must go beyond “non-empty.” Examples include ZIP CRC and
required members, SQL dump headers, parseable JSON, SQLite integrity checks, or
a native inspection command. Stream large artifacts to disk and avoid loading
an artifact-sized response into memory.

## 6. Implement a recoverable restore

Choose the honest capability:

- `automatic`: the plugin completes and verifies the restore;
- `partial`: the plugin performs the strongest safe workflow, but a documented
  vendor limitation prevents complete proof.

Restore implementations receive a staged, hash-verified artifact from
`RestoreService`. They still validate the vendor payload before changing the
destination. A destructive restore must:

1. reject unsafe paths, links, archive members, and incompatible artifacts;
2. acquire only the minimum required access;
3. stage replacement state on the destination filesystem when applicable;
4. preserve the previous destination until the new state is validated;
5. roll back before restarting after any failure;
6. prove application readiness and restored content, not mere process existence;
7. return `{"status": "success"}`, `{"status": "partial"}`, or
   `{"status": "failed"}` with a secret-safe message.

Backups and restores for the same target are centrally serialized. Do not add a
second plugin-local locking scheme unless the vendor has a cross-target shared
resource and the need is demonstrated by a concurrency test.

## 7. Cover the contract with tests

At minimum, cover:

- valid and invalid configuration;
- successful connectivity and each meaningful auth/protocol failure;
- discovery and schema API behavior;
- unique transactional artifact publication and valid sidecar;
- rejection of empty, malformed, incomplete, or malicious payloads;
- successful isolated restore with independent content/readiness proof;
- rollback or safe failure at each destructive boundary;
- timeouts, cancellation, and concurrent use when relevant;
- secret redaction in errors and logs.

Also cover `get_status()` when it performs I/O or derives a meaningful vendor
state. A minimal implementation may report `unknown` when the vendor has no
safe status endpoint; it may report `ok` only after an actual check.

Use the repository virtual environment:

```bash
cd backend
.venv/bin/pytest -q tests/plugins/test_<plugin_key>_plugin.py
.venv/bin/pytest -q
.venv/bin/mypy app tests
.venv/bin/black --check app tests
.venv/bin/isort --check-only app tests
```

If the schema or frontend behavior changes, also run:

```bash
cd frontend
npm test -- --run
npm run lint
npm run build
```

## 8. Verify discovery and perform drills

With a local backend running, verify the public API:

```bash
curl -fsS http://localhost:8080/api/v1/plugins/
curl -fsS http://localhost:8080/api/v1/plugins/<plugin_key>/schema
curl -fsS -X POST http://localhost:8080/api/v1/plugins/<plugin_key>/test \
  -H 'content-type: application/json' \
  --data '{"base_url":"http://service.local","api_key":"example"}'
```

Run two consecutive local drills against fresh isolated destinations. For each
drill, retain evidence of:

- distinct artifact path and non-zero size;
- sidecar path, plugin key, target slug, and creation time;
- independently calculated SHA-256;
- vendor-level artifact inspection;
- restored marker/content equality;
- destination restart or readiness proof where applicable.

After deployment, production work remains backup-only. Creating targets,
changing configuration, stopping a service, or granting broader access requires
explicit approval.

## Review and release

Review each plugin milestone against both this contract and its service-specific
plan. Keep the diff focused, update `docs/PLUGIN_COMPATIBILITY.md`, and record
the exact tested version and evidence. Follow `CONTRIBUTING.md` for repository
checks, Semantic Versioning, changelog, tag, and image publication.
