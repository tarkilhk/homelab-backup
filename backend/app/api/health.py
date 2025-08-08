"""Health check API router."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> dict[str, str]:
    """Readiness check endpoint."""
    return {"status": "ready"}
