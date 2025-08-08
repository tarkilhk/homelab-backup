# Architecture Overview (v0)

## Components
- **Backend**: FastAPI + APScheduler + SQLite. Plugins loaded from `backend/app/plugins`.
- **Frontend**: React + Vite + TypeScript + shadcn/ui.
- **Storage**: NAS mounted on host; bind-mounted at `/backups` in backend container.
- **Observability**: Prometheus `/metrics`, JSON logs to stdout (to be scraped by Loki/Alloy).

## Data Flow (Backup)
1. Scheduler triggers Job.
2. Backend loads plugin and target config.
3. Plugin performs native export and writes artifact.
4. Backend records run in DB, calculates checksum (later), updates metrics.
5. On failure, send email.

## DB (SQLite)
- `targets(id, name, slug, type, config_json, created_at, updated_at)`
- `jobs(id, target_id, name, schedule_cron, enabled, plugin, created_at, updated_at)`
- `runs(id, job_id, started_at, finished_at, status, message, artifact_path, artifact_bytes, sha256, logs_text)`

## Security
- LAN-only; HAProxy terminates TLS. Secrets via `.env` and minimal obfuscation in DB.

## Deployment
- Docker Compose: backend + frontend. Reverse proxy in front.
