"""Verifica 03, points 4 and 5: the house's clock, and the offline queue.

Point 5 asks three things of the queue: the cap holds, a 500 never loses a
valid event, and an event the server will never take does not loop forever.
The third was false: the oldest fifty were sent on every heartbeat and an
event the panel skipped - or a batch it failed on - stayed at the head for
good, with everything behind it stuck too.

Point 4: a Raspberry Pi without NTP stamps events in 1970, and the panel has no
partition for 1970. The panel's own time arrives on every answer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.haim_agent.api import ApiResponse, HaimApiError
from custom_components.haim_agent.clock import NOT_BEFORE, HouseClock, iso
from custom_components.haim_agent.const import EVENT_MAX_ATTEMPTS, EVENT_QUEUE_MAX, ISSUE_CLOCK_SKEWED

from test_coordinator import coordinator  # noqa: F401 - the fixture

SERVER_NOW = "2026-10-14T10:00:00Z"


def answer(status: int, data=None, server_time: str | None = SERVER_NOW) -> ApiResponse:
    return ApiResponse(status=status, data=data, server_time=server_time)


async def queued(coordinator, count: int) -> list[str]:
    for index in range(count):
        await coordinator.coordinator.async_queue_event("tick", "info", {"index": index})

    return [event["event_uid"] for event in await coordinator.coordinator._queue.async_peek(EVENT_QUEUE_MAX)]


class Server:
    """Takes everything except the uids it was told to refuse, and how."""

    def __init__(self, refuse: set[str] | None = None, fail_batch_with: int | None = None) -> None:
        self.refuse = refuse or set()
        self.fail_batch_with = fail_batch_with
        self.batches: list[list[str]] = []

    async def __call__(self, events):
        uids = [event["event_uid"] for event in events]
        self.batches.append(uids)

        if self.fail_batch_with and self.refuse & set(uids):
            return answer(self.fail_batch_with, {"message": "Server Error"})

        return answer(200, {"accepted": [uid for uid in uids if uid not in self.refuse], "duplicate": []})


# ------------------------------------------------------------------ the queue


async def test_the_cap_still_holds(coordinator) -> None:
    uids = await queued(coordinator, EVENT_QUEUE_MAX + 7)

    assert len(uids) == EVENT_QUEUE_MAX


async def test_a_500_loses_nothing(coordinator) -> None:
    uids = await queued(coordinator, 5)
    coordinator.api.events_response = answer(500, {"message": "Server Error"})

    for _ in range(3):
        await coordinator.coordinator.async_heartbeat()

    assert [event["event_uid"] for event in await coordinator.coordinator._queue.async_peek(50)][:1] == uids[:1]
    assert await coordinator.coordinator._queue.async_size() == 5


async def test_an_event_the_server_skips_does_not_block_the_others_forever(coordinator, monkeypatch) -> None:
    """The panel skips an event it cannot parse - no accepted, no duplicate."""
    uids = await queued(coordinator, 3)
    server = Server(refuse={uids[0]})
    monkeypatch.setattr(coordinator.api, "send_events", server)

    for _ in range(EVENT_MAX_ATTEMPTS + 2):
        await coordinator.coordinator.async_heartbeat()

    # The other two went on the first try, the bad one is gone, and it went
    # alone after its first strike rather than dragging a batch with it.
    assert await coordinator.coordinator._queue.async_size() == 0
    assert set(server.batches[0]) == set(uids)
    assert all(batch == [uids[0]] for batch in server.batches[1:])
    assert len(server.batches) == EVENT_MAX_ATTEMPTS


async def test_an_event_that_makes_the_whole_batch_fail_is_isolated_and_the_rest_delivered(
    coordinator, monkeypatch
) -> None:
    """The panel answers 500 to the whole batch because of ONE event (a 1970
    date with no partition). Valid events must all arrive; only that one goes."""
    uids = await queued(coordinator, 4)
    poison = uids[2]
    server = Server(refuse={poison}, fail_batch_with=500)
    monkeypatch.setattr(coordinator.api, "send_events", server)

    for _ in range(EVENT_MAX_ATTEMPTS * 3):
        await coordinator.coordinator.async_heartbeat()

    assert await coordinator.coordinator._queue.async_size() == 0

    delivered = {uid for batch in server.batches for uid in batch if uid != poison}
    assert delivered == set(uids) - {poison}


async def test_a_network_that_is_down_costs_no_strikes(coordinator) -> None:
    await queued(coordinator, 2)
    coordinator.api.events_response = HaimApiError("no route to host")

    for _ in range(EVENT_MAX_ATTEMPTS * 2):
        await coordinator.coordinator.async_heartbeat()

    assert await coordinator.coordinator._queue.async_size() == 2


async def test_a_heartbeat_the_panel_failed_is_not_a_success(coordinator) -> None:
    """A 500 on /state used to count as delivered: connected, and the queue
    flushed at a server that had just failed."""
    await queued(coordinator, 1)
    coordinator.api.state_response = answer(500, {"message": "Server Error"})

    assert await coordinator.coordinator.async_heartbeat() is False
    assert coordinator.coordinator.connected is False
    assert coordinator.api.events_calls == []


async def test_strikes_survive_a_restart(coordinator, monkeypatch) -> None:
    import custom_components.haim_agent.queue as queue_module

    uids = await queued(coordinator, 1)
    await coordinator.coordinator._queue.async_strike(uids)

    store = coordinator.coordinator._queue._store
    monkeypatch.setattr(queue_module, "Store", lambda *args, **kwargs: store)
    revived = queue_module.EventQueue(hass=None, site_uuid="site-uuid-1")

    await revived.async_load()

    assert revived._attempts == {uids[0]: 1}


# ------------------------------------------------------------------ the clock


def test_a_1970_clock_is_impossible_and_the_server_time_is_used() -> None:
    clock = HouseClock(wall=lambda: datetime(1970, 1, 1, tzinfo=UTC), monotonic=lambda: 100.0)

    assert clock.skewed is True
    # Before any answer there is nothing better: the local time, flagged.
    assert clock.now().year == 1970

    clock.observe(SERVER_NOW)

    assert iso(clock.now()) == SERVER_NOW


def test_the_server_estimate_moves_with_the_monotonic_clock() -> None:
    ticks = [100.0]
    clock = HouseClock(wall=lambda: datetime(2000, 1, 1, tzinfo=UTC), monotonic=lambda: ticks[0])
    clock.observe(SERVER_NOW)

    ticks[0] = 160.0

    assert iso(clock.now()) == "2026-10-14T10:01:00Z"


def test_more_than_a_day_away_from_the_server_is_impossible_either_way() -> None:
    server = datetime(2026, 10, 14, 10, tzinfo=UTC)

    for local, impossible in (
        (server + timedelta(hours=23), False),
        (server + timedelta(days=2), True),
        # fake-hwclock: the date of the last clean shutdown, weeks ago.
        (server - timedelta(days=20), True),
    ):
        clock = HouseClock(wall=lambda local=local: local, monotonic=lambda: 0.0)
        clock.observe(iso(server))

        assert clock.skewed is impossible, local


def test_a_good_clock_is_left_alone() -> None:
    local = datetime(2026, 10, 14, 10, 0, 3, tzinfo=UTC)
    clock = HouseClock(wall=lambda: local, monotonic=lambda: 0.0)
    clock.observe(SERVER_NOW)

    assert clock.now() == local
    assert NOT_BEFORE < local


async def test_a_wrong_clock_raises_a_repair_issue_and_events_carry_the_server_time(coordinator, monkeypatch) -> None:
    import custom_components.haim_agent.clock as clock_module

    coordinator.coordinator._clock = clock_module.HouseClock(
        wall=lambda: datetime(1970, 1, 1, tzinfo=UTC), monotonic=lambda: 0.0
    )

    # Queued before anything was heard from the server: stamped 1970.
    await coordinator.coordinator.async_queue_event("backup_failed", "critical")

    coordinator.api.state_response = answer(204)
    await coordinator.coordinator.async_heartbeat()

    assert ISSUE_CLOCK_SKEWED in coordinator.issues

    # Re-stamped before it left: the panel never sees 1970.
    sent = coordinator.api.events_calls[-1][0]
    assert sent["occurred_at"] == SERVER_NOW

    # The very first heartbeat goes out before anything is known about the
    # server's time, so it carries the local one; from the next, the server's.
    payload, _ = coordinator.api.state_calls[-1]
    assert payload["sent_at"].startswith("1970")

    await coordinator.coordinator.async_heartbeat()
    payload, _ = coordinator.api.state_calls[-1]
    assert payload["sent_at"] == SERVER_NOW

    await coordinator.coordinator.async_queue_event("integration_error", "info")
    assert (await coordinator.coordinator._queue.async_peek(10))[-1]["occurred_at"] == SERVER_NOW


async def test_a_good_clock_raises_nothing(coordinator) -> None:
    from custom_components.haim_agent.clock import HouseClock as Clock

    coordinator.coordinator._clock = Clock(
        wall=lambda: datetime(2026, 10, 14, 10, 0, 1, tzinfo=UTC), monotonic=lambda: 0.0
    )
    coordinator.api.state_response = answer(204)

    await coordinator.coordinator.async_heartbeat()

    assert ISSUE_CLOCK_SKEWED not in coordinator.issues


@pytest.mark.parametrize("language", ["en", "it"])
def test_the_clock_issue_has_words_in_both_languages(language: str) -> None:
    import json
    from pathlib import Path

    here = Path(__file__).resolve().parents[1] / "custom_components" / "haim_agent"
    labels = json.loads((here / "translations" / f"{language}.json").read_text())

    assert labels["issues"][ISSUE_CLOCK_SKEWED]["title"]
    assert labels["issues"][ISSUE_CLOCK_SKEWED]["description"]


# ------------------------------------------- the panel's window (prompt 05, 3)


def _house_clock_at(coordinator, moment: datetime) -> None:
    coordinator.coordinator._clock = HouseClock(wall=lambda: moment, monotonic=lambda: 0.0)


def _panel_taking_all(sent: list[str]):
    async def panel(events):
        sent.extend(event["occurred_at"] for event in events)

        return answer(200, {"accepted": [event["event_uid"] for event in events], "duplicate": []})

    return panel


async def test_an_event_answered_as_duplicate_for_being_out_of_window_leaves_for_good(coordinator, monkeypatch) -> None:
    """The panel drops an `occurred_at` outside [now - 14 days, now + 10 min]
    and answers it in `duplicate`, the word that makes the queue let go. It
    must leave the queue, and never be sent again."""
    _house_clock_at(coordinator, datetime(2026, 10, 14, 10, 0, tzinfo=UTC))
    coordinator.api.state_response = answer(204)
    uids = await queued(coordinator, 3)
    batches: list[list[str]] = []

    async def panel(events):
        sent = [event["event_uid"] for event in events]
        batches.append(sent)

        # The first one is out of the window; the others are stored.
        return answer(200, {"accepted": sent[1:], "duplicate": sent[:1]})

    monkeypatch.setattr(coordinator.api, "send_events", panel)

    await coordinator.coordinator.async_heartbeat()

    assert batches == [uids]
    assert await coordinator.coordinator._queue.async_size() == 0
    assert coordinator.coordinator._queue._attempts == {}

    for _ in range(3):
        await coordinator.coordinator.async_heartbeat()

    assert batches == [uids]


async def test_a_house_clock_running_fast_does_not_get_its_events_thrown_away(coordinator, monkeypatch) -> None:
    """Thirty minutes fast is "plausible" for the repair issue (a day either
    way), but past the panel's ten minutes: every event would come back as a
    duplicate and be gone. Re-stamped with the panel's time before sending."""
    server = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
    _house_clock_at(coordinator, server + timedelta(minutes=30))
    coordinator.api.state_response = answer(204, server_time=iso(server))
    await queued(coordinator, 1)
    sent: list[str] = []
    monkeypatch.setattr(coordinator.api, "send_events", _panel_taking_all(sent))

    await coordinator.coordinator.async_heartbeat()

    stamped = datetime.fromisoformat(sent[0].replace("Z", "+00:00"))

    assert stamped <= server + timedelta(minutes=10)
    assert ISSUE_CLOCK_SKEWED not in coordinator.issues


async def test_a_clock_a_minute_fast_is_left_alone(coordinator, monkeypatch) -> None:
    server = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
    _house_clock_at(coordinator, server + timedelta(minutes=1))
    coordinator.api.state_response = answer(204, server_time=iso(server))
    await queued(coordinator, 1)
    original = coordinator.coordinator._queue._events[0]["occurred_at"]
    sent: list[str] = []
    monkeypatch.setattr(coordinator.api, "send_events", _panel_taking_all(sent))

    await coordinator.coordinator.async_heartbeat()

    assert sent == [original]


# ------------------------------------------- verified stamps (D44)


async def test_a_house_offline_for_two_days_sends_its_events_with_their_own_time(coordinator) -> None:
    """D44: stamped by a clock that agreed with the panel, an event keeps its
    time however late it is sent. It used to be re-stamped to "now" after a
    day, so two days offline put every event at the wrong hour."""
    start = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
    ticks = [0.0]
    coordinator.coordinator._clock = HouseClock(
        wall=lambda: start + timedelta(seconds=ticks[0]), monotonic=lambda: ticks[0]
    )

    coordinator.api.state_response = answer(204, server_time=iso(start))
    await coordinator.coordinator.async_heartbeat()

    await coordinator.coordinator.async_queue_event("backup_failed", "critical")
    stamped = (await coordinator.coordinator._queue.async_peek(1))[0]["occurred_at"]

    # Two days without the panel, then it answers again.
    ticks[0] = 2 * 24 * 3600.0
    coordinator.api.state_response = answer(204, server_time=iso(start + timedelta(days=2)))
    await coordinator.coordinator.async_heartbeat()

    sent = coordinator.api.events_calls[-1][0]
    assert sent["occurred_at"] == stamped == iso(start)

    # And nothing of the bookkeeping travels.
    assert set(sent) == {"event_uid", "type", "severity", "payload", "occurred_at"}


async def test_a_clock_weeks_behind_before_the_first_answer_is_still_corrected(coordinator) -> None:
    """The case D38 was for: `fake-hwclock` restarts days ago, and an event is
    queued before the panel has said what time it is. Not verified, so judged
    again when it is sent."""
    coordinator.coordinator._clock = HouseClock(
        wall=lambda: datetime(2026, 10, 9, 8, 0, tzinfo=UTC), monotonic=lambda: 0.0
    )

    await coordinator.coordinator.async_queue_event("backup_failed", "critical")

    coordinator.api.state_response = answer(204)
    await coordinator.coordinator.async_heartbeat()

    assert coordinator.api.events_calls[-1][0]["occurred_at"] == SERVER_NOW


async def test_what_was_verified_is_remembered_on_disk(coordinator) -> None:
    coordinator.coordinator._clock = HouseClock(
        wall=lambda: datetime(2026, 10, 14, 10, 0, tzinfo=UTC), monotonic=lambda: 0.0
    )
    coordinator.api.state_response = answer(204)
    await coordinator.coordinator.async_heartbeat()

    coordinator.api.events_response = OSError("panel down")
    await coordinator.coordinator.async_queue_event("backup_failed", "critical")

    queue = coordinator.coordinator._queue
    uid = (await queue.async_peek(1))[0]["event_uid"]

    assert queue._store.saved["verified"] == [uid]
