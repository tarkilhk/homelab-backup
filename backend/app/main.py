"""Main FastAPI application for homelab backup system."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
import logging
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from app.core.db import init_db, bootstrap_db, SessionLocal
from app.core.logging import setup_logging
from app.core.scheduler import get_scheduler, schedule_jobs_on_startup


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager."""
    # Startup
    setup_logging()
    logger = logging.getLogger(__name__)
    init_db()
    bootstrap_db()
    
    scheduler = get_scheduler()
    # Schedule enabled jobs from DB before starting scheduler
    db = SessionLocal()
    try:
        schedule_jobs_on_startup(scheduler, db)
    finally:
        db.close()
    scheduler.start()
    logger.info("APScheduler started with Asia/Singapore timezone and jobs scheduled")
    
    yield
    
    # Shutdown
    scheduler.shutdown()
    logger.info("APScheduler shutdown")


app = FastAPI(
    title="Homelab Backup API",
    description="Backup system with plugin architecture",
    version="0.1.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
from app.api import health, targets, jobs, runs, plugins, metrics

# Mount health endpoints unversioned for infra probes (/health, /ready)
app.include_router(health.router)

# Mount application APIs under versioned prefix
app.include_router(targets.router, prefix="/api/v1")
app.include_router(jobs.router, prefix="/api/v1")
app.include_router(runs.router, prefix="/api/v1")
app.include_router(plugins.router, prefix="/api/v1")

# Prometheus metrics (unversioned)
app.include_router(metrics.router)


@app.get("/")
async def root() -> RedirectResponse:
    """Redirect root to Swagger UI."""
    return RedirectResponse(url="/api/docs")


# Readiness is provided via health router as /ready


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(app, host="0.0.0.0", port=8080)
