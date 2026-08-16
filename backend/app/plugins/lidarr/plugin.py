from pathlib import Path

from app.core.plugins.servarr import ServarrPlugin


class LidarrPlugin(ServarrPlugin):
    app_name = "Lidarr"
    api_prefix = "/api/v1"
    database_members = ("lidarr.db",)
    expected_version = "3.1.0.4875"
    expected_package_version = "3.1.0.4875-ls38"
    expected_migration = 80
    require_start_time = True
    require_distinct_trigger_second = True
    require_persistence_restart = True
    native_backup_mount = Path("/sources/lidarr/backups")
    restore_content_tables = (
        ("tag", "Tags"),
        ("rootfolder", "RootFolders"),
        ("indexer", "Indexers"),
        ("downloadclient", "DownloadClients"),
        ("notification", "Notifications"),
        ("artist", "Artists"),
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
            "Artists",
            "ArtistMetadata",
            "Albums",
            "AlbumReleases",
            "Tracks",
            "TrackFiles",
            "QualityProfiles",
            "MetadataProfiles",
            "CustomFormats",
            "ImportLists",
            "History",
        }
    )
