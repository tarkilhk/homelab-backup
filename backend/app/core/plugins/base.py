"""Base classes for backup plugins."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class BackupContext:
    """Context information for backup operations."""
    
    job_id: str
    target_id: str
    config: Dict[str, Any]
    metadata: Optional[Dict[str, Any]] = None


class BackupPlugin(ABC):
    """Base class for all backup plugins."""
    
    def __init__(self, name: str, version: str = "1.0.0"):
        """Initialize plugin with name and version."""
        self.name = name
        self.version = version
    
    @abstractmethod
    async def validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate plugin configuration."""
        pass
    
    @abstractmethod
    async def test(self, config: Dict[str, Any]) -> bool:
        """Test connectivity to the target using the provided configuration."""
        pass
    
    @abstractmethod
    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        """Perform backup operation."""
        pass
    
    @abstractmethod
    async def restore(self, context: BackupContext) -> Dict[str, Any]:
        """Perform restore operation."""
        pass
    
    @abstractmethod
    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        """Get backup status."""
        pass
    
    def get_info(self) -> Dict[str, str]:
        """Get plugin information."""
        return {
            "name": self.name,
            "version": self.version,
            "type": self.__class__.__name__,
        }
