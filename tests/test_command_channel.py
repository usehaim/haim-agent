"""The command channel: dispatch, refusal, and reporting that fails."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.haim_agent.api import HaimApiError, TokenRevoked
from custom_components.haim_agent.command_channel import CommandChannel, CommandDispatcher, field


class FakeChannel(CommandChannel):
    def __init__(self, commands: list[dict[str, Any]] | None = None, report_error: Exception | None = None) -> None:
        self._commands = commands or []
        self._report_error = report_error
        self.reports: list[tuple[str, str, str | None, dict | None]] = []

    async def async_receive(self):
        return self._commands.pop(0) if self._commands else None

    async def async_report(self, command_uuid, status, error=None, data=None):
        if self._report_error is not None:
            raise self._report_error

        self.reports.append((command_uuid, status, error, data))


@pytest.fixture
def dispatcher():
    channel = FakeChannel()
    dispatcher = CommandDispatcher(channel)

    return dispatcher, channel


async def test_it_runs_a_known_command_and_reports_both_ends(dispatcher) -> None:
    dispatch, channel = dispatcher
    ran: list[dict] = []

    async def handler(data: dict) -> dict:
        ran.append(data)

        return {"ok": True}

    dispatch.register("self_test", handler)

    action = await dispatch.async_dispatch(
        {"action": "self_test", "command_uuid": "abc", "data": {"why": "installer asked"}}
    )

    assert action == "self_test"
    assert ran == [{"why": "installer asked"}]
    # Started and then succeeded: the cloud's watchdog needs to see the first
    # one, or a slow command is indistinguishable from a house with no network.
    assert [report[1] for report in channel.reports] == ["started", "succeeded"]
    assert channel.reports[-1][3] == {"ok": True}


async def test_an_unknown_command_is_refused_and_not_ignored(dispatcher) -> None:
    dispatch, channel = dispatcher

    await dispatch.async_dispatch({"action": "open_the_pod_bay_doors", "command_uuid": "abc"})

    # Ignoring it would leave the cloud waiting for an answer that never comes,
    # until the watchdog calls it expired - which looks exactly like a house
    # with no network. "Too old to do this" is something an installer can act on,
    # and since contract 1.6.0 it is a code the panel puts into words.
    assert len(channel.reports) == 1
    assert channel.reports[0][1] == "failed"
    assert channel.reports[0][2] == "unsupported_action"


async def test_a_handler_that_raises_is_reported_as_failed(dispatcher) -> None:
    dispatch, channel = dispatcher

    async def broken(_data: dict) -> dict:
        raise RuntimeError("the serial port is not there any more")

    dispatch.register("clear_logs", broken)

    await dispatch.async_dispatch({"action": "clear_logs", "command_uuid": "abc"})

    assert [report[1] for report in channel.reports] == ["started", "failed"]
    # The kind of failure, not its text (verifica 03, sixth review): the text of
    # an exception raised inside Home Assistant can name anything in the house.
    assert channel.reports[-1][2] == "command_failed:RuntimeError"


async def test_idle_costs_nothing_and_says_nothing(dispatcher) -> None:
    dispatch, channel = dispatcher

    action = await dispatch.async_dispatch({"action": "idle", "command_uuid": "abc"})

    assert action == "idle"
    assert channel.reports == []


async def test_reporting_failing_does_not_bring_the_poll_down() -> None:
    channel = FakeChannel(report_error=HaimApiError("connection reset"))
    dispatch = CommandDispatcher(channel)

    async def handler(_data: dict) -> dict:
        return {}

    dispatch.register("self_test", handler)

    # The command may well have run. Losing the polling loop because we could
    # not say so would turn one lost answer into an instance that stops
    # listening altogether.
    action = await dispatch.async_dispatch({"action": "self_test", "command_uuid": "abc"})

    assert action == "self_test"


async def test_nothing_to_do_is_the_normal_answer() -> None:
    dispatch = CommandDispatcher(FakeChannel([]))

    assert await dispatch.async_poll_once() is None


async def test_a_revoked_key_is_not_swallowed_by_the_poll() -> None:
    class Revoked(FakeChannel):
        async def async_receive(self):
            raise TokenRevoked("revoked")

    dispatch = CommandDispatcher(Revoked())

    # The coordinator owns this decision - pause, and tell the household. The
    # poll must not quietly absorb it and carry on asking forever.
    with pytest.raises(TokenRevoked):
        await dispatch.async_poll_once()


async def test_a_command_with_no_action_is_dropped(dispatcher) -> None:
    dispatch, channel = dispatcher

    assert await dispatch.async_dispatch({"command_uuid": "abc"}) is None
    assert channel.reports == []


@pytest.mark.parametrize(
    "command",
    [
        {"action": "self_test", "command_uuid": "abc", "data": {"x": 1}},
        {"action": "self_test", "commandUuid": "abc", "data": {"x": 1}},
    ],
)
async def test_it_reads_the_fields_under_either_convention(command: dict) -> None:
    channel = FakeChannel()
    dispatch = CommandDispatcher(channel)

    async def handler(_data: dict) -> dict:
        return {}

    dispatch.register("self_test", handler)

    # Three lines, and it makes the component immune to a change of convention
    # on the server - which with a client installed in fifty houses that update
    # when they feel like it is not a theoretical problem.
    assert await dispatch.async_dispatch(command) == "self_test"
    assert [report[1] for report in channel.reports] == ["started", "succeeded"]


def test_the_field_reader_understands_both_spellings() -> None:
    assert field({"command_uuid": "a"}, "command_uuid") == "a"
    assert field({"commandUuid": "a"}, "command_uuid") == "a"
    assert field({"expiresAt": "t"}, "expires_at") == "t"
    assert field({}, "missing") is None
