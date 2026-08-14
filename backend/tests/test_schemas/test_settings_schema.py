from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.schemas import JobCreate, SettingsUpdate


@pytest.mark.parametrize(
    "policy",
    [
        "not-json",
        json.dumps({"rules": [{"unit": "century", "window": 1, "keep": 1}]}),
        json.dumps({"rules": [{"unit": "day", "window": 0, "keep": 1}]}),
        json.dumps({"rules": [{"unit": "day", "window": 1, "keep": 0}]}),
    ],
)
def test_settings_reject_unsafe_retention_policy(policy: str) -> None:
    with pytest.raises(ValidationError):
        SettingsUpdate(global_retention_policy_json=policy)


def test_job_create_rejects_unsafe_retention_policy() -> None:
    with pytest.raises(ValidationError):
        JobCreate(
            tag_id=1,
            name="unsafe",
            schedule_cron="0 2 * * *",
            retention_policy_json=json.dumps({"rules": [{"unit": "day", "window": -7, "keep": 1}]}),
        )


def test_retention_policy_is_normalized_at_the_write_boundary() -> None:
    payload = SettingsUpdate(
        global_retention_policy_json=' { "rules" : [ { "unit": "day", "window": 7, "keep": 1 } ] } '
    )

    assert payload.global_retention_policy_json == (
        '{"rules":[{"unit":"day","window":7,"keep":1}]}'
    )
