"""Main FastAPI application for homelab backup system."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
import logging
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import base64

from app.core.db import init_db, bootstrap_db, get_session, get_engine
import app.core.db as db_mod
from app.core.logging import setup_logging
from app.core.scheduler import get_scheduler, schedule_jobs_on_startup
from sqlalchemy.exc import IntegrityError


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager."""
    # Startup
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("App startup | configuring DB and scheduler")
    init_db()
    bootstrap_db()
    
    scheduler = get_scheduler()
    # Schedule enabled jobs from DB before starting scheduler
    # Prefer FastAPI dependency override for DB if present (used by tests)
    override = app.dependency_overrides.get(get_session)  # type: ignore[attr-defined]
    if override is not None:
        db = next(override())
        try:
            schedule_jobs_on_startup(scheduler, db)
        finally:
            db.close()
    else:
        # Ensure engine/session are initialized and use real DB
        get_engine()
        session_factory = db_mod.SessionLocal
        assert session_factory is not None
        db_session = session_factory()
        try:
            schedule_jobs_on_startup(scheduler, db_session)
        finally:
            db_session.close()
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
    docs_url=None,
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

# Mount backend static dir for assets like favicon
_static_dir = Path(__file__).resolve().parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

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
from app.api import tags, groups, restores, backups, settings, maintenance

# Mount health endpoints unversioned for infra probes (/health, /ready)
app.include_router(health.router)

# Mount application APIs under versioned prefix
app.include_router(targets.router, prefix="/api/v1")
app.include_router(jobs.router, prefix="/api/v1")
app.include_router(runs.router, prefix="/api/v1")
app.include_router(plugins.router, prefix="/api/v1")
app.include_router(tags.router, prefix="/api/v1")
app.include_router(groups.router, prefix="/api/v1")
app.include_router(restores.router, prefix="/api/v1")
app.include_router(backups.router, prefix="/api/v1")
app.include_router(settings.router, prefix="/api/v1")
app.include_router(maintenance.router, prefix="/api/v1")

# Prometheus metrics (unversioned)
app.include_router(metrics.router)


@app.get("/")
async def root() -> RedirectResponse:
    """Redirect root to Swagger UI."""
    return RedirectResponse(url="/api/docs")


# Custom Swagger UI with favicon pointing to /favicon.ico (served by the frontend/public)
from fastapi.openapi.docs import get_swagger_ui_html  # noqa: E402


@app.get("/api/docs", include_in_schema=False)
async def custom_swagger_ui_html():
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} — Docs",
        swagger_favicon_url="/static/favicon.ico",
    )

# Serve /favicon.ico for Swagger and other clients
@app.get("/favicon.ico", include_in_schema=False)
async def serve_favicon():
    # Prefer dev path (monorepo), then packaged static path
    candidates = [
        Path(__file__).resolve().parents[2] / "frontend" / "public" / "favicon.ico",
        Path(__file__).resolve().parent / "static" / "favicon.ico",
    ]
    for p in candidates:
        if p.exists():
            return FileResponse(p, media_type="image/x-icon")
    # Tiny 16x16 fallback ICO (green square) encoded inline
    fallback_b64 = (
        "AAABAAEAEBAAAAEAIABoBAAAFgAAACgAAAAQAAAAIAAAAAEAGAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A"
        "////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD/"
        "//8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP//"
        "/wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////"
        "AP///wD///8A////AP///wD///8A"
    )
    try:
        data = base64.b64decode(fallback_b64)
    except Exception:
        data = b""  # should not happen
    return Response(content=data, media_type="image/x-icon")

# Readiness is provided via health router as /ready


# Global exception handlers
@app.exception_handler(IntegrityError)
async def handle_integrity_error(_request: Request, exc: IntegrityError) -> JSONResponse:
    # Standardize DB integrity errors as 409 with readable message
    return JSONResponse(status_code=409, content={"detail": str(exc)})

if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(app, host="0.0.0.0", port=8080)
