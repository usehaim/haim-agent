"""The offline queue: what happened while nobody could hear us.

Persisted in Home Assistant's own Store, so it survives a restart - which is
precisely the case it exists for, since a restart is one of the things worth
telling the installer about and also one of the things that would lose the
telling.

Capped at two hundred, oldest discarded first. An instance back from three days
offline must not be able to push tens of thousands of events at once; if it has
been away that long, the twenty most recent transitions carry every bit of the
story that the four hundredth does not.

Every event carries a `event_uid` generated here and kept across retries. The
server dedupes on it, so sending the same batch twice - which is exactly what a
failed POST over a bad connection looks like - is safe by design rather than by
luck.

And one event the server will never take must not hold the others hostage
(verifica 03). Before, the oldest fifty were sent every time; if one of them
was refused for good - a malformed timestamp, a 1970 date with no partition to
go into - the same fifty went out every heartbeat, forever, and nothing behind
them ever left. Now:

  - an event the server answered for, but neither accepted nor called a
    duplicate, has one strike against it;
  - a batch that fails outright (5xx, a proxy page) puts a strike on its
    OLDEST event only, and the next attempt sends that event alone. Valid
    events are never punished for a neighbour: alone, they go through;
  - an event with EVENT_MAX_ATTEMPTS strikes is dropped and logged.

Strikes are only ever counted after a heartbeat has just succeeded (the flush
runs only then), so a server that is down - not answering at all - costs
nothing: that is a network error, not an answer, and it is not counted.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    EVENT_MAX_ATTEMPTS,
    EVENT_PAYLOAD_MAX_BYTES,
    EVENT_QUEUE_MAX,
    STORAGE_KEY_QUEUE,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# The panel's own vocabulary (panel-b, point 10): what the SERVER decides from
# what it sees - silence, flapping, an alert firing or clearing. The panel now
# stores every event with its source, and one posted by an agent under these
# names would read as the agent claiming to be the server. This component
# never sends them. What it may send are its own names for what only the house
# can see - `backup_failed`, `integration_error`, `update_available` (§4 of the
# architecture) - and they are never these.
SERVER_EVENT_TYPES = frozenset(
    {
        "went_offline",
        "went_frozen",
        "degraded",
        "came_online",
        "site_unstable",
        "monitoring_disabled",
        "never_seen",
    }
)
SERVER_EVENT_PREFIXES = ("alert_fired_", "alert_cleared_")


def is_server_event_type(event_type: Any) -> bool:
    return isinstance(event_type, str) and (
        event_type in SERVER_EVENT_TYPES or event_type.startswith(SERVER_EVENT_PREFIXES)
    )


class EventQueue:
    """A small, persistent, capped FIFO."""

    def __init__(self, hass: HomeAssistant, site_uuid: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_QUEUE}.{site_uuid or 'default'}"
        )
        self._events: list[dict[str, Any]] = []
        self._attempts: dict[str, int] = {}
        # Uids whose `occurred_at` was stamped by a clock that agreed with the
        # panel (D44). Kept beside the events, not in them: nothing of it is
        # sent.
        self._verified: set[str] = set()
        self._loaded = False

    async def async_load(self) -> None:
        if self._loaded:
            return

        stored = await self._store.async_load() or {}
        events = stored.get("events")

        self._events = [event for event in events if isinstance(event, dict)] if isinstance(events, list) else []

        # Queued by an older version, before payloads were bounded and before
        # the server's event types were off limits.
        self._events = [event for event in self._events if not is_server_event_type(event.get("type"))]

        for event in self._events:
            if isinstance(event.get("payload"), dict):
                event["payload"] = bounded_payload(event["payload"])

        # The cap holds on the way in from disk too: a store written by another
        # version, or by hand, is not allowed to be bigger than the queue.
        self._events = self._events[-EVENT_QUEUE_MAX:]

        attempts = stored.get("attempts")
        self._attempts = (
            {str(uid): int(count) for uid, count in attempts.items() if isinstance(count, int)}
            if isinstance(attempts, dict)
            else {}
        )
        verified = stored.get("verified")
        queued = {str(event.get("event_uid")) for event in self._events}
        self._verified = (
            {str(uid) for uid in verified if str(uid) in queued} if isinstance(verified, list) else set()
        )
        self._loaded = True

    async def async_add(
        self,
        event_type: str,
        severity: str,
        payload: dict[str, Any] | None = None,
        occurred_at: str | None = None,
        verified: bool = False,
    ) -> dict[str, Any]:
        """Queue one transition. Returns the event as it will be sent.

        `occurred_at` comes from the coordinator's HouseClock, which knows when
        the local clock is impossible. The local clock is the fallback only.

        A type from the server's vocabulary is refused outright: it is a
        mistake in this component, and the test suite is where it shows.
        """
        if is_server_event_type(event_type):
            raise ValueError(f"'{event_type}' is the panel's event type, not one an agent may send.")

        await self.async_load()

        event = {
            "event_uid": str(uuid.uuid4()),
            "type": event_type,
            "severity": severity if severity in ("info", "warning", "critical") else "info",
            "payload": bounded_payload(payload),
            "occurred_at": occurred_at or datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        }

        self._events.append(event)

        if verified:
            self._verified.add(event["event_uid"])

        if len(self._events) > EVENT_QUEUE_MAX:
            dropped = len(self._events) - EVENT_QUEUE_MAX

            # The oldest go, not the newest. What happened five minutes ago is
            # more use than what happened three days ago, and the cap has to
            # bite somewhere.
            self._events = self._events[dropped:]
            self._verified &= {str(event.get("event_uid")) for event in self._events}

            _LOGGER.debug("Event queue full, discarded %s of the oldest", dropped)

        await self._async_save()

        return event

    async def async_peek(self, limit: int) -> list[dict[str, Any]]:
        """The oldest `limit` events, without removing them.

        Nothing is removed until the server has said it took them. A queue that
        empties on send loses everything to one timeout.

        If the oldest has a strike against it, it goes ALONE: either it is the
        one the server will not take, and it is isolated; or it is not, and it
        goes through on its own and the next batch is full again.
        """
        await self.async_load()

        if self._events and self._attempts.get(str(self._events[0].get("event_uid")), 0) > 0:
            return self._events[:1] if limit > 0 else []

        return self._events[: max(0, limit)]

    async def async_strike(self, uids: list[str]) -> list[str]:
        """One strike each. Returns the uids dropped for having too many."""
        await self.async_load()

        dropped: list[str] = []

        for uid in uids:
            count = self._attempts.get(uid, 0) + 1
            self._attempts[uid] = count

            if count >= EVENT_MAX_ATTEMPTS:
                dropped.append(uid)

        if dropped:
            _LOGGER.warning(
                "Dropped %s event(s) the HAIM panel refused %s times in a row", len(dropped), EVENT_MAX_ATTEMPTS
            )
            gone = set(dropped)
            self._events = [event for event in self._events if event.get("event_uid") not in gone]

            for uid in dropped:
                self._attempts.pop(uid, None)
                self._verified.discard(uid)

        await self._async_save()

        return dropped

    async def async_retime(self, fix) -> None:
        """Rewrite `occurred_at` where `fix` says it must change.

        `fix(occurred_at, verified) -> str | None`; None leaves the event
        alone. Used to correct events stamped by a clock that was later found
        to be wrong; `verified` says the stamp was checked when it was made.
        """
        await self.async_load()

        changed = False

        for event in self._events:
            corrected = fix(event.get("occurred_at"), str(event.get("event_uid")) in self._verified)

            if corrected is not None and corrected != event.get("occurred_at"):
                event["occurred_at"] = corrected
                changed = True

        if changed:
            await self._async_save()

    async def async_acknowledge(self, uids: list[str]) -> None:
        """Drop the events the server has confirmed - accepted or duplicate.

        Duplicates count as done: it means an earlier attempt got through and
        only the answer was lost, which is the single most common way this goes
        wrong on a bad connection.
        """
        await self.async_load()

        confirmed = set(uids)
        self._events = [event for event in self._events if event.get("event_uid") not in confirmed]

        for uid in confirmed:
            self._attempts.pop(uid, None)
            self._verified.discard(uid)

        await self._async_save()

    async def async_size(self) -> int:
        await self.async_load()

        return len(self._events)

    async def _async_save(self) -> None:
        await self._store.async_save(
            {"events": self._events, "attempts": self._attempts, "verified": sorted(self._verified)}
        )


def _encoded_size(value: Any) -> int:
    """Bytes as the panel measures them: PHP's json_encode with default flags.

    Same as Python's compact, ASCII-only JSON, except that PHP also escapes
    every forward slash.
    """
    text = json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)

    return len(text) + text.count("/")


def _shorten(value: Any, width: int) -> Any:
    """Every string cut to `width`, every list to twenty items, all the way down."""
    if isinstance(value, str):
        return value if len(value) <= width else value[: max(1, width - 1)] + "…"

    if isinstance(value, dict):
        return {_shorten(str(key), 64): _shorten(item, width) for key, item in value.items()}

    if isinstance(value, (list, tuple)):
        return [_shorten(item, width) for item in list(value)[:20]]

    if value is None or isinstance(value, (bool, int, float)):
        return value

    return _shorten(str(value), width)


def bounded_payload(payload: Any) -> dict[str, Any]:
    """The payload, small enough for the panel to keep, and marked when cut.

    In steps, keeping as much as fits: first long strings and long lists are
    cut shorter and shorter; if that is not enough - hundreds of keys - only
    the top-level values that fit are kept, in their order. Whatever was cut
    says so with `"truncated": true`, the marker the panel itself would put in
    place of the whole payload.
    """
    if not isinstance(payload, dict):
        return {}

    if _encoded_size(payload) <= EVENT_PAYLOAD_MAX_BYTES:
        return payload

    for width in (512, 128, 32):
        shortened = {**_shorten(payload, width), "truncated": True}

        if _encoded_size(shortened) <= EVENT_PAYLOAD_MAX_BYTES:
            return shortened

    kept: dict[str, Any] = {"truncated": True}

    for key, value in _shorten(payload, 32).items():
        candidate = {**kept, key: value}

        if _encoded_size(candidate) <= EVENT_PAYLOAD_MAX_BYTES:
            kept = candidate

    return kept
