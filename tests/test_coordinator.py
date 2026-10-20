"""The heartbeat loop and what it does with each answer.

The four responses are genuinely different and treating any two of them the
same is how an instance ends up either silent or hammering.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.haim_agent.api import ApiResponse, HaimApiError, SiteInactive, TokenRevoked
from custom_components.haim_agent.const import ISSUE_SITE_INACTIVE, ISSUE_TOKEN_REVOKED
from custom_components.haim_agent.coordinator import HaimCoordinator


class FakeApi:
    """Answers whatever the test tells it to, and remembers what it was sent."""

    def __init__(self, *, state_response: Any = None, events_response: Any = None) -> None:
        self.state_response = state_response or ApiResponse(status=204, data=None)
        self.events_response = events_response or ApiResponse(
            status=200, data={"accepted": [], "duplicate": []}
        )
        self.state_calls: list[tuple[dict, str]] = []
        self.events_calls: list[list[dict]] = []
        self.key: str | None = "haim_abcdef12_" + "x" * 32

    async def send_state(self, payload: dict, state_hash: str) -> ApiResponse:
        self.state_calls.append((payload, state_hash))

        if isinstance(self.state_response, Exception):
            raise self.state_response

        return self.state_response

    async def send_events(self, events: list[dict]) -> ApiResponse:
        self.events_calls.append(events)

        if isinstance(self.events_response, Exception):
            raise self.events_response

        return self.events_response

    def with_key(self, agent_key: str) -> None:
        self.key = agent_key


@pytest.fixture
def coordinator(monkeypatch: pytest.MonkeyPatch):
    """A coordinator with the scheduling and the collector stubbed out.

    What is under test is the decision made about each answer, not Home
    Assistant's timer helpers.
    """
    import custom_components.haim_agent.coordinator as module
    import custom_components.haim_agent.queue as queue_module

    scheduled: dict[str, Any] = {"interval": None, "later": None}

    monkeypatch.setattr(
        module, "async_track_time_interval",
        lambda hass, action, delta: scheduled.__setitem__("interval", delta) or (lambda: None),
    )
    monkeypatch.setattr(
        module, "async_call_later",
        lambda hass, delay, action: scheduled.__setitem__("later", delay) or (lambda: None),
    )

    issues: list[str] = []
    monkeypatch.setattr(
        module.ir, "async_create_issue",
        lambda hass, domain, issue_id, **kwargs: issues.append(issue_id),
    )

    class MemoryStore:
        def __init__(self) -> None:
            self.saved: dict[str, Any] | None = None

        async def async_load(self):
            return self.saved

        async def async_save(self, data):
            self.saved = data

    monkeypatch.setattr(queue_module, "Store", lambda *args, **kwargs: MemoryStore())

    api = FakeApi()
    persisted: list[str] = []
    coordinator = HaimCoordinator(
        hass=None, api=api, site_uuid="site-uuid-1", persist_key=persisted.append
    )

    async def fake_state():
        return {
            "ha_version": "2026.8.2",
            "agent_version": "1.0.0",
            "install_type": "core",
            "capabilities": [],
            "counts": {"entities": 42, "devices": 3, "automations": 1, "dashboards": 1},
            "problems": {"unavailable_entities": 0, "config_entry_errors": 0, "repair_issues": 0},
        }

    async def fake_volatile():
        return {"uptime_seconds": 100, "resources": {"cpu": 5.0}}

    monkeypatch.setattr(coordinator._collector, "async_build_state", fake_state)
    monkeypatch.setattr(coordinator._collector, "async_build_volatile", fake_volatile)

    return SimpleNamespace(
        coordinator=coordinator, api=api, issues=issues, scheduled=scheduled, persisted=persisted
    )


async def test_a_204_is_a_success_and_says_nothing_more(coordinator) -> None:
    assert await coordinator.coordinator.async_heartbeat() is True
    assert coordinator.coordinator.connected is True
    assert coordinator.coordinator.last_error is None


async def test_the_hash_travels_in_the_header_and_covers_only_the_state_block(coordinator) -> None:
    from custom_components.haim_agent.hashing import state_hash

    await coordinator.coordinator.async_heartbeat()

    payload, sent_hash = coordinator.api.state_calls[0]

    assert sent_hash == state_hash(payload["state"])
    assert "volatile" in payload
    assert "resources" not in payload["state"]


async def test_a_revoked_key_stops_it_and_tells_the_household(coordinator) -> None:
    coordinator.api.state_response = TokenRevoked("revoked")

    assert await coordinator.coordinator.async_heartbeat() is False

    # Stop, and make sure somebody in the house can see why. Retrying would
    # never change the answer, and a component that has been cut off and says so
    # only in a debug log is one nobody knows has stopped working.
    assert coordinator.coordinator.paused is True
    assert coordinator.coordinator.last_error == "token_revoked"
    assert ISSUE_TOKEN_REVOKED in coordinator.issues


async def test_an_archived_instance_pauses_for_six_hours(coordinator) -> None:
    coordinator.api.state_response = SiteInactive("archived")

    await coordinator.coordinator.async_heartbeat()

    assert coordinator.coordinator.paused is True
    assert ISSUE_SITE_INACTIVE in coordinator.issues
    # Six hours, not thirty seconds: nothing that happens in this house will
    # change the answer.
    assert coordinator.scheduled["later"] == 6 * 60 * 60


async def test_a_paused_coordinator_does_not_send(coordinator) -> None:
    coordinator.api.state_response = SiteInactive("archived")
    await coordinator.coordinator.async_heartbeat()

    sent_so_far = len(coordinator.api.state_calls)

    assert await coordinator.coordinator.async_heartbeat() is False
    assert len(coordinator.api.state_calls) == sent_so_far


async def test_a_flaky_network_is_not_an_incident(coordinator) -> None:
    coordinator.api.state_response = OSError("connection reset")

    assert await coordinator.coordinator.async_heartbeat() is False

    # A network that comes and goes is the normal condition of a house. Note it,
    # try again next interval, and do not raise anything in anybody's face.
    assert coordinator.coordinator.connected is False
    assert coordinator.coordinator.paused is False
    assert coordinator.issues == []


async def test_it_takes_a_new_interval_from_the_cloud(coordinator) -> None:
    coordinator.api.state_response = ApiResponse(
        status=200,
        data={"config": {"state_interval": 600, "event_batch_max": 25, "log_level": "counts"}},
        config_version="2",
    )

    await coordinator.coordinator.async_heartbeat()

    # This is how a whole fleet is reconfigured without shipping a release to
    # three hundred houses and waiting for HACS to catch up.
    assert coordinator.scheduled["interval"].total_seconds() == 600


async def test_it_refuses_an_interval_that_would_hurt_us(coordinator) -> None:
    coordinator.api.state_response = ApiResponse(
        status=200, data={"config": {"state_interval": 0}}, config_version="3"
    )

    await coordinator.coordinator.async_heartbeat()

    # The bounds are ours, not theirs. A server bug that sent zero would turn
    # every instance in the fleet into a denial of service against us.
    assert coordinator.coordinator.offset >= 0
    assert coordinator.scheduled["interval"] is None or coordinator.scheduled["interval"].total_seconds() >= 60


ROTATED = "haim_99887766_" + "y" * 32


def _later(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    import custom_components.haim_agent.coordinator as module

    now = module.time.monotonic() + seconds
    monkeypatch.setattr(module.time, "monotonic", lambda: now)


async def test_a_rotated_key_is_stored_before_it_is_used(coordinator, monkeypatch) -> None:
    """The panel repeats the new key until it is used, then stops. A key used
    only from memory was gone at the next restart, and seven days later the
    house was locked out (prompt 05, point 1)."""
    old = coordinator.api.key
    coordinator.api.state_response = ApiResponse(status=204, data=None, rotated_key=ROTATED)

    await coordinator.coordinator.async_heartbeat()

    # On disk first, and the old key still in use.
    assert coordinator.persisted == [ROTATED]
    assert coordinator.api.key == old

    # The same header a moment later: the write may not have landed yet.
    await coordinator.coordinator.async_heartbeat()
    assert coordinator.api.key == old
    assert coordinator.persisted == [ROTATED]

    # Once it has had time to reach the disk, the next answer switches.
    _later(monkeypatch, 60)
    await coordinator.coordinator.async_heartbeat()

    assert coordinator.api.key == ROTATED
    assert coordinator.persisted == [ROTATED]


async def test_a_rotated_key_that_cannot_be_stored_is_not_used(coordinator, monkeypatch) -> None:
    old = coordinator.api.key

    def broken(_key: str) -> None:
        raise OSError("disk full")

    coordinator.coordinator._persist_key = broken
    coordinator.api.state_response = ApiResponse(status=204, data=None, rotated_key=ROTATED)

    await coordinator.coordinator.async_heartbeat()
    _later(monkeypatch, 60)
    await coordinator.coordinator.async_heartbeat()

    assert coordinator.api.key == old


@pytest.mark.parametrize(
    "offered",
    ["haim_short_" + "y" * 32, "haim_99887766_" + "y" * 31, "Bearer haim_99887766_" + "y" * 32, ""],
)
async def test_a_replacement_in_a_shape_the_panel_never_issues_is_ignored(coordinator, monkeypatch, offered) -> None:
    old = coordinator.api.key
    coordinator.api.state_response = ApiResponse(status=204, data=None, rotated_key=offered)

    await coordinator.coordinator.async_heartbeat()
    _later(monkeypatch, 60)
    await coordinator.coordinator.async_heartbeat()

    assert coordinator.persisted == []
    assert coordinator.api.key == old


async def test_the_rotated_key_lands_in_the_config_entry(hass) -> None:
    """Against a real Home Assistant: what the coordinator persists is what the
    next start reads as the agent key."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.haim_agent import key_persister
    from custom_components.haim_agent.const import CONF_AGENT_KEY, CONF_BASE_URL, DOMAIN

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_BASE_URL: "https://haim.example", CONF_AGENT_KEY: "haim_abcdef12_" + "x" * 32},
    )
    entry.add_to_hass(hass)

    key_persister(hass, entry)(ROTATED)

    assert entry.data[CONF_AGENT_KEY] == ROTATED
    assert entry.data[CONF_BASE_URL] == "https://haim.example"


async def test_it_flushes_the_queue_once_it_is_heard_again(coordinator) -> None:
    await coordinator.coordinator.async_queue_event("backup_failed", "critical")
    await coordinator.coordinator.async_queue_event("integration_error", "info")

    coordinator.api.events_response = ApiResponse(
        status=200, data={"accepted": [], "duplicate": []}
    )

    await coordinator.coordinator.async_heartbeat()

    assert len(coordinator.api.events_calls) == 1
    assert len(coordinator.api.events_calls[0]) == 2


async def test_a_failed_flush_keeps_everything_queued(coordinator) -> None:
    await coordinator.coordinator.async_queue_event("backup_failed", "critical")

    coordinator.api.events_response = OSError("connection reset")

    await coordinator.coordinator.async_heartbeat()

    # Nothing is dropped because the network was briefly unavailable.
    assert await coordinator.coordinator._queue.async_size() == 1


async def test_the_offset_is_the_same_every_time_the_component_starts(coordinator) -> None:
    first = coordinator.coordinator.offset

    second = HaimCoordinator(hass=None, api=coordinator.api, site_uuid="site-uuid-1").offset

    assert first == second


async def test_the_panel_cannot_send_out_more_of_the_log_than_the_house_chose(coordinator) -> None:
    """Independent review (D41): `counts` chosen in the house, `full` pushed by
    the panel, and the whole log went out while the notice said otherwise."""
    assert coordinator.coordinator._collector._log_level == "counts"

    coordinator.api.state_response = ApiResponse(
        status=200, data={"config": {"log_level": "full"}}, config_version="9"
    )

    await coordinator.coordinator.async_heartbeat()

    assert coordinator.coordinator._collector._log_level == "counts"

    coordinator.coordinator.set_log_level("full")

    assert coordinator.coordinator._collector._log_level == "counts"


async def test_the_panel_can_still_send_less(monkeypatch) -> None:
    import custom_components.haim_agent.queue as queue_module
    from custom_components.haim_agent.coordinator import HaimCoordinator

    monkeypatch.setattr(queue_module, "Store", lambda *args, **kwargs: None)
    chosen_full = HaimCoordinator(hass=None, api=FakeApi(), site_uuid="s", log_level="full")

    chosen_full.set_log_level("counts")
    assert chosen_full._collector._log_level == "counts"

    # And back up, but only as far as the house allowed.
    chosen_full.set_log_level("full")
    assert chosen_full._collector._log_level == "full"

    assert chosen_full.capped_log_level("nonsense") == "full"


def test_a_family_that_was_not_asked_sends_counts_only() -> None:
    from custom_components.haim_agent.const import DEFAULT_LOG_LEVEL

    assert DEFAULT_LOG_LEVEL == "counts"
