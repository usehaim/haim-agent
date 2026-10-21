"""The collector, against a fake Home Assistant.

Two shapes are tested: one WITH a Supervisor and one without. The second is the
one that matters more - it is the laboratory profile, `core`, the poorest of the
four, and if the component works there it works everywhere.

The other half of this file is about failure. A collector that raises on one
field must still produce a payload containing all the others: an undocumented
Home Assistant API changing in a release is not a hypothetical, it is what
happens every few months, and the wrong answer is a silent instance.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.haim_agent.collector import StateCollector
from custom_components.haim_agent.const import LOG_LEVEL_FULL


class FakeStates:
    def __init__(self, states: dict[str, SimpleNamespace]) -> None:
        self._states = states

    def async_entity_ids(self, domain: str) -> list[str]:
        return [entity_id for entity_id in self._states if entity_id.startswith(f"{domain}.")]

    def async_all(self) -> list[SimpleNamespace]:
        return list(self._states.values())

    def get(self, entity_id: str) -> SimpleNamespace | None:
        return self._states.get(entity_id)


class FakeConfigEntries:
    def __init__(self, entries: list[SimpleNamespace]) -> None:
        self._entries = entries

    def async_entries(self) -> list[SimpleNamespace]:
        return self._entries


class FakeHass:
    """Just enough Home Assistant for the collector to run against."""

    def __init__(self, *, states=None, entries=None, data=None, components=None) -> None:
        self.states = FakeStates(states or {})
        self.config_entries = FakeConfigEntries(entries or [])
        self.data = data or {}
        self.config = SimpleNamespace(
            time_zone="Europe/Rome",
            components=components or set(),
            language="it",
            path=lambda *parts: "/config",
        )

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def state(entity_id: str, value: str, **attributes: Any) -> SimpleNamespace:
    return SimpleNamespace(entity_id=entity_id, state=value, attributes=attributes)


@pytest.fixture
def hass_without_supervisor() -> FakeHass:
    """A Core install: no Supervisor, no add-ons, and everything else working.

    This is the laboratory profile and the poorest of the four. Nothing here is
    allowed to be a failure - it is a smaller product, not a broken one.
    """
    return FakeHass(
        states={
            "automation.a": state("automation.a", "on"),
            "automation.b": state("automation.b", "on"),
            "script.c": state("script.c", "off"),
            "light.d": state("light.d", "unavailable"),
            "light.e": state("light.e", "on"),
            "update.home_assistant_core_update": state(
                "update.home_assistant_core_update",
                "on",
                installed_version="2026.8.2",
                latest_version="2026.8.4",
                supported_features=3,
                release_url="https://example.test/release",
            ),
            "sensor.backup_last_successful_automatic_backup": state(
                "sensor.backup_last_successful_automatic_backup", "2026-08-24T03:00:00+00:00"
            ),
            "sensor.backup_backup_manager_state": state(
                "sensor.backup_backup_manager_state", "idle", agents=["local"]
            ),
        },
        entries=[SimpleNamespace(domain="mqtt", state=None), SimpleNamespace(domain="zha", state=None)],
        components={"system_log", "backup"},
    )


async def test_it_reports_a_core_instance_without_pretending_it_has_a_supervisor(
    hass_without_supervisor: FakeHass,
) -> None:
    collector = StateCollector(hass_without_supervisor)

    state_block = await collector.async_build_state()

    assert state_block["install_type"] == "core"
    assert state_block["supervisor_version"] is None
    assert state_block["os_version"] is None
    # Empty, not missing: a Container or Core install has no add-ons, and the
    # cloud degrades what it offers rather than refusing the instance.
    assert state_block["addons"] == []
    assert "supervisor" not in state_block["capabilities"]


async def test_it_counts_without_naming(hass_without_supervisor: FakeHass) -> None:
    state_block = await StateCollector(hass_without_supervisor).async_build_state()

    assert state_block["counts"]["automations"] == 2
    assert state_block["counts"]["scripts"] == 1

    # The whole boundary, asserted: nothing anywhere in the block names an
    # entity, a device, a room or a person.
    import json

    serialised = json.dumps(state_block)

    for forbidden in ("light.d", "light.e", "automation.a", "script.c"):
        assert forbidden not in serialised


async def test_it_counts_unavailable_entities_rather_than_listing_them(
    hass_without_supervisor: FakeHass,
) -> None:
    state_block = await StateCollector(hass_without_supervisor).async_build_state()

    # One unavailable light in the fixture. A real instance sits at a hundred
    # and forty of these on a good day, which is why it is a number to watch the
    # shape of and not a fault in itself.
    assert state_block["problems"]["unavailable_entities"] == 1


async def test_it_reads_update_feature_flags_from_the_enum(
    hass_without_supervisor: FakeHass,
) -> None:
    state_block = await StateCollector(hass_without_supervisor).async_build_state()

    update = state_block["updates"][0]

    assert update["kind"] == "core"
    assert update["available"] is True
    assert update["installed"] == "2026.8.2"
    assert update["latest"] == "2026.8.4"
    # Read from UpdateEntityFeature, never hardcoded: those numbers have been
    # renumbered before, and a stale constant means offering a backup that will
    # not actually be taken.
    assert isinstance(update["supports_backup"], bool)


async def test_it_reports_backups_without_a_supervisor(
    hass_without_supervisor: FakeHass,
) -> None:
    state_block = await StateCollector(hass_without_supervisor).async_build_state()

    # The four backup manager sensors are readable with a non-admin token and
    # with no Supervisor at all. The competing product does not report backups
    # in any form; on a Container install there is no other way to know.
    assert state_block["backup"]["last_success_at"] == "2026-08-24T03:00:00+00:00"
    assert state_block["backup"]["state"] == "idle"
    assert state_block["backup"]["agents_configured"] == 1


async def test_a_field_that_explodes_does_not_take_the_payload_with_it(
    hass_without_supervisor: FakeHass,
) -> None:
    class Exploding(FakeStates):
        def async_all(self):
            raise RuntimeError("the entity registry API changed again")

    hass_without_supervisor.states = Exploding(
        {"automation.a": state("automation.a", "on")}
    )

    state_block = await StateCollector(hass_without_supervisor).async_build_state()

    # One field degrades, everything else survives. This is the difference
    # between a customer's instance going quiet and one number being wrong.
    assert state_block["problems"]["unavailable_entities"] == 0
    assert state_block["ha_version"]
    assert state_block["install_type"] == "core"


async def test_the_volatile_block_carries_what_changes_and_the_state_block_does_not(
    hass_without_supervisor: FakeHass,
) -> None:
    collector = StateCollector(hass_without_supervisor, log_level=LOG_LEVEL_FULL)

    state_block = await collector.async_build_state()
    volatile = await collector.async_build_volatile()

    # If any of these four ever appears in the hashed block, the cloud's "204,
    # nothing changed" branch stops firing and ninety per cent of its write
    # saving evaporates - with no symptom until there are a thousand instances.
    for changing in ("uptime_seconds", "resources", "ip_local", "logs"):
        assert changing in volatile
        assert changing not in state_block


@pytest.fixture
def hass_with_supervisor(hass_without_supervisor: FakeHass, monkeypatch: pytest.MonkeyPatch) -> FakeHass:
    """An OS install: a Supervisor, add-ons, and a host to report on."""
    import homeassistant.components.hassio as hassio

    monkeypatch.setattr(hassio, "is_hassio", lambda hass: True, raising=False)
    monkeypatch.setattr(
        hassio,
        "get_host_info",
        lambda hass: {"disk_total": 234.0, "disk_used": 27.1, "chassis": "vm"},
        raising=False,
    )
    monkeypatch.setattr(
        hassio,
        "get_os_info",
        lambda hass: {"version": "18.2", "board": "generic-x86-64"},
        raising=False,
    )
    monkeypatch.setattr(
        hassio,
        "get_supervisor_info",
        lambda hass: {
            "version": "2026.07.5",
            "addons": [
                {"slug": "core_mosquitto", "name": "Mosquitto broker", "version": "7.1.0",
                 "update_available": False, "state": "started"},
                {"slug": "45df7312_zigbee2mqtt", "name": "Zigbee2MQTT", "version": "2.13.0-1",
                 "update_available": True, "state": "started"},
            ],
        },
        raising=False,
    )

    # And the system info has to agree, or the collector correctly refuses to
    # believe in a Supervisor it was not told about.
    import custom_components.haim_agent.system as system

    async def fake_system_info(hass):
        return {
            "version": "2026.8.2",
            "installation_type": "Home Assistant OS",
            "python_version": "3.14.6",
        }

    monkeypatch.setattr(system, "_async_system_info", fake_system_info)
    monkeypatch.setattr(system, "_has_supervisor", lambda hass: True)

    return hass_without_supervisor


async def test_it_reports_add_ons_and_host_versions_where_there_is_a_supervisor(
    hass_with_supervisor: FakeHass,
) -> None:
    state_block = await StateCollector(hass_with_supervisor).async_build_state()

    assert state_block["install_type"] == "os"
    assert state_block["supervisor_version"] == "2026.07.5"
    assert state_block["os_version"] == "18.2"
    assert state_block["board"] == "generic-x86-64"
    assert "supervisor" in state_block["capabilities"]

    slugs = [addon["slug"] for addon in state_block["addons"]]

    # Slugs, not contents. `core_mosquitto` says an MQTT broker runs here and
    # nothing whatsoever about the household.
    assert slugs == ["core_mosquitto", "45df7312_zigbee2mqtt"]
    assert state_block["addons"][1]["update_available"] is True


async def test_the_supervisor_disappearing_mid_release_costs_one_block_not_the_payload(
    hass_with_supervisor: FakeHass, monkeypatch: pytest.MonkeyPatch
) -> None:
    import homeassistant.components.hassio as hassio

    def gone(hass):
        raise AttributeError("get_supervisor_info was renamed")

    monkeypatch.setattr(hassio, "get_supervisor_info", gone, raising=False)

    state_block = await StateCollector(hass_with_supervisor).async_build_state()

    # Half the Home Assistant APIs this component uses are not documented as
    # public, and they change without deprecation. The payload still goes.
    assert state_block["addons"] == []
    assert state_block["counts"]["automations"] == 2
    assert state_block["install_type"] == "os"


async def test_a_blocked_backup_manager_is_not_a_failed_backup(
    hass_without_supervisor: FakeHass,
) -> None:
    hass_without_supervisor.states._states["sensor.backup_backup_manager_state"] = state(
        "sensor.backup_backup_manager_state", "blocked"
    )

    state_block = await StateCollector(hass_without_supervisor).async_build_state()

    # `blocked` means backups are temporarily not possible - during startup,
    # during an update. Reading it as a failure fires a "backup failed" alert at
    # every restart of an instance that has never taken a backup in its life.
    # This is not hypothetical: it is what happened the first time this ran
    # against the laboratory.
    assert state_block["backup"]["state"] is None
    assert state_block["backup"]["last_failure_reason"] is None


async def test_a_real_failure_is_reported_as_one(hass_without_supervisor: FakeHass) -> None:
    hass_without_supervisor.states._states["sensor.backup_backup_manager_state"] = state(
        "sensor.backup_backup_manager_state", "idle"
    )
    hass_without_supervisor.states._states["event.backup_automatic_backup"] = state(
        "event.backup_automatic_backup", "2026-08-24T03:00:00+00:00",
        failed_reason="No space left on device",
    )

    state_block = await StateCollector(hass_without_supervisor).async_build_state()

    # The event entity is what carries the reason, and it is the only thing that
    # justifies saying a backup failed.
    assert state_block["backup"]["state"] == "failed"
    assert state_block["backup"]["last_failure_reason"] == "No space left on device"


async def test_an_instance_that_has_never_backed_up_says_nothing_either_way(
    hass_without_supervisor: FakeHass,
) -> None:
    del hass_without_supervisor.states._states["sensor.backup_backup_manager_state"]
    del hass_without_supervisor.states._states["sensor.backup_last_successful_automatic_backup"]

    state_block = await StateCollector(hass_without_supervisor).async_build_state()

    # null is not false. A brand new instance must not raise a backup alert on
    # its first day for a backup nobody has configured yet.
    assert state_block["backup"]["state"] is None
    assert state_block["backup"]["last_success_at"] is None
    assert state_block["backup"]["last_attempt_at"] is None


def fake_issue_registry(monkeypatch, issues: list) -> None:
    """The collector reaches the registry through `ir.async_get(hass).issues`.

    Nothing faked it before, so `_safe` swallowed the failure and every test saw
    zero repair issues - which is why nothing noticed that only a count was
    being sent.
    """
    from homeassistant.helpers import issue_registry as ir

    registry = SimpleNamespace(issues={index: issue for index, issue in enumerate(issues)})

    monkeypatch.setattr(ir, "async_get", lambda hass: registry)


async def test_it_reports_which_integration_raised_the_repair_issues(
    hass_without_supervisor, monkeypatch
) -> None:
    """Contract 1.2.0. «44 problemi» non e' azionabile, «44 problemi, per lo
    piu' shelly» lo e'."""
    fake_issue_registry(monkeypatch, [
        SimpleNamespace(domain="shelly", severity="IssueSeverity.warning", dismissed_version=None),
        SimpleNamespace(domain="shelly", severity="IssueSeverity.critical", dismissed_version=None),
        SimpleNamespace(domain="hacs", severity="IssueSeverity.warning", dismissed_version=None),
    ])

    payload = await StateCollector(hass_without_supervisor).async_build_state()

    # Sorted by domain, always: `state` is hashed and list order is part of the
    # hash, so an iteration order that shifted would make every heartbeat look
    # like a change (rule 12).
    assert payload["repairs"] == [
        {"domain": "hacs", "count": 1, "critical": 0},
        {"domain": "shelly", "count": 2, "critical": 1},
    ]

    assert payload["problems"]["repair_issues"] == 3
    assert payload["problems"]["repair_issues_critical"] == 1


async def test_the_repair_issues_carry_nothing_but_the_domain(
    hass_without_supervisor, monkeypatch
) -> None:
    """The line this whole field lives or dies on.

    `issue_id` is routinely built out of an entity id, and
    `translation_placeholders` is where the names actually are. Neither travels.
    """
    fake_issue_registry(monkeypatch, [
        SimpleNamespace(
            domain="shelly",
            issue_id="unavailable_device_luce_camera_matrimoniale",
            translation_key="device_unavailable",
            translation_placeholders={"name": "Luce camera matrimoniale"},
            severity="IssueSeverity.warning",
            dismissed_version=None,
        ),
    ])

    payload = await StateCollector(hass_without_supervisor).async_build_state()

    assert payload["repairs"] == [{"domain": "shelly", "count": 1, "critical": 0}]

    everything = json.dumps(payload)

    assert "camera_matrimoniale" not in everything
    assert "Luce camera matrimoniale" not in everything
    assert "device_unavailable" not in everything


async def test_a_dismissed_issue_is_not_a_problem(hass_without_supervisor, monkeypatch) -> None:
    fake_issue_registry(monkeypatch, [
        SimpleNamespace(domain="hacs", severity="IssueSeverity.warning", dismissed_version="2026.8.0"),
    ])

    payload = await StateCollector(hass_without_supervisor).async_build_state()

    assert payload["repairs"] == []


def test_devices_are_listed_before_and_after_home_assistant_2027_9() -> None:
    """Iterating `devices` yields ids up to 2026.9 and entries from 2026.10, where
    `.values()` is deprecated and breaks in 2027.9 (warning seen on a real instance)."""
    from types import SimpleNamespace

    from custom_components.haim_agent.collector import _device_entries

    lamp = SimpleNamespace(name="Lampada", name_by_user=None)

    class OldDevices(dict):
        pass

    class NewDevices:
        def __iter__(self):
            return iter([lamp])

        def values(self):
            raise AssertionError("deprecated in 2026.10, gone in 2027.9")

    assert _device_entries(SimpleNamespace(devices=OldDevices(abc=lamp))) == [lamp]
    assert _device_entries(SimpleNamespace(devices=NewDevices())) == [lamp]
    assert _device_entries(SimpleNamespace(devices=OldDevices())) == []
