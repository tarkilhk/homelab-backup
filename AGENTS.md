## AGENTS.md — Operating Guide for Coding Agents

This repository is agent-ready. Use this document as your entry point for rules, conventions, and task workflows.

### Start here: Cursor rules (must follow)
- Frontend rules: `frontend/.cursorrules`
- Backend rules: `backend/.cursorrules`

These two files define style, structure, and execution expectations used by Cursor. Always read and adhere to them before making edits.

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
- For backup artifacts, follow the convention `/backups/<target_slug>/<YYYY-MM-DD>/...` and return `{ "artifact_path": "..." }` from plugin backups.

### Backend specifics (high level)
- Plugin contract and discovery are defined under `backend/app/core/plugins/` and `backend/app/plugins/`.
- Tests live in `backend/tests/` and use `pytest`/`pytest-asyncio`. Prefer `httpx.MockTransport` for HTTP-based plugins.

### Frontend specifics (high level)
- The Targets UI renders plugin config forms from each plugin's `schema.json`.
- Keep schemas flat and simple; use titles, defaults as hints, and required fields where appropriate.

### Typical tasks and where to look
- Add a new backup plugin: See `ADDING_PLUGINS.md` (includes scaffolding, schema, tests, and artifact conventions).
- Update plugin schema/UI: Update the plugin's `schema.json` and validate rendering in `frontend/src/pages/Targets.tsx`.
- Extend backend APIs: Follow patterns in `backend/app/api/` and ensure tests cover new routes.

### PR readiness checklist
- You followed `frontend/.cursorrules` and `backend/.cursorrules`.
- New or changed behavior is covered by tests; all tests pass locally.
- No secrets in code, logs, or docs.
- For plugins: discovery works, schema is returned by the API, `test` is non-destructive, and `backup` writes an artifact to the correct path and returns it.

### Notes for automation
- When invoking tools or commands programmatically, prefer absolute paths for reliability.

Keep this file up to date when workflows or conventions change so agents can operate autonomously and safely.


