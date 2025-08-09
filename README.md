# Homelab Backup Orchestrator (Starter Pack)

Starter repository layout for building a pluggable backup orchestrator for homelab services.

## What’s inside
- **PRD.md** — full product spec and constraints
- **ARCHITECTURE.md** — components and data flow
- **PLUGIN_SPEC.md** — how to write plugins (Pi-hole, Postgres, etc.)
- **BACKEND_PLAN.md** — step-by-step plan to scaffold FastAPI + APScheduler
- **FRONTEND_PLAN.md** — plan to scaffold React + Vite + shadcn/ui
- **cursor.rules.backend.yml** — Cursor rules for backend generation
- **cursor.rules.frontend.yml** — Cursor rules for frontend generation
- **TEST_PLAN.md**, **CONTRIBUTING.md**
- **deploy/docker-compose.yml** + **.env.example**

## Quickstart
1. Mount your NAS on the Docker host (e.g., `/mnt/nas/backups`).
2. Copy `deploy/.env.example` to `deploy/.env` and set `BACKUP_ROOT_HOST`.
3. `docker compose -f deploy/docker-compose.yml up -d --build`
4. Open the UI and add services/jobs.

## License
GPL-3.0-or-later (see LICENSE).
