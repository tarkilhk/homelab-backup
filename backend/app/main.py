"""Main FastAPI application for homelab backup system."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.scheduler import get_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager."""
    # Startup
    scheduler = get_scheduler()
    scheduler.start()
    print("APScheduler started with Asia/Singapore timezone")
    
    yield
    
    # Shutdown
    scheduler.shutdown()
    print("APScheduler shutdown")


app = FastAPI(
    title="Homelab Backup API",
    description="Backup system with plugin architecture",
    version="0.1.0",
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
from app.api import health

app.include_router(health.router, prefix="/api/v1")


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(app, host="0.0.0.0", port=8080)
