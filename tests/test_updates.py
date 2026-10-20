"""Installing updates, against a fake Home Assistant.

The cloud has already decided WHETHER and WHEN by the time a batch gets here.
What is tested is what only the house can decide: that the entity still offers
the update, that a backup which cannot take itself is waited for, and that an
option the entity does not support is never sent.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.haim_agent.updates import async_install_batch


class FakeStates:
    def __init__(self, states: dict[str, SimpleNamespace]) -> None:
        self._states = states

    def get(self, entity_id: str) -> SimpleNamespace | None:
        return self._states.get(entity_id)

    def set(self, entity_id: str, state: str, **attributes: Any) -> None:
        self._states[entity_id] = SimpleNamespace(state=state, attributes=attributes)


class FakeServices:
    def __init__(self, on_call=None) -> None:
        self.calls: list[tuple[str, str, dict, bool]] = []
        self._on_call = on_call

    async def async_call(self, domain: str, service: str, payload: dict, blocking: bool = False) -> None:
        self.calls.append((domain, service, payload, blocking))

        if self._on_call is not None:
            self._on_call(domain, service)


class FakeHass:
    def __init__(self, states: dict[str, SimpleNamespace], on_call=None) -> None:
        self.states = FakeStates(states)
        self.services = FakeServices(on_call)


def waiting_update(**attributes: Any) -> SimpleNamespace:
    return SimpleNamespace(state="on", attributes=attributes)


class Recorder:
    def __init__(self) -> None:
        self.reports: list[tuple[str, str, str | None]] = []

    async def __call__(self, uuid: str, status: str, error: str | None = None) -> None:
        self.reports.append((uuid, status, error))


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """The backup poll waits five seconds a turn. Not in a test suite."""
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("custom_components.haim_agent.updates.asyncio.sleep", instant)


async def test_it_reports_every_item_on_its_own_id() -> None:
    # A batch that updates Core restarts the instance halfway through, so a
    # single report at the end would never be sent.
    hass = FakeHass({
        "update.a_update": waiting_update(),
        "update.b_update": waiting_update(),
    })
    report = Recorder()

    await async_install_batch(hass, report, [
        {"uuid": "one", "entity_id": "update.a_update", "kind": "addon", "backup": False},
        {"uuid": "two", "entity_id": "update.b_update", "kind": "addon", "backup": False},
    ])

    assert report.reports == [("one", "started", None), ("two", "started", None)]
    assert len(hass.services.calls) == 2


async def test_it_never_claims_success_because_the_call_returned() -> None:
    # update.install is not blocking. Returning from it says Home Assistant
    # accepted the request, not that anything was installed - and for Core the
    # process is about to be killed anyway.
    hass = FakeHass({"update.a_update": waiting_update()})
    report = Recorder()

    await async_install_batch(hass, report, [
        {"uuid": "one", "entity_id": "update.a_update", "kind": "core", "backup": False},
    ])

    assert [status for _, status, _ in report.reports] == ["started"]


async def test_it_leaves_what_restarts_the_house_for_last() -> None:
    # Everything else in the batch has to be done before the lights go out.
    hass = FakeHass({
        "update.core_update": waiting_update(),
        "update.addon_update": waiting_update(),
    })
    report = Recorder()

    await async_install_batch(hass, report, [
        {"uuid": "core", "entity_id": "update.core_update", "kind": "core", "backup": False},
        {"uuid": "addon", "entity_id": "update.addon_update", "kind": "addon", "backup": False},
    ])

    assert [call[2]["entity_id"] for call in hass.services.calls] == [
        "update.addon_update",
        "update.core_update",
    ]


async def test_one_failure_does_not_stop_the_chain() -> None:
    hass = FakeHass({
        "update.b_update": waiting_update(),
    })
    report = Recorder()

    outcome = await async_install_batch(hass, report, [
        {"uuid": "missing", "entity_id": "update.gone_update", "kind": "addon", "backup": False},
        {"uuid": "present", "entity_id": "update.b_update", "kind": "addon", "backup": False},
    ])

    assert outcome == {"started": 1, "failed": 1}
    # A code, never the message: the message names the entity (sixth review).
    assert ("missing", "failed", "not_found") in report.reports
    assert len(hass.services.calls) == 1


async def test_it_refuses_an_update_somebody_already_installed() -> None:
    # The inventory the cloud reasoned over is as fresh as the last heartbeat,
    # and thirty seconds is long enough for somebody standing at the machine.
    hass = FakeHass({"update.a_update": SimpleNamespace(state="off", attributes={})})
    report = Recorder()

    outcome = await async_install_batch(hass, report, [
        {"uuid": "one", "entity_id": "update.a_update", "kind": "addon", "backup": False},
    ])

    assert outcome == {"started": 0, "failed": 1}
    assert hass.services.calls == []


async def test_it_never_sends_a_version_the_entity_cannot_take() -> None:
    # The cloud sets `version` only where the entity reported SPECIFIC_VERSION.
    # Sending it otherwise makes Home Assistant refuse the call outright, and
    # the installer gets an error about a service schema instead of an update.
    hass = FakeHass({"update.addon_update": waiting_update()})
    report = Recorder()

    await async_install_batch(hass, report, [
        {"uuid": "one", "entity_id": "update.addon_update", "kind": "addon", "backup": False, "version": None},
    ])

    assert "version" not in hass.services.calls[0][2]


async def test_it_sends_the_version_where_the_entity_takes_one() -> None:
    hass = FakeHass({"update.core_update": waiting_update()})
    report = Recorder()

    await async_install_batch(hass, report, [
        {"uuid": "one", "entity_id": "update.core_update", "kind": "core", "backup": False, "version": "2026.8.3"},
    ])

    assert hass.services.calls[0][2]["version"] == "2026.8.3"


async def test_it_lets_the_entity_take_its_own_backup_when_it_can() -> None:
    hass = FakeHass({"update.core_update": waiting_update()})
    report = Recorder()

    await async_install_batch(hass, report, [
        {"uuid": "one", "entity_id": "update.core_update", "kind": "core", "backup": True, "supports_backup": True},
    ])

    # One call, with backup on it. No separate backup service.
    assert hass.services.calls == [("update", "install", {"entity_id": "update.core_update", "backup": True}, False)]


async def test_it_takes_the_backup_itself_and_waits_when_the_entity_cannot(monkeypatch) -> None:
    # This is the whole of safeguard 2. Without the waiting, "backup required"
    # is a box that was ticked while the update ran alongside a backup that had
    # not finished.
    states = {
        "update.addon_update": waiting_update(),
        "sensor.backup_backup_manager_state": SimpleNamespace(state="idle", attributes={}),
    }
    hass = FakeHass(states)

    def start_backup(domain: str, service: str) -> None:
        if (domain, service) == ("backup", "create_automatic"):
            hass.states.set("sensor.backup_backup_manager_state", "create_backup")

    hass.services._on_call = start_backup

    report = Recorder()

    # It finishes while we are waiting.
    waits: list[int] = []

    async def finish_after_one_turn(_seconds: float) -> None:
        waits.append(1)
        hass.states.set("sensor.backup_backup_manager_state", "idle")

    monkeypatch.setattr(
        "custom_components.haim_agent.updates.asyncio.sleep", finish_after_one_turn
    )

    await async_install_batch(hass, report, [
        {"uuid": "one", "entity_id": "update.addon_update", "kind": "addon", "backup": True, "supports_backup": False},
    ])

    services = [(call[0], call[1]) for call in hass.services.calls]

    assert services == [("backup", "create_automatic"), ("update", "install")]
    assert waits, "the install ran without ever waiting for the backup"


async def test_a_failed_backup_stops_the_install() -> None:
    states = {
        "update.addon_update": waiting_update(),
        "sensor.backup_backup_manager_state": SimpleNamespace(state="idle", attributes={}),
    }
    hass = FakeHass(states)

    def fail_backup(domain: str, service: str) -> None:
        if (domain, service) == ("backup", "create_automatic"):
            hass.states.set("event.backup_automatic_backup", "2026-08-25T02:00:00+00:00", failed_reason="no space left")

    hass.services._on_call = fail_backup

    report = Recorder()

    outcome = await async_install_batch(hass, report, [
        {"uuid": "one", "entity_id": "update.addon_update", "kind": "addon", "backup": True, "supports_backup": False},
    ])

    assert outcome == {"started": 0, "failed": 1}
    # Nothing was installed over a backup that did not happen.
    assert [(call[0], call[1]) for call in hass.services.calls] == [("backup", "create_automatic")]
    assert report.reports[-1][2] == "backup_failed"


async def test_it_skips_an_item_it_could_never_account_for() -> None:
    hass = FakeHass({"update.a_update": waiting_update()})
    report = Recorder()

    await async_install_batch(hass, report, [
        {"entity_id": "update.a_update", "kind": "addon", "backup": False},
    ])

    assert report.reports == []
    assert hass.services.calls == []
