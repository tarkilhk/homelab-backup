"""Plugins discovery API."""

from __future__ import annotations

import json
from typing import List

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.core.plugins.loader import list_plugins, get_plugin_schema_path


router = APIRouter(prefix="/plugins", tags=["plugins"])


@router.get("/", response_model=List[dict])
def list_available_plugins() -> List[dict]:
    """List plugins available in the backend registry."""
    return list_plugins()


@router.get("/{key}/schema")
def get_plugin_schema(key: str) -> JSONResponse:
    """Return the JSON schema for a plugin so the UI can render forms."""
    schema_path = get_plugin_schema_path(key)
    if not schema_path:
        raise HTTPException(status_code=404, detail="Schema not found for plugin")
    with open(schema_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return JSONResponse(content=data)


