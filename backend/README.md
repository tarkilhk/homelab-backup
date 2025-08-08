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

Build and run with Docker:
```bash
docker build -t homelab-backup .
docker run -p 8080:8080 homelab-backup
```

## API Endpoints

- `GET /healthz` - Health check
- `GET /api/v1/health` - Health check (API versioned)
- `GET /api/v1/ready` - Readiness check

## Features

- FastAPI with automatic OpenAPI documentation
- APScheduler with Asia/Singapore timezone
- SQLAlchemy with SQLite database
- Plugin architecture for backup operations
- CORS middleware enabled
