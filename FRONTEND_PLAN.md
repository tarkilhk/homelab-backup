# Frontend Plan (Cursor-first)

## Tech
React + Vite + TypeScript, Tailwind, shadcn/ui, react-query

## Structure
frontend/
  src/
    api/ (OpenAPI client)
    pages/ (Dashboard, Targets, Jobs, Runs, Settings)
    components/ (tables, forms)
    hooks/, utils/
  public/

## Steps
1. Scaffold Vite + Tailwind + shadcn/ui.
2. Layout + nav.
3. Targets page: list + create/edit from plugin JSON schema.
4. Jobs page: cron picker (daily/weekly/custom) + Run Now.
5. Runs page: history with status, logs, artifact links.
6. Basic toasts/errors, loading states.

## Routes
- `/targets/:id/schedule` — Create a scheduled Job for a specific Target. Form captures: job name, cron, plugin, plugin version, enabled. Submits to `POST /api/v1/jobs/`.
