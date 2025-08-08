"""Plugin loader and dynamic registry.

- Discovers plugins by scanning `app/plugins/*` for Python packages
  and importing any class that subclasses `BackupPlugin`.
- Registry maps plugin keys (folder names) to plugin classes.
- `get_plugin(name)` returns an instantiated plugin.
"""

from __future__ import annotations

from typing import Dict, Type, List, Optional, Iterable

import importlib
import inspect
import logging
import os

from app.core.plugins.base import BackupPlugin


_REGISTRY: Dict[str, Type[BackupPlugin]] = {}


logger = logging.getLogger(__name__)


def _iter_subclasses_in_module(module) -> Iterable[Type[BackupPlugin]]:
    """Yield all `BackupPlugin` subclasses defined or re-exported by a module."""
    for _, obj in inspect.getmembers(module, inspect.isclass):
        try:
            if issubclass(obj, BackupPlugin) and obj is not BackupPlugin:
                yield obj
        except Exception:
            # Non-type or weird objects; ignore
            continue


def _discover_plugins() -> Dict[str, Type[BackupPlugin]]:
    """Scan `app/plugins/*` directories and build a registry.

    Discovery strategy:
    - Treat each immediate child directory under `app/plugins/` that contains
      an `__init__.py` as a plugin key.
    - Import `app.plugins.<key>`; search for subclasses of `BackupPlugin` in
      the package module. If none are found, attempt `app.plugins.<key>.plugin`.
    - If multiple candidates exist, prefer a class whose name ends with
      "Plugin"; otherwise pick the first deterministically (sorted by name).
    - Swallow import errors for individual plugins, logging at debug level.
    """
    registry: Dict[str, Type[BackupPlugin]] = {}

    # Compute absolute path to `app/plugins`
    app_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    plugins_dir = os.path.join(app_root, "plugins")

    if not os.path.isdir(plugins_dir):
        return registry

    for entry in sorted(os.listdir(plugins_dir)):
        key_path = os.path.join(plugins_dir, entry)
        if not os.path.isdir(key_path):
            continue
        if not os.path.exists(os.path.join(key_path, "__init__.py")):
            continue

        key = entry
        module_base = f"app.plugins.{key}"

        candidates: List[Type[BackupPlugin]] = []
        try:
            pkg = importlib.import_module(module_base)
            candidates.extend(list(_iter_subclasses_in_module(pkg)))
        except Exception as exc:
            logger.debug("Failed importing %s: %s", module_base, exc)
            continue

        # If not found on package level, try conventional `.plugin` module
        if not candidates:
            try:
                mod = importlib.import_module(f"{module_base}.plugin")
                candidates.extend(list(_iter_subclasses_in_module(mod)))
            except Exception as exc:
                logger.debug("Failed importing %s.plugin: %s", module_base, exc)
                continue

        if not candidates:
            # No plugin classes discovered for this key
            continue

        # Prefer classes with *Plugin suffix; stable selection by name
        candidates = sorted(candidates, key=lambda c: c.__name__)
        preferred = [c for c in candidates if c.__name__.endswith("Plugin")]
        cls = preferred[0] if preferred else candidates[0]

        registry[key] = cls

    return registry


def refresh_registry() -> None:
    """Re-scan the plugins directory and rebuild the registry."""
    global _REGISTRY
    _REGISTRY = _discover_plugins()


def get_plugin(name: str) -> BackupPlugin:
    """Instantiate a plugin by registry name.

    Raises KeyError if not found.
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        # Try refreshing registry in case plugins were added at runtime
        refresh_registry()
        cls = _REGISTRY.get(name)
        if cls is None:
            raise KeyError(f"Unknown plugin: {name}")
    return cls(name=name)


def list_plugins() -> List[dict]:
    """Return a list of available plugins with basic info.

    Each item: {"key": str, "name": str, "version": str}
    """
    # Always refresh on list to reflect filesystem changes
    refresh_registry()
    plugins: List[dict] = []
    for key, cls in sorted(_REGISTRY.items()):
        try:
            instance = cls(name=key)
            info = instance.get_info()
            plugins.append({
                "key": key,
                "name": info.get("name", key),
                "version": info.get("version", "unknown"),
            })
        except Exception:
            plugins.append({"key": key, "name": key, "version": "unknown"})
    return plugins


def get_plugin_schema_path(key: str) -> Optional[str]:
    """Return absolute schema.json path for the plugin if present."""
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    schema_path = os.path.join(base_dir, "plugins", key, "schema.json")
    return schema_path if os.path.exists(schema_path) else None


# Perform initial discovery on import
refresh_registry()


