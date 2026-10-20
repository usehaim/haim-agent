"""429 and Retry-After (prompt 05, point 2).

The panel now limits per instance - state, poll, events, command results,
decisions, credentials - and per address on failed keys. A 429 used to be just
another status: on /events the batch was counted as refused and the oldest
event took a strike, so ten rounds of being told "slow down" could throw an
event away. Now a 429 is waited out, per kind of call, with jitter, and the
queue loses nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import pytest

import custom_components.haim_agent.api as api_module
from custom_components.haim_agent.api import HaimApi, RateLimited, _retry_after

KEY = "haim_abcdef12_" + "x" * 32


class Response:
    def __init__(self, status: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Session:
    """Answers each path with whatever the test queued for it; the last one repeats."""

    def __init__(self) -> None:
        self.answers: dict[str, list[Response]] = {}
        self.calls: list[str] = []

    def answer(self, path: str, *responses: Response) -> None:
        self.answers.setdefault(path, []).extend(responses)

    def request(self, method, url, **kwargs):
        path = url.split("/api/v1/agent", 1)[1]
        self.calls.append(path)
        queued = self.answers.get(path) or [Response(204)]

        return queued.pop(0) if len(queued) > 1 else queued[0]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(api_module.time, "monotonic", lambda: now["t"])

    return now


async def test_a_429_is_waited_out_for_that_kind_of_call_only(clock) -> None:
    session = Session()
    session.answer(
        "/events",
        Response(429, {"message": "Too Many Attempts."}, {"Retry-After": "30"}),
        Response(200, {"accepted": [], "duplicate": []}),
    )
    api = HaimApi(session, "https://haim.example", agent_key=KEY)

    with pytest.raises(RateLimited) as raised:
        await api.send_events([])

    # Retry-After, plus jitter on top - never less.
    assert 30 <= raised.value.retry_after <= 30 + 3 + 1

    # Inside the wait: not even sent.
    with pytest.raises(RateLimited):
        await api.send_events([])

    assert session.calls == ["/events"]

    # Another limit entirely: the poll, which carries consent requests, goes on.
    await api.poll()
    assert session.calls == ["/events", "/poll"]

    clock["t"] += 40
    response = await api.send_events([])

    assert response.status == 200
    assert session.calls == ["/events", "/poll", "/events"]


async def test_houses_limited_together_do_not_come_back_together(clock) -> None:
    waits = set()

    for _ in range(20):
        session = Session()
        session.answer("/state", Response(429, None, {"Retry-After": "60"}))

        with pytest.raises(RateLimited) as raised:
            await HaimApi(session, "https://haim.example", agent_key=KEY).send_state({}, "0" * 64)

        waits.add(round(raised.value.retry_after, 3))

    assert len(waits) > 1
    assert all(60 <= wait <= 67 for wait in waits)


@pytest.mark.parametrize(
    ("header", "low", "high"),
    [
        ("12", 12, 12),
        (None, 60, 60),
        ("soon", 60, 60),
        ("0", 1, 1),
        ("-5", 1, 1),
        ("86400", 900, 900),
    ],
)
def test_retry_after_is_read_within_reason(header, low, high) -> None:
    assert low <= _retry_after(header) <= high


def test_retry_after_as_an_http_date() -> None:
    later = datetime.now(UTC) + timedelta(seconds=120)

    assert 100 <= _retry_after(format_datetime(later, usegmt=True)) <= 121


async def test_a_429_on_events_costs_the_queue_nothing(monkeypatch, clock) -> None:
    """The bug: the 429 was read as a refused batch and the oldest event took
    a strike. Ten of those and it was gone."""
    import custom_components.haim_agent.queue as queue_module
    from conftest import FakeStore
    from custom_components.haim_agent.coordinator import HaimCoordinator

    FakeStore.files.clear()
    monkeypatch.setattr(queue_module, "Store", FakeStore)

    session = Session()
    session.answer("/state", Response(204))
    session.answer("/events", Response(429, None, {"Retry-After": "60"}))
    api = HaimApi(session, "https://haim.example", agent_key=KEY)

    coordinator = HaimCoordinator(hass=None, api=api, site_uuid="rate-limited")

    async def state():
        return {"counts": {"entities": 1}}

    async def volatile():
        return {}

    monkeypatch.setattr(coordinator._collector, "async_build_state", state)
    monkeypatch.setattr(coordinator._collector, "async_build_volatile", volatile)

    await coordinator.async_queue_event("backup_failed", "warning")
    uid = coordinator._queue._events[0]["event_uid"]

    for _ in range(12):
        await coordinator.async_heartbeat()
        clock["t"] += 5

    # Sent once; every later heartbeat stayed inside the wait and did not try.
    assert session.calls.count("/events") == 1
    assert coordinator._queue._attempts == {}
    assert [event["event_uid"] for event in coordinator._queue._events] == [uid]

    # Once the wait is over, the same event goes, and goes through.
    clock["t"] += 120
    session.answers["/events"] = [Response(200, {"accepted": [uid], "duplicate": []})]

    await coordinator.async_heartbeat()

    assert session.calls.count("/events") == 2
    assert await coordinator._queue.async_size() == 0


async def test_a_429_on_the_poll_is_not_a_command(clock) -> None:
    """A 429 carries a JSON body, and it must never reach the dispatcher as if
    it were the command."""
    from custom_components.haim_agent.command_channel import CommandDispatcher, HttpPollChannel

    session = Session()
    session.answer("/poll", Response(429, {"action": "self_test", "command_uuid": "x"}, {"Retry-After": "10"}))
    dispatcher = CommandDispatcher(HttpPollChannel(HaimApi(session, "https://haim.example", agent_key=KEY)))
    ran: list[str] = []

    async def handler(_data):
        ran.append("self_test")

        return {}

    dispatcher.register("self_test", handler)

    assert await dispatcher.async_poll_once() is None
    assert ran == []
