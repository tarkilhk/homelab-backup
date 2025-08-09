# Homelab Backup Orchestrator

Pluggable backup orchestrator for homelab services. Define targets (Pi-hole, databases, apps), schedule jobs, and store artifacts in a consistent directory layout on your NAS or local disk.

## Features
- FastAPI backend with SQLite persistence and APScheduler
- Pluggable architecture: add new backup plugins easily
- React/Vite frontend for managing targets, jobs, and runs
- Prometheus metrics at `/metrics` (success/failure counts, last run timestamp)

## Deployment (Docker Compose)

Prerequisites:
- Docker and Docker Compose
- A host directory for backup artifacts (e.g., `/mnt/nas/backups`)

1) Create `deploy/.env` with your settings:

```env
TZ=Etc/UTC
# REQUIRED: host directory to store backups
BACKUP_ROOT_HOST=/mnt/nas/backups

# Optional
LOG_LEVEL=INFO
DB_FOLDER=/app/db
```

2) Start the stack:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

3) Access the services:
- UI: `http://localhost:8081`
- API docs (Swagger): `http://localhost:8080/api/docs`
- Health: `http://localhost:8080/health` and `http://localhost:8080/ready`
- Metrics: `http://localhost:8080/metrics`

To stop:

```bash
docker compose -f deploy/docker-compose.yml down
```

## Configuration

Environment variables consumed by the compose stack/backend:
- `TZ`: container timezone
- `BACKUP_ROOT_HOST`: host path mounted to `/backups` in the backend container
- `LOG_LEVEL`: backend log level (DEBUG/INFO/WARNING/ERROR)
- `DB_FOLDER`: backend path (inside container) where the SQLite DB directory lives; the compose file maps a host directory to `/app/db`

Volumes and persistence:
- Backups are written under `/backups/<target-slug>/<YYYY-MM-DD>/...` in the backend container, which maps to `BACKUP_ROOT_HOST` on the host.
- The SQLite database is stored under `DB_FOLDER` (default `/app/db`) and is volume-mapped by the compose file.

## Basic usage
1. Open the UI at `http://localhost:8081` and create a Target.
2. Choose a plugin and fill the configuration form.
3. Create a Job referencing the Target and set a schedule.
4. Run the Job manually or wait for the scheduler.
5. Verify artifacts in your host backup directory.

Prometheus metrics include per-job successes/failures and last-run timestamp. See `/metrics` for details.

## Plugins
The system supports adding new plugins to back up different services. See `ADDING_PLUGINS.md` for the canonical contract and a complete, step-by-step guide.

## Project structure
- `backend/`: FastAPI app, APScheduler, SQLite via SQLAlchemy
- `frontend/`: React + Vite UI
- `deploy/`: `docker-compose.yml` for local/NAS deployment
- `db/` and `db_default/`: local database folders (mapped in compose)

## License
GPL-3.0-or-later (see `LICENSE`).
