# Homelab Backup Backend

FastAPI-based backup system with plugin architecture.

## Setup

Use a **virtual environment** for local development and testing (required on many systems to avoid "externally-managed-environment" errors).

1. Create and activate a venv (from the `backend/` directory):
```bash
python3 -m venv .venv
source .venv/bin/activate   # Linux/macOS; on Windows: .venv\Scripts\activate
```

2. Install dependencies:
```bash
pip install -e .
```

For running tests you need dev extras:
```bash
pip install -e ".[dev]"
```

3. Run the application:
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

## Development and testing

- **Tests** live in `tests/` and use pytest. Always use the project venv so `pip`/`pytest` are not system-managed.
- Create venv once, then install and run:

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

Or after activating (e.g. `source .venv/bin/activate`): `pip install -e ".[dev]"` and `pytest -q`.

## Docker

Build and run locally:
```bash
docker build -t homelab-backend:dev .
docker run --rm -p 8080:8080 homelab-backend:dev
```

Or use the published image:
```bash
docker pull tarkilhk/homelab-backup:backend-latest
docker run --rm -p 8080:8080 tarkilhk/homelab-backup:backend-latest
```

## API Endpoints

- `GET /health` - Health check
- `GET /ready` - Readiness check
- `GET /metrics` - Prometheus metrics
- `GET /api/docs` - Swagger UI
- `GET /api/openapi.json` - OpenAPI schema

Versioned application APIs are mounted under `/api/v1` (e.g., `/api/v1/targets`, `/api/v1/jobs`, `/api/v1/runs`, `/api/v1/maintenance`).

## Features

- FastAPI with automatic OpenAPI documentation
- APScheduler with Asia/Singapore timezone (unified scheduling for backup and maintenance jobs)
- SQLAlchemy with SQLite database
- Plugin architecture for backup operations
- Maintenance job scheduling and execution history tracking
- Retention cleanup with configurable policies
- CORS middleware enabled
