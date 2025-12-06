"""Service for scanning and discovering backup artifacts on disk."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from sqlalchemy.orm import Session

from app.core.plugins.sidecar import read_backup_sidecar
from app.models import TargetRun


class BackupFromDisk:
    """Represents a backup artifact found on disk."""

    def __init__(
        self,
        *,
        artifact_path: str,
        target_slug: Optional[str] = None,
        date: Optional[str] = None,
        plugin_name: Optional[str] = None,
        file_size: int,
        modified_at: str,
        metadata_source: str = "inferred",  # "sidecar" or "inferred"
    ):
        self.artifact_path = artifact_path
        self.target_slug = target_slug
        self.date = date
        self.plugin_name = plugin_name
        self.file_size = file_size
        self.modified_at = modified_at
        self.metadata_source = metadata_source


class BackupsFromDiskService:
    """Service for scanning backup directory and discovering artifacts on disk."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def scan_backups(
        self,
        *,
        backup_base_path: Optional[str] = None,
    ) -> List[BackupFromDisk]:
        """Scan the backup directory and return all backup artifacts found on disk.
        
        Args:
            backup_base_path: Base path for backups (defaults to /backups or BACKUP_BASE_PATH env)
            
        Returns:
            List of BackupFromDisk objects representing artifacts found on disk
        """
        if backup_base_path is None:
            backup_base_path = os.environ.get("BACKUP_BASE_PATH", "/backups")
        
        if not os.path.exists(backup_base_path):
            return []
        
        artifacts: List[BackupFromDisk] = []
        
        # Walk the directory structure: /backups/<target_slug>/<YYYY-MM-DD>/...
        try:
            for target_slug_dir in Path(backup_base_path).iterdir():
                if not target_slug_dir.is_dir():
                    continue
                
                target_slug = target_slug_dir.name
                
                # Skip hidden directories and special directories
                if target_slug.startswith("."):
                    continue
                
                for date_dir in target_slug_dir.iterdir():
                    if not date_dir.is_dir():
                        continue
                    
                    date_str = date_dir.name
                    # Validate date format YYYY-MM-DD
                    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
                        continue
                    
                    # Scan for artifact files in this date directory
                    for file_path in date_dir.iterdir():
                        if not file_path.is_file():
                            continue
                        
                        # Skip sidecar files and hidden files
                        if file_path.name.endswith(".meta.json") or file_path.name.startswith("."):
                            continue
                        
                        artifact_path = str(file_path)
                        
                        # Check if this artifact is already tracked in the database
                        tracked = (
                            self.db.query(TargetRun)
                            .filter(TargetRun.artifact_path == artifact_path)
                            .first()
                        )
                        
                        if tracked is not None:
                            # This artifact is tracked, skip it
                            continue
                        
                        # Get file metadata
                        try:
                            stat = file_path.stat()
                            file_size = stat.st_size
                            modified_at = datetime.fromtimestamp(stat.st_mtime).isoformat()
                        except OSError:
                            continue
                        
                        # Try to read sidecar metadata first
                        sidecar_data = read_backup_sidecar(artifact_path)
                        
                        if sidecar_data:
                            # Use sidecar metadata
                            artifacts.append(
                                BackupFromDisk(
                                    artifact_path=artifact_path,
                                    target_slug=sidecar_data.get("target_slug") or target_slug,
                                    date=sidecar_data.get("created_at", "").split("T")[0] if sidecar_data.get("created_at") else date_str,
                                    plugin_name=sidecar_data.get("plugin_name"),
                                    file_size=file_size,
                                    modified_at=modified_at,
                                    metadata_source="sidecar",
                                )
                            )
                        else:
                            # Infer metadata from filename and path
                            inferred_plugin = self._infer_plugin_from_filename(file_path.name)
                            
                            artifacts.append(
                                BackupFromDisk(
                                    artifact_path=artifact_path,
                                    target_slug=target_slug,
                                    date=date_str,
                                    plugin_name=inferred_plugin,
                                    file_size=file_size,
                                    modified_at=modified_at,
                                    metadata_source="inferred",
                                )
                            )
        
        except PermissionError:
            # If we can't read the directory, return empty list
            pass
        except Exception:
            # Log but don't fail - return what we found so far
            pass
        
        return artifacts

    def _infer_plugin_from_filename(self, filename: str) -> Optional[str]:
        """Infer plugin name from filename pattern.
        
        Common patterns:
        - {plugin}-backup-{timestamp}.{ext}
        - {plugin}-dump-{timestamp}.{ext}
        - {plugin}-teleporter-{timestamp}.{ext}
        - {plugin}-export-{timestamp}.{ext}
        - {plugin}-db-{timestamp}.{ext}
        """
        # Remove extension
        name_without_ext = os.path.splitext(filename)[0]
        
        # Common patterns to match
        patterns = [
            r"^([a-z]+)-backup-",
            r"^([a-z]+)-dump-",
            r"^([a-z]+)-dumpall-",
            r"^([a-z]+)-teleporter-",
            r"^([a-z]+)-export-",
            r"^([a-z]+)-db-",
        ]
        
        for pattern in patterns:
            match = re.match(pattern, name_without_ext.lower())
            if match:
                plugin_candidate = match.group(1)
                # Validate against known plugin names
                known_plugins = {
                    "pihole", "postgresql", "mysql", "vaultwarden", "jellyfin",
                    "wordpress", "calcom", "sonarr", "lidarr", "radarr", "invoiceninja",
                }
                if plugin_candidate in known_plugins:
                    return plugin_candidate
        
        return None


