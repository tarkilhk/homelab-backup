from pathlib import Path

from app.core.plugins.servarr import ServarrPlugin


class RadarrPlugin(ServarrPlugin):
    app_name = "Radarr"
    api_prefix = "/api/v3"
    database_members = ("radarr.db",)
    expected_version = "6.3.0.10514"
    expected_package_version = "6.3.0.10514-ls313"
    expected_migration = 242
    require_start_time = True
    require_distinct_trigger_second = True
    require_persistence_restart = True
    native_backup_mount = Path("/sources/radarr/backups")
    restore_content_tables = (
        ("tag", "Tags"),
        ("rootfolder", "RootFolders"),
        ("indexer", "Indexers"),
        ("downloadclient", "DownloadClients"),
        ("notification", "Notifications"),
        ("movie", "Movies"),
    )
    required_native_tables = frozenset(
        {
            "VersionInfo",
            "Config",
            "RootFolders",
            "Indexers",
            "DownloadClients",
            "Notifications",
            "Tags",
            "Movies",
            "MovieMetadata",
            "MovieFiles",
            "QualityProfiles",
            "CustomFormats",
            "ImportLists",
            "History",
        }
    )
