# Plugin Specification (v0)

## Interface
```python
from dataclasses import dataclass
from typing import Any, Dict

@dataclass
class BackupContext:
    target_slug: str
    backup_root: str
    temp_dir: str
    now_iso: str
    logger: Any

class BackupPlugin:
    name: str
    version: str

    async def backup(self, target_cfg: Dict[str, Any], creds: Dict[str, Any], ctx: BackupContext) -> str:
        """Perform backup and return absolute artifact path."""

    async def validate_artifact(self, artifact_path: str, ctx: BackupContext) -> None:
        """Raise on failure. Default is existence + non-zero size."""
```

## Config Schema
Each plugin ships a `schema.json` used by the UI to render forms.
Minimal example:
```json
{ "type": "object", "required": ["base_url","token"], "properties": {
  "base_url": {"type":"string","format":"uri"},
  "token": {"type":"string"}
}}
```

## Conventions
- Write to: `/backups/<targetSlug>/<YYYY-MM-DD>/...`.
- Log with structured messages; avoid printing secrets.
- Detect target capability (API endpoints) and choose the best native export.
