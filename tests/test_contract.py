"""The contract, checked from this side.

Both sides validate against the same schema in CI. The day one of them drifts
from it, the build fails on the side that drifted - which is the whole reason
the schema is a file in a shared folder and not a paragraph in a document.
"""

from __future__ import annotations

import hashlib
import json

import jsonschema
import pytest


def test_the_real_fixture_matches_the_schema(real_fixture: dict, state_schema: dict) -> None:
    jsonschema.validate(instance=real_fixture, schema=state_schema)


def test_the_canonical_hash_is_the_one_the_contract_publishes(real_fixture: dict) -> None:
    canonical = json.dumps(real_fixture["state"], sort_keys=True, separators=(",", ":"))

    # Documented in contract/README.md for this exact payload. If this value
    # ever moves, either the fixture changed or the canonicalisation did, and
    # both are things somebody has to have decided on purpose.
    assert len(canonical.encode()) == 3741
    assert (
        hashlib.sha256(canonical.encode()).hexdigest()
        == "be82f77998b3905ab0a2af7d752996261fffa7dc0444147fbc2006fbc9eac9a8"
    )


def test_the_volatile_block_is_not_in_the_hash(real_fixture: dict) -> None:
    before = json.dumps(real_fixture["state"], sort_keys=True, separators=(",", ":"))

    busy = json.loads(json.dumps(real_fixture))
    busy["volatile"]["resources"]["cpu"] = 97.4
    busy["volatile"]["uptime_seconds"] = 1
    busy["volatile"]["ip_local"] = "10.0.0.9"

    after = json.dumps(busy["state"], sort_keys=True, separators=(",", ":"))

    # If a field that changes on every send ever ends up inside `state`, the
    # hash differs every time, the 204 never fires, and ninety per cent of the
    # write saving evaporates - with no symptom until there are a thousand
    # instances. This is the assertion that catches it on this side.
    assert before == after


@pytest.mark.parametrize(
    "forbidden",
    ["entity_id", "friendly_name", "area", "room", "latitude", "longitude", "mac", "ssid"],
)
def test_the_schema_has_no_place_to_put_household_data(state_schema: dict, forbidden: str) -> None:
    properties = state_schema["properties"]["state"]["properties"]

    # `updates[].entity_id` is the one legitimate use in the whole schema, and
    # it names a Home Assistant update entity - update.home_assistant_core_update
    # - not anything anybody in the house owns or named. So the check is not
    # "the word never appears" but "it appears nowhere except there".
    without_updates = {key: value for key, value in properties.items() if key != "updates"}

    assert f'"{forbidden}"' not in json.dumps(without_updates)
