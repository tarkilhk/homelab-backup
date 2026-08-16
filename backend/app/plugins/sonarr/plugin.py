from pathlib import Path

from app.core.plugins.servarr import ServarrPlugin


class SonarrPlugin(ServarrPlugin):
    app_name = "Sonarr"
    api_prefix = "/api/v3"
    database_members = ("sonarr.db",)
    expected_version = "4.0.19.2979"
    expected_package_version = "4.0.19.2979-ls320"
    expected_migration = 217
    require_start_time = True
    require_distinct_trigger_second = True
    require_persistence_restart = True
    native_backup_mount = Path("/sources/sonarr/backups")
    restore_content_tables = (
        ("tag", "Tags"),
        ("rootfolder", "RootFolders"),
        ("indexer", "Indexers"),
        ("downloadclient", "DownloadClients"),
        ("notification", "Notifications"),
        ("series", "Series"),
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
            "Series",
            "Episodes",
            "EpisodeFiles",
            "QualityProfiles",
            "CustomFormats",
            "ImportLists",
            "History",
        }
    )
