from pathlib import Path

from app.core.plugins.servarr import ServarrPlugin


class ReadarrPlugin(ServarrPlugin):
    app_name = "Readarr"
    api_prefix = "/api/v1"
    database_members = ("readarr.db",)
    expected_version = "0.4.18.2805"
    expected_migration = 158
    native_backup_mount = Path("/sources/readarr/backups")
    fresh_restore_resource_paths = (
        "tag",
        "rootfolder",
        "indexer",
        "downloadclient",
        "notification",
    )
    required_native_tables = frozenset(
        {
            "Config",
            "RootFolders",
            "Indexers",
            "DownloadClients",
            "Notifications",
            "Tags",
            "Authors",
            "AuthorMetadata",
            "Books",
            "Editions",
            "BookFiles",
            "QualityProfiles",
            "MetadataProfiles",
            "History",
        }
    )
