## AGENTS.md — Operating Guide for Coding Agents

This repository is agent-ready. Use this document as your entry point for rules, conventions, and task workflows.

### Key docs you will need
- Adding plugins (canonical, step-by-step): `ADDING_PLUGINS.md`
- Contributing workflow: `CONTRIBUTING.md`
- Backend readme: `backend/README.md`
- Frontend readme: `frontend/README.md`

If you are asked to add a new backup plugin, follow `ADDING_PLUGINS.md` exactly. It defines the authoritative interfaces, discovery contract, schema expectations, and required tests.

### Working style and expectations
- Prefer small, focused edits with clear diff scopes.
- Write tests first for new behavior; mock external IO and networks.
- Keep changes simple and deterministic; avoid unnecessary abstractions.
- Do not log secrets; redact tokens/passwords in all logs and messages.
- For backup artifacts, use `create_backup_artifact()` or `write_backup_bytes()`
  from `app.core.plugins.artifacts`. These helpers publish a non-empty artifact
  atomically under `/backups/<target_slug>/<YYYY-MM-DD>/` and write its sidecar.
  Return `{ "artifact_path": "..." }` only after the helper completes.
- Every new backup capability must include a restore workflow and declare an
  accurate `restore_capability`. Prove restores only against an isolated local
  destination; production restores are forbidden.

### Backend specifics (high level)
- **Virtual environment required**: When running backend commands (e.g. `pip`, `pytest`), always use a venv to avoid externally-managed-environment errors. Create with `python3 -m venv .venv` in `backend/`, then use `.venv/bin/pip` and `.venv/bin/pytest`, or activate first. Full steps: `backend/README.md` (Development and testing).
- Plugin contract and discovery are defined under `backend/app/core/plugins/` and `backend/app/plugins/`.
- Tests live in `backend/tests/` and use `pytest`/`pytest-asyncio`. Prefer `httpx.MockTransport` for HTTP-based plugins.

### Plugin connectivity failures

`test()` returns `True` only after complete success and raises a specific,
user-facing exception for every failure. The canonical exception mapping,
redaction rules, and test examples live in `ADDING_PLUGINS.md`; keep them in one
place so the API and target form receive useful errors without documentation
drift.

### Frontend specifics (high level)
- The Targets UI renders plugin config forms from each plugin's `schema.json`.
- Keep schemas flat and simple; use titles, defaults as hints, and required fields where appropriate.

### Typical tasks and where to look
- Add a new backup plugin: See `ADDING_PLUGINS.md` (includes scaffolding, schema, tests, and artifact conventions).
- Update plugin schema/UI: Update the plugin's `schema.json` and validate rendering in `frontend/src/pages/Targets.tsx`.
- Extend backend APIs: Follow patterns in `backend/app/api/` and ensure tests cover new routes.
- Maintenance jobs: Scheduled maintenance tasks (e.g., retention cleanup) are tracked separately from backup jobs. See `backend/app/models/maintenance.py`, `backend/app/services/maintenance.py`, and `backend/app/api/maintenance.py`. Maintenance jobs use deterministic `key` identifiers (never hardcode numeric IDs).

### PR readiness checklist
- Frontend lint/build and backend mypy/Black/isort checks pass.
- New or changed behavior is covered by tests; all tests pass locally.
- No secrets in code, logs, or docs.
- For plugins: discovery works, schema is returned by the API, `test` is non-destructive, `backup` writes an artifact to the correct path and returns it, and sidecar metadata is written for disaster recovery.

### Notes for automation
- When invoking tools or commands programmatically, prefer absolute paths for reliability.

Keep this file up to date when workflows or conventions change so agents can operate autonomously and safely.
