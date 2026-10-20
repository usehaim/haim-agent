"""Verifica 03: nothing leaves that the panel cannot store.

`ha_version` and `agent_version` live in varchar(20) on the panel. A
development build reports `2026.11.0.dev202610140301` - twenty-five characters
- and the panel answered 500 to every heartbeat, forever. The column is the
cause and is in docs/DOMANDE-APERTE.md; this is the half that lives here.
"""

from __future__ import annotations

import json

import pytest

from custom_components.haim_agent.bounds import UPDATE_VERSION_MAX, VERSION_MAX, text, version
from custom_components.haim_agent.collector import StateCollector

from test_collector import FakeHass, state


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The case that broke the panel. Still a dev build of 2026.11.0.
        ("2026.11.0.dev202610140301", "2026.11.0.dev"),
        # A beta whose number does not fit keeps the fact that it is a beta.
        ("2026.10.0b12345678901234", "2026.10.0b"),
        # Everything that already fits is left exactly as it was.
        ("2026.8.2", "2026.8.2"),
        ("2026.10.0b3", "2026.10.0b3"),
        ("2026.10.0.dev0", "2026.10.0.dev0"),
        ("18.2", "18.2"),
        ("2026.07.5", "2026.07.5"),
    ],
)
def test_a_version_is_shortened_never_cut(raw: str, expected: str) -> None:
    assert version(raw) == expected
    assert len(version(raw)) <= VERSION_MAX


def test_a_version_that_cannot_be_shortened_honestly_is_left_out() -> None:
    # One part longer than the column: no truthful prefix exists.
    assert version("x" * 30) is None
    # Dropping the dev label to make room would turn a dev build into what
    # reads as a release. Missing is honest; that would not be.
    assert version("2026.11.0.dev202610140301.local.thing.more.parts", 12) is None


def test_no_number_is_ever_cut_in_half() -> None:
    shortened = version("2026.11.0.dev202610140301")

    # The naive answer, value[:20], is `2026.11.0.dev2026101`: a build number
    # that never existed, and an advisory gate would reason on it.
    assert shortened != "2026.11.0.dev202610140301"[:VERSION_MAX]
    assert "2026101" not in shortened


def test_free_text_has_a_ceiling() -> None:
    assert text("a" * 1000, 100) == "a" * 100
    assert text(None) is None


async def test_a_development_build_reaches_the_heartbeat_in_a_form_the_panel_can_store(monkeypatch) -> None:
    import custom_components.haim_agent.system as system

    async def dev_build(hass):
        return {"version": "2026.11.0.dev202610140301", "installation_type": "Home Assistant Core",
                "python_version": "3.14.0rc2+experimental-build-20261014"}

    monkeypatch.setattr(system, "_async_system_info", dev_build)

    hass = FakeHass(states={
        "update.home_assistant_core_update": state(
            "update.home_assistant_core_update", "on",
            installed_version="2026.11.0.dev202610140301" + ".nightly" * 5,
            latest_version="2026.11.0", supported_features=3,
        ),
    })

    payload = await StateCollector(hass).async_build_state()

    assert payload["ha_version"] == "2026.11.0.dev"
    assert len(payload["agent_version"]) <= VERSION_MAX
    assert payload["python_version"] is None or len(payload["python_version"]) <= VERSION_MAX
    assert len(payload["updates"][0]["installed"]) <= UPDATE_VERSION_MAX
    assert payload["updates"][0]["installed"].startswith("2026.11.0.dev")


async def test_lists_leave_as_lists_whatever_a_helper_returns(monkeypatch) -> None:
    """A tuple, a set or a dict where the contract says array.

    A set would not even serialise - the whole heartbeat would go with it - and
    a dict becomes `{}` on the far side, which a PHP `foreach` reads as a
    different shape.
    """
    import custom_components.haim_agent.collector as collector_module

    monkeypatch.setattr(collector_module, "async_describe_instance", _describing({"system_log", "hacs"}))

    collector = StateCollector(FakeHass())
    monkeypatch.setattr(collector, "_integrations", lambda: {"zha": "loaded"})
    monkeypatch.setattr(collector, "_updates", lambda: ({"kind": "core"},))

    payload = await collector.async_build_state()

    for field in ("capabilities", "integrations", "addons", "updates", "repairs"):
        assert isinstance(payload[field], list), field

    # Sorted, so the hash does not depend on set iteration order (rule 12).
    assert payload["capabilities"] == ["hacs", "system_log"]
    assert payload["integrations"] == []
    json.dumps(payload)


async def test_an_add_on_state_the_contract_does_not_know_travels_as_null(monkeypatch) -> None:
    import custom_components.haim_agent.collector as collector_module
    import homeassistant.components.hassio as hassio

    monkeypatch.setattr(collector_module, "async_describe_instance", _describing(["supervisor"]))
    monkeypatch.setattr(hassio, "get_host_info", lambda hass: {}, raising=False)
    monkeypatch.setattr(hassio, "get_os_info", lambda hass: {}, raising=False)
    monkeypatch.setattr(hassio, "get_supervisor_info", lambda hass: {"addons": [
        {"slug": "core_mosquitto", "version": "7.1.0", "state": "unknown"},
        {"slug": "x", "version": "1.0.0", "state": "started"},
    ]}, raising=False)

    payload = await StateCollector(FakeHass()).async_build_state()

    assert [addon["state"] for addon in payload["addons"]] == [None, "started"]


def _describing(capabilities):
    async def describe(hass):
        return {
            "ha_version": "2026.8.2", "agent_version": "1.0.0", "install_type": "core",
            "python_version": "3.14.0", "instance_uuid": "", "capabilities": capabilities,
        }

    return describe
