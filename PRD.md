# Homelab Backup Orchestrator — PRD (v0)

**Date:** 2025-08-08  
**Owner:** You  
**License:** Apache-2.0

## Context
Homelab with ~70 containers + some external services (Pi-hole on RPi, pfSense, etc.) running behind HAProxy on Proxmox. Goal is to have a single tool orchestrating **app‑native backups** to a NAS.

## MVP Goals
- Web UI + API to configure **Targets** and **Jobs** (daily/weekly cron-like).
- **Plugins** per app (Pi-hole, Postgres, Invoice Ninja, Prometheus, Grafana, Radarr).
- Store artifacts under `/backups/<target>/<YYYY-MM-DD>/...` (NAS mounted on host and bind-mounted).
- Basic validation (file exists, non-zero size) + run logs + statuses.
- Email notifications **on failures**.

## Non-Goals (v0)
- Proxmox/ZFS snapshots, retention, compression, encryption, auth/OIDC, offsite, restore automation.

## Target Services (v0)
- **Pi-hole**: native API export.
- **Postgres**: `pg_dump -Fc` over network.
- **Invoice Ninja**: built-in export API if present; else DB+files fallback.
- **Prometheus**: config export; optional TSDB snapshot (feature flag).
- **Grafana**: dashboards & datasources via API.
- **Radarr**: built-in backup endpoint if available; else config files.

## Requirements
### Functional
- CRUD for Targets/Jobs; Manual “Run now”; View Runs and artifacts.
- Plugins prefer API/native export; fallback per-case.
- Prometheus metrics: job_success_total, job_failure_total, last_run_timestamp.
- Email notifier on job failure.

### Non-functional
- FastAPI backend + APScheduler; React frontend.
- SQLite for state; single Docker compose deployment.
- LAN-only via HAProxy; secrets via `.env` on VM.

## Constraints
- Timezone: Asia/Singapore.
- No resource limits; NAS ≈ 50 TB.
- License: Apache-2.0.

## Success Metrics
- ≥95% scheduled jobs execute on-time with successful artifact creation.
- Operator can see last run, next run, and errors per service at a glance.

## Open Items (tracked for later)
- Retention/Compression/Encryption
- Automated restore; restore-to-fresh-instance
- Multi-channel notifications (Discord/Matrix/etc.)
