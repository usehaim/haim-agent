"""How the cloud asks this instance to do something.

An interface with exactly one implementation, which looks like over-design and
is not. Past roughly two thousand instances the sensible transport is MQTT -
persistent sessions, and a Last Will that makes an instance going offline
instant rather than a thing discovered three intervals later. Adding
`MqttChannel` then should touch this file and nothing else, and that is only
true if the seam exists from the start.

Long polling is deliberately excluded on the other side: PHP holds a worker for
the whole duration of a request, so a long poll per instance is a worker per
instance. That is not a tuning problem, it is the execution model.

Polling is fixed at thirty seconds with no night-time backoff. It is the cloud
that knows a consent request is pending, not the instance: with a two minute
backoff a request made at eleven at night would reach the house two minutes
later, four times the requirement.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .api import HaimApi, HaimApiError, SiteInactive, TokenRevoked
from .redaction import redact

_LOGGER = logging.getLogger(__name__)

CommandHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class CommandChannel(ABC):
    """One way of receiving commands and reporting what became of them."""

    @abstractmethod
    async def async_receive(self) -> dict[str, Any] | None:
        """The next command, or None when there is nothing to do."""

    @abstractmethod
    async def async_report(self, command_uuid: str, status: str, error: str | None = None, data: dict[str, Any] | None = None) -> None:
        """Say what happened. Silence is treated as failure on the far side."""


class HttpPollChannel(CommandChannel):
    """The only implementation in v1: ask every thirty seconds."""

    def __init__(self, api: HaimApi) -> None:
        self._api = api

    async def async_receive(self) -> dict[str, Any] | None:
        response = await self._api.poll()

        if response.status == 204 or not response.data:
            return None

        return response.data

    async def async_report(
        self,
        command_uuid: str,
        status: str,
        error: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        await self._api.report_command(
            command_uuid,
            {"status": status, "error": error, "data": data or {}},
        )


def _error_code(error: Exception) -> str:
    """What the panel is told about a failure: a fixed code, nothing more.

    `code` when the exception carries one of ours, otherwise the exception's
    CLASS name - `ValueError`, `TimeoutError` - which says what kind of thing
    went wrong and nothing about whose house it went wrong in.
    """
    code = getattr(error, "code", None)

    if isinstance(code, str) and code.isidentifier():
        return code

    if isinstance(error, TimeoutError):
        return "timeout"

    return "command_failed:" + type(error).__name__


def field(command: dict[str, Any], *names: str) -> Any:
    """Reads a field under any of the names it might arrive with.

    Both snake_case and camelCase are accepted. It costs three lines and it
    makes this component immune to a change of convention on the server - which,
    with a client installed in fifty houses that update when they feel like it,
    is not a theoretical problem.
    """
    for name in names:
        if name in command:
            return command[name]

        camel = "".join(part.capitalize() if index else part for index, part in enumerate(name.split("_")))

        if camel in command:
            return command[camel]

    return None


class CommandDispatcher:
    """Routes one command to its handler and reports the outcome.

    An unknown command is REJECTED, never ignored. Ignoring it leaves the cloud
    waiting for an answer that will never come, and the watchdog eventually
    calls it expired - which looks identical to a house with no network. The
    installer deserves to be told "that instance is too old to do this" rather
    than left guessing.
    """

    def __init__(self, channel: CommandChannel) -> None:
        self._channel = channel
        self._handlers: dict[str, CommandHandler] = {}
        self._refused_when_late: set[str] = set()

    def register(self, action: str, handler: CommandHandler, *, refuse_when_late: bool = False) -> None:
        """`refuse_when_late`: a command that reaches this house after its own
        `expires_at` is refused instead of run. Opt-in, per action: for most
        commands a late arrival is harmless and a house with a wrong clock would
        refuse them all. For the one that opens a window it is not harmless - a
        late "let them in" is one nobody is waiting on any more (D37).
        """
        self._handlers[action] = handler

        if refuse_when_late:
            self._refused_when_late.add(action)
        else:
            self._refused_when_late.discard(action)

    @property
    def actions(self) -> list[str]:
        return sorted(self._handlers)

    async def async_poll_once(self) -> str | None:
        """Ask for a command, run it, report. Returns the action, if any."""
        try:
            command = await self._channel.async_receive()
        except (TokenRevoked, SiteInactive):
            # The coordinator owns these two; the poll must not swallow them.
            raise
        except (HaimApiError, OSError):
            _LOGGER.debug("Could not poll for commands", exc_info=True)

            return None

        if not command:
            return None

        return await self.async_dispatch(command)

    async def async_dispatch(self, command: dict[str, Any]) -> str | None:
        action = field(command, "action")
        command_uuid = field(command, "command_uuid")
        data = field(command, "data") or {}

        if not isinstance(action, str) or not isinstance(command_uuid, str):
            _LOGGER.debug("Ignoring a command with no action or no id")

            return None

        if action == "idle":
            return action

        handler = self._handlers.get(action)

        if handler is None:
            await self._report(
                command_uuid,
                "failed",
                error=f"This version of the HAIM component does not support '{action}'.",
            )

            return action

        if action in self._refused_when_late and _is_late(field(command, "expires_at")):
            await self._report(
                command_uuid,
                "failed",
                error=f"'{action}' arrived after it expired, and was not carried out.",
            )

            return action

        await self._report(command_uuid, "started")

        try:
            result = await handler(data if isinstance(data, dict) else {})
        except Exception as error:  # noqa: BLE001
            _LOGGER.debug("Command %s failed", action, exc_info=True)
            # A code, not the exception's text (sixth review): the text of an
            # exception raised inside Home Assistant names entities, paths and
            # URLs. The detail stays in the local debug log.
            await self._report(command_uuid, "failed", error=_error_code(error))

            return action

        await self._report(command_uuid, "succeeded", data=result)

        return action

    async def async_report_item(self, uuid: str, status: str, error: str | None = None) -> None:
        """Report on something that is not the command itself.

        An update batch arrives as ONE command carrying N items, and each item
        has its own uuid because a batch that updates Core restarts Home
        Assistant halfway through - a single report at the end would never be
        sent. The far side scopes both kinds of uuid to this instance, so there
        is nothing here that could be pointed at somebody else's house.
        """
        await self._report(uuid, status, error=error)

    async def _report(
        self,
        command_uuid: str,
        status: str,
        error: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Reporting that fails must not bring the poll down with it.

        The command may well have run. Losing the polling loop because we could
        not say so would turn one lost answer into an instance that stops
        listening altogether.
        """
        # An error is an exception's text, and Home Assistant's exceptions name
        # the entity that failed - `light.camera_di_giulia` - and sometimes the
        # URL, password included, they could not reach. It goes through the
        # same filter as a log line (verifica 03).
        if error is not None:
            error = redact(error)[:300]

        try:
            await self._channel.async_report(command_uuid, status, error, data)
        except (HaimApiError, OSError):
            _LOGGER.debug("Could not report the outcome of %s", command_uuid, exc_info=True)


def _is_late(expires_at: Any) -> bool:
    """True when the command's own deadline is behind us. Unreadable is late.

    A command marked to be refused when late carries a deadline in every
    version of the contract (`Command.expires_at` is required). One that comes
    without a readable one is not taken on trust.
    """
    if not isinstance(expires_at, str) or not expires_at:
        return True

    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed <= datetime.now(timezone.utc)
