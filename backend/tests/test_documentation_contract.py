from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_plugin_authoring_guide_matches_the_runtime_contract() -> None:
    """The canonical guide must lead contributors through the safe public seams."""

    guide = (REPOSITORY_ROOT / "ADDING_PLUGINS.md").read_text(encoding="utf-8")
    agent_guide = (REPOSITORY_ROOT / "AGENTS.md").read_text(encoding="utf-8")

    required_contract = (
        "create_backup_artifact",
        "write_backup_bytes",
        "restore_capability",
        "get_status()",
        "/api/v1/plugins",
        ".venv/bin/pytest",
        "two consecutive",
        "isolated",
    )
    for requirement in required_contract:
        assert requirement in guide

    assert "return False" not in guide
    assert '{"status": "not_implemented"}' not in guide
    assert "write_backup_sidecar(artifact_path" not in guide
    assert "create_backup_artifact" in agent_guide
    assert "write_backup_bytes" in agent_guide


def test_plugin_compatibility_map_does_not_report_pre_release_state() -> None:
    compatibility = (REPOSITORY_ROOT / "docs" / "PLUGIN_COMPATIBILITY.md").read_text(
        encoding="utf-8"
    )

    assert "repaired image has not yet been deployed" not in compatibility
    assert "Jellyfin and WordPress cannot be configured there yet" not in compatibility
    assert "Current program scope excludes WordPress" in compatibility
    assert "production restore" in compatibility.lower()
