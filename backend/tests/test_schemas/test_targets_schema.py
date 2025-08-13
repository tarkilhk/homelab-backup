from __future__ import annotations

from datetime import datetime, timezone
import pytest
from pydantic import ValidationError

from app.schemas import TargetCreate, TargetUpdate, Target


def test_target_create_schema() -> None:
    data = {
        "name": "Pi-hole",
        "slug": "pihole",
        "plugin_name": "pihole",
        "plugin_config_json": "{}",
    }
    target = TargetCreate(**data)
    assert target.name == "Pi-hole"
    assert target.plugin_name == "pihole"


def test_target_update_schema() -> None:
    update = TargetUpdate(name="New Name")
    assert update.name == "New Name"
    assert update.plugin_name is None


def test_target_response_schema() -> None:
    now = datetime.now(timezone.utc)
    data = {
        "id": 1,
        "name": "Pi-hole",
        "slug": "pihole",
        "plugin_name": "pihole",
        "plugin_config_json": "{}",
        "created_at": now,
        "updated_at": now,
    }
    target = Target(**data)
    assert target.id == 1
    assert target.created_at == now


def test_invalid_target_data_validation() -> None:
    with pytest.raises(ValidationError):
        # Missing plugin fields should raise in TargetCreate
        TargetCreate(name="X")  # type: ignore[call-arg]


