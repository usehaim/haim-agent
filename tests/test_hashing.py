"""The hash, and the rule that everything else in the contract rests on."""

from __future__ import annotations

import json

from custom_components.haim_agent.hashing import canonical, state_hash


def test_it_reproduces_the_hash_the_contract_publishes(real_fixture: dict) -> None:
    # The same value the panel computes in PHP, and the one written down in
    # contract/README.md. If PHP and Python ever disagree about escaping,
    # ordering or whitespace, every heartbeat looks changed, the 204 never
    # fires, and nothing else in either test suite would notice.
    assert len(canonical(real_fixture["state"]).encode()) == 3741
    assert state_hash(real_fixture["state"]) == (
        "be82f77998b3905ab0a2af7d752996261fffa7dc0444147fbc2006fbc9eac9a8"
    )


def test_two_identical_states_hash_the_same(real_fixture: dict) -> None:
    first = json.loads(json.dumps(real_fixture["state"]))
    second = json.loads(json.dumps(real_fixture["state"]))

    assert state_hash(first) == state_hash(second)


def test_key_order_does_not_change_the_hash(real_fixture: dict) -> None:
    state = real_fixture["state"]
    reordered = dict(reversed(list(state.items())))

    # JSON objects have no order and the two sides serialise independently, so
    # the canonical form has to sort. Otherwise a dict built in a different
    # order on one side would look like a change on the other.
    assert state_hash(state) == state_hash(reordered)


def test_a_changed_metric_does_not_change_the_hash(real_fixture: dict) -> None:
    before = state_hash(real_fixture["state"])

    busy = json.loads(json.dumps(real_fixture))
    busy["volatile"]["resources"]["cpu"] = 97.4
    busy["volatile"]["resources"]["ram"] = 88.0
    busy["volatile"]["uptime_seconds"] = 12

    # The 204 path exists because of exactly this. A volatile field leaking into
    # the hashed block is the most expensive mistake this contract prevents.
    assert state_hash(busy["state"]) == before


def test_a_changed_version_does_change_the_hash(real_fixture: dict) -> None:
    before = state_hash(real_fixture["state"])

    upgraded = json.loads(json.dumps(real_fixture["state"]))
    upgraded["ha_version"] = "2026.9.1"

    assert state_hash(upgraded) != before
