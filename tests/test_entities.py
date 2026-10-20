"""The entities a household actually sees.

The status one matters most: "not connected" has more than one cause and they
mean very different things. A network down for ten minutes is not a key that has
been revoked, and nobody should have to read a log to tell them apart.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.haim_agent.binary_sensor import HaimConnectionSensor
from custom_components.haim_agent.sensor import HaimLastHeartbeatSensor, HaimStatusSensor


def coordinator(**state) -> SimpleNamespace:
    base = {
        "site_uuid": "site-1",
        "connected": True,
        "paused": False,
        "last_error": None,
        "last_success": None,
        "add_listener": lambda listener: None,
        "remove_listener": lambda listener: None,
    }
    base.update(state)

    return SimpleNamespace(**base)


def test_the_connection_sensor_says_whether_we_are_being_heard() -> None:
    assert HaimConnectionSensor(coordinator(connected=True)).is_on is True
    assert HaimConnectionSensor(coordinator(connected=False)).is_on is False


@pytest.mark.parametrize(
    "state,expected",
    [
        ({"connected": True}, "connected"),
        ({"connected": False}, "retrying"),
        ({"connected": False, "paused": True}, "paused"),
        ({"connected": False, "paused": True, "last_error": "token_revoked"}, "revoked"),
    ],
)
def test_the_status_sensor_tells_the_four_cases_apart(state: dict, expected: str) -> None:
    assert HaimStatusSensor(coordinator(**state)).native_value == expected


def test_a_revoked_key_reads_as_revoked_and_not_merely_paused() -> None:
    sensor = HaimStatusSensor(coordinator(connected=False, paused=True, last_error="token_revoked"))

    # Paused means "come back in six hours". Revoked means "somebody decided
    # this". Showing them the same way would leave a household waiting for
    # something that is never coming back on its own.
    assert sensor.native_value == "revoked"


def test_the_entities_are_unique_per_instance() -> None:
    first = HaimConnectionSensor(coordinator(site_uuid="site-a"))
    second = HaimConnectionSensor(coordinator(site_uuid="site-b"))

    assert first.unique_id != second.unique_id


def test_the_entities_group_under_one_device() -> None:
    connection = HaimConnectionSensor(coordinator())
    heartbeat = HaimLastHeartbeatSensor(coordinator())

    # A household that cannot find what a component added has no way to decide
    # how they feel about it.
    assert connection.device_info["identifiers"] == heartbeat.device_info["identifiers"]


def test_the_maintenance_access_entities_follow_the_window() -> None:
    """D51: on while a window is open, with the time it ends; off and unknown otherwise."""
    from datetime import datetime, timezone

    from custom_components.haim_agent.binary_sensor import HaimMaintenanceAccessSensor
    from custom_components.haim_agent.sensor import HaimAccessEndsSensor

    ends = datetime(2026, 10, 20, 16, 0, tzinfo=timezone.utc)
    open_window = coordinator(remote=SimpleNamespace(is_open=True, expires_at=ends))
    closed = coordinator(remote=SimpleNamespace(is_open=False, expires_at=None))

    assert HaimMaintenanceAccessSensor(open_window).is_on is True
    assert HaimAccessEndsSensor(open_window).native_value == ends
    assert HaimMaintenanceAccessSensor(closed).is_on is False
    assert HaimAccessEndsSensor(closed).native_value is None
    # Before the window manager is attached: nothing open, nothing to show.
    assert HaimMaintenanceAccessSensor(coordinator(remote=None)).is_on is False
    assert HaimAccessEndsSensor(coordinator(remote=None)).native_value is None
