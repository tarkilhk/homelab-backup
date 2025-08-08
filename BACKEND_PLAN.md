# Backend Plan (Cursor-first)

## Tech
Python 3.11+, FastAPI, APScheduler, SQLAlchemy, httpx

## Structure
backend/
  app/
    main.py
    api/ (targets, jobs, runs, artifacts, health, metrics)
    core/ (db, scheduler, plugins/base.py, plugins/loader.py, util/)
    models/ (SQLAlchemy)
    schemas/ (Pydantic)
    plugins/ (pihole, postgres, invoiceninja, prometheus, grafana, radarr)
  tests/

## Steps
1. Bootstrap FastAPI + health.
2. Add SQLite models and Pydantic schemas; CRUD for targets/jobs/runs.
3. Add APScheduler with cron support; Asia/Singapore TZ.
4. Implement plugin loader; wire a Pi-hole plugin (skeleton) and `Run now`.
5. Implement Prometheus `/metrics`.
6. Add email notifier on failure (simple SMTP env vars).
7. Compose file and env to mount NAS.
