"""Prompt 05, points 8-10: where the panel moved and the contract did not.

The contract is what the two sides may say to each other. Where the panel now
does something the contract does not describe, the agent keeps to the contract
and these tests say so - and they turn red the day the contract changes, so the
agent is aligned then rather than forgotten.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from custom_components.haim_agent import bounds
from custom_components.haim_agent.api import ApiResponse

from test_coordinator import coordinator  # noqa: F401 - the fixture
from test_remote_access import _fake_storage, manager, window  # noqa: F401 - _fake_storage is a fixture


@pytest.fixture(scope="module")
def openapi(contract_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((contract_dir / "openapi.yaml").read_text())


# ------------------------------------------------- 8. `dropped` from /events


def test_dropped_is_not_in_the_contract_yet(openapi) -> None:
    """D40 asked for it as an optional field in 1.4.0. When it lands, an event
    in `dropped` must be retried WITHOUT a strike: change the flush, then this."""
    response = openapi["paths"]["/events"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]

    assert set(response["properties"]) == {"accepted", "duplicate"}, (
        "`dropped` is in the contract now: retry those events without counting a strike (prompt 05, point 8)"
    )


async def test_an_event_in_dropped_is_treated_as_any_unanswered_event(coordinator) -> None:  # noqa: F811
    """Today's behaviour, on purpose (D38): `dropped` is not read, the event
    stays queued with one strike, and goes alone next time."""
    await coordinator.coordinator.async_queue_event("backup_failed", "warning")
    uid = coordinator.coordinator._queue._events[0]["event_uid"]
    coordinator.api.events_response = ApiResponse(
        status=200, data={"accepted": [], "duplicate": [], "dropped": [uid]}
    )

    await coordinator.coordinator.async_heartbeat()

    assert await coordinator.coordinator._queue.async_size() == 1
    assert coordinator.coordinator._queue._attempts == {uid: 1}


# ------------------------------------------------------- 9. long versions


@pytest.mark.parametrize("field", ["ha_version", "agent_version"])
def test_the_version_bound_is_the_schema_s_or_the_old_column_s(state_schema, field: str) -> None:
    """D40: when the schema says how long a version may be, bounds.py follows
    it. Until then the agent keeps to varchar(20), the column of a panel two
    minor versions old - the panel of today copes with more (panel-b, point 7),
    but an agent cannot know which panel it is talking to."""
    declared = state_schema["properties"]["state"]["properties"][field].get("maxLength")

    if declared is None:
        assert bounds.VERSION_MAX == 20
    else:
        assert bounds.VERSION_MAX == declared, f"{field}: the schema says {declared}, bounds.py says {bounds.VERSION_MAX}"


def test_a_dev_build_still_reads_as_a_dev_build() -> None:
    assert bounds.version("2026.11.0.dev202610140301") == "2026.11.0.dev"


# ------------------------------------------------- 10. ha_user_id in /credentials


async def test_the_credentials_payload_is_what_the_contract_asks_for(openapi, _fake_storage) -> None:  # noqa: F811
    """D40: `ha_user_id` is the id of the HAIM maintenance account, which the
    component made for itself - not a person of the house."""
    schema = openapi["paths"]["/credentials"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    remote, hass, api = manager()

    await remote.async_open(window())

    [payload] = api.credentials

    jsonschema.validate(payload, schema)
    assert set(payload) == set(schema["required"])
    assert payload["ha_user_id"] == (await remote._user.async_find()).id
    assert payload["ha_username"] == "haim-maintenance"
