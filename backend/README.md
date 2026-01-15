# Homelab Backup Backend

FastAPI-based backup system with plugin architecture.

## Setup

1. Install dependencies:
```bash
pip install -e .
```

2. Run the application:
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

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
