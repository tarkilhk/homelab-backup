# Homelab Backup

Pluggable backup orchestrator for homelab services. Define targets (Pi-hole, databases, apps), schedule jobs, and store artifacts in a consistent directory layout on your NAS or local disk.

## Features
- FastAPI backend with SQLite persistence and APScheduler
- Pluggable architecture: add new backup plugins easily
- React/Vite frontend for managing targets, jobs, and runs
- Maintenance job scheduling and execution history tracking
- Retention cleanup with configurable policies
- Prometheus metrics at `/metrics` (success/failure counts, last run timestamp)

## Deployment (Docker Compose)

Prerequisites:
- Docker and Docker Compose
- A host directory for backup artifacts (e.g., `/mnt/nas/backups`)

Compose and configuration files:
- Compose file: [deploy/docker-compose.yml](deploy/docker-compose.yml)
- Env sample: [deploy/.env.sample](deploy/.env.sample)
- Environment file: `deploy/.env` (see sample below)

### Images on Docker Hub
- Repository: [`tarkilhk/homelab-backup`](https://hub.docker.com/r/tarkilhk/homelab-backup)
- Tags used by CI:
  - Backend: `backend-latest`, `backend-vX.Y.Z`, `backend-sha-<shortsha>`
  - Frontend: `frontend-latest`, `frontend-vX.Y.Z`, `frontend-sha-<shortsha>`

Use Docker Compose to manage images and containers; it will pull tags as needed.

The compose file references these tags. You can pin a specific tag by editing `deploy/docker-compose.yml` and changing the `image` tags.

1) Create `deploy/.env` with your settings (or copy the sample first):

```env
TZ=Etc/UTC

# Backend options
LOG_LEVEL=INFO
LOG_SQL_ECHO=0

# Optional: SMTP notifications
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
SMTP_FROM=
SMTP_TO=
SMTP_STARTTLS=true

# Frontend → Backend connection
# The frontend proxies `/api/*` to this origin. For Compose, the default works.
# Examples: http://backend:8080 (Compose service), http://192.168.1.50:8080, https://api.example.com
BACKEND_ORIGIN=http://backend:8080
```

Reminder: Within Docker Compose, services talk to each other via the service name and the container port. Set `BACKEND_ORIGIN` to `http://backend:8080` (service name + internal port), not to the host-mapped port (e.g., `http://backend:8086`). Using the host port from inside containers will lead to 502 errors from the frontend.

Note: The compose file maps the SQLite database directory with a host bind mount. Update the left-hand side of the mapping to a persistent path on your host if needed (defaults to the repository's `db/` folder).

2) Start the stack (Compose pulls images automatically):

```bash
docker compose -f deploy/docker-compose.yml up -d
```

Optionally, to build locally instead of pulling prebuilt images:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

3) Access the services:
- UI: `http://localhost:8081`
- API docs (Swagger): `http://localhost:8080/api/docs`
- Health: `http://localhost:8080/health` and `http://localhost:8080/ready`
- Metrics: `http://localhost:8080/metrics`

Note: Always use Docker Compose to run this stack. The frontend relies on the compose network to reach the backend at `http://backend:8080` (see `frontend/nginx.conf`). Running containers individually with `docker run` will require recreating the same network and service names.

To stop:

```bash
docker compose -f deploy/docker-compose.yml down
```

## Configuration

Environment variables consumed by the compose stack/backend:
- `TZ`: container timezone
- `LOG_LEVEL`: backend log level (DEBUG/INFO/WARNING/ERROR)

Volumes and persistence:
- Backups are written under `/backups/<target-slug>/<YYYY-MM-DD>/...` inside the backend container. Bind mount your host directory to `/backups` in your compose file, for example:

```yaml
services:
  backend:
    volumes:
      - /mnt/nas/backups:/backups
      - ./db:/app/db
```
- The SQLite database is stored under `/app/db` inside the backend container. Mount any host directory you prefer to this path in the compose file.

## Basic usage
1. Open the UI at `http://<your-server-name>:8081` and create a Target.
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
- `db/`: local database folders (mapped in compose)

## License
GPL-3.0-or-later (see `LICENSE`).
