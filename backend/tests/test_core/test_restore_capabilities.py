"""Restore capability declarations must match actual plugin behavior."""

from app.core.plugins.loader import get_plugin, list_plugins


def test_plugins_publish_honest_restore_capabilities() -> None:
    expected = {
        "calcom": "automatic",
        "gitea": "automatic",
        "homelab_backup": "partial",
        "mysql": "partial",
        "postgresql": "automatic",
        "vaultwarden": "automatic",
        "wordpress": "automatic",
        "invoiceninja": "partial",
        "jellyfin": "automatic",
        "lidarr": "automatic",
        "pihole": "automatic",
        "radarr": "automatic",
        "sftpgo": "partial",
        "sonarr": "automatic",
    }

    listed = {item["key"]: item["restore_capability"] for item in list_plugins()}
    assert listed == expected
    for key, capability in expected.items():
        assert get_plugin(key).restore_capability == capability
