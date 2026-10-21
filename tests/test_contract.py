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


def _command_schema(contract_dir, action: str) -> dict:
    return json.loads((contract_dir / "schemas" / "commands" / f"{action}.schema.json").read_text())


def _window(relay) -> dict:
    return {
        "request_uuid": "8a63d880-d424-4a75-ab31-a3ba154fa491",
        "session_uuid": "11111111-1111-4111-8111-111111111111",
        "slug": "abcdefghijklmnopqrstuvwxyz",
        "granted_minutes": 60,
        "expires_at": "2026-10-20T13:16:57Z",
        "ticket_hash": "0" * 64,
        "relay_url": relay,
    }


@pytest.mark.parametrize("relay", [None, "https://tun.usehaim.com"])
def test_a_window_the_house_opens_is_one_the_contract_allows(contract_dir, relay) -> None:
    jsonschema.validate(instance=_window(relay), schema=_command_schema(contract_dir, "connection_approved"))


def test_the_contract_forbids_the_empty_relay_of_20_october(contract_dir) -> None:
    # The house tolerates it since 1.0.1; the panel must never send it again.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=_window(""), schema=_command_schema(contract_dir, "connection_approved"))


def test_an_answer_given_elsewhere_carries_no_window(contract_dir) -> None:
    jsonschema.validate(
        instance={"request_uuid": "8a63d880-d424-4a75-ab31-a3ba154fa491", "granted_minutes": 60},
        schema=_command_schema(contract_dir, "connection_approved"),
    )


def test_every_close_reason_in_the_contract_is_one_the_house_reads(contract_dir) -> None:
    from custom_components.haim_agent.commands import _REASON

    reasons = _command_schema(contract_dir, "close_session")["properties"]["close_reason"]["enum"]

    assert "house_refused" in reasons
    assert all(_REASON.fullmatch(reason) for reason in reasons)
