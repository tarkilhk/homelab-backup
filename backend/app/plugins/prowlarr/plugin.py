from pathlib import Path

from app.core.plugins.servarr import ServarrPlugin


class ProwlarrPlugin(ServarrPlugin):
    app_name = "Prowlarr"
    api_prefix = "/api/v1"
    database_members = ("prowlarr.db",)
    expected_version = "2.4.0.5397"
    expected_migration = 44
    command_result_required = False
    native_backup_mount = Path("/sources/prowlarr/backups")
    fresh_restore_resource_paths = (
        "tag",
        "indexer",
        "downloadclient",
        "applications",
        "notification",
    )
    required_native_tables = frozenset(
        {
            "Config",
            "Indexers",
            "DownloadClients",
            "Notifications",
            "IndexerProxies",
            "Applications",
            "ApplicationIndexerMapping",
            "Tags",
            "AppSyncProfiles",
            "History",
        }
    )
