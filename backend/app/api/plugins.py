"""Plugins discovery API."""

from __future__ import annotations

import json
from typing import List, Dict, Any

from fastapi import APIRouter, HTTPException, Body
from fastapi.responses import JSONResponse

from app.core.plugins.loader import list_plugins, get_plugin_schema_path, get_plugin


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


@router.post("/{key}/test")
async def test_plugin_connectivity(key: str, config: Dict[str, Any] = Body(...)) -> JSONResponse:
    """Invoke the plugin's test method with provided configuration and return {"ok": bool}."""
    try:
        plugin = get_plugin(key)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown plugin")

    try:
        ok = await plugin.test(config)
    except Exception as exc:  # pragma: no cover - defensive
        # Do not leak secrets; return generic failure with message
        return JSONResponse(content={"ok": False, "error": str(exc)})

    if not ok:
        return JSONResponse(content={"ok": False, "error": "Connection test failed"})
    return JSONResponse(content={"ok": True})


