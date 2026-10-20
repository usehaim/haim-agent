"""The heartbeat loop: build, hash, send, and decide what to do with the answer.

Sends every five minutes by default, at an offset derived from this instance's
own identifier so that three hundred houses updated by HACS on the same evening
do not all report in the same thirty seconds.

The four answers it has to handle are genuinely different, and treating any two
of them the same is how an instance ends up either silent or hammering:

    204  nothing changed. The normal case, roughly nine sends in ten.
    200  recorded, and the config block in the body may have moved.
    401  token_revoked: stop. Raise a repair issue in the house so somebody
         sees it, and do not retry - the answer will not change on its own.
    410  archived or the plan lapsed: pause, and come back in six hours. Not
         thirty seconds: nothing that happens here changes that answer either.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import timedelta
from typing import Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from .api import HaimApi, HaimApiError, SiteInactive, TokenRevoked
from .clock import HouseClock, iso, parse
from .collector import StateCollector
from .const import (
    AGENT_KEY_PATTERN,
    DEFAULT_EVENT_BATCH_MAX,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_LOG_LEVEL,
    DEFAULT_STATE_INTERVAL,
    DOMAIN,
    ISSUE_CLOCK_SKEWED,
    ISSUE_SITE_INACTIVE,
    ISSUE_TOKEN_REVOKED,
    LOG_LEVELS,
    PAUSED_RETRY_SECONDS,
    ROTATED_KEY_ADOPT_AFTER_SECONDS,
)
from .hashing import state_hash
from .jitter import offset_seconds
from .queue import EventQueue

_LOGGER = logging.getLogger(__name__)


class HaimCoordinator:
    """Owns the schedule, the queue and the connection state."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: HaimApi,
        site_uuid: str,
        log_level: str = DEFAULT_LOG_LEVEL,
        pseudonym_key: str | None = None,
        persist_key: Callable[[str], None] | None = None,
    ) -> None:
        self._hass = hass
        self._api = api
        # Writes a rotated key into the config entry. Without it a rotated key
        # is never used: the old one keeps working through its overlap, which
        # is better than a key that lives only in memory and is gone at the
        # next restart.
        self._persist_key = persist_key
        self._pending_key: str | None = None
        self._pending_since = 0.0
        self._site_uuid = site_uuid
        # What the house chose. The panel may send less, never more (D41).
        self._log_ceiling = log_level if log_level in LOG_LEVELS else DEFAULT_LOG_LEVEL
        self._collector = StateCollector(hass, self._log_ceiling, pseudonym_key)
        self._queue = EventQueue(hass, site_uuid)
        self._clock = HouseClock()
        self._clock_issue: bool | None = None

        self._state_interval = DEFAULT_STATE_INTERVAL
        self._poll_interval = DEFAULT_POLL_INTERVAL
        self._event_batch_max = DEFAULT_EVENT_BATCH_MAX
        self._config_version: str | None = None

        self._connected = False
        self._paused = False
        self._last_success: Any = None
        self._last_error: str | None = None
        self._cancel_timer: Any = None
        self._cancel_poll: Any = None
        self._dispatcher: Any = None
        self._listeners: list[Any] = []

    # -------------------------------------------------------------- listeners

    def add_listener(self, listener: Any) -> None:
        """Entities subscribe so they update the moment something changes.

        Push rather than poll: an entity that polled the coordinator would show
        a state up to thirty seconds stale, and the one entity a household looks
        at is the one that says whether this thing is talking right now.
        """
        self._listeners.append(listener)

    def remove_listener(self, listener: Any) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("An entity listener failed", exc_info=True)

    # ------------------------------------------------------------------ state

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def last_success(self) -> Any:
        return self._last_success

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def site_uuid(self) -> str:
        return self._site_uuid

    # ---------------------------------------------------------------- lifecycle

    def attach_commands(self, dispatcher: Any) -> None:
        self._dispatcher = dispatcher

    def attach_consent(self, consent: Any) -> None:
        """The household's side of consent, so an entity can read it.

        The callback is what makes the entity move. Consent changes when a
        command arrives or when somebody taps a button, neither of which is a
        heartbeat - without this the sensor would only catch up on the next one,
        up to thirty seconds after the household was actually asked.
        """
        self._consent = consent
        consent.on_change(self._notify)

    @property
    def consent(self) -> Any:
        return getattr(self, "_consent", None)

    async def async_start(self) -> None:
        """Send once now, then settle into the interval at our own offset."""
        await self._queue.async_load()
        await self.async_heartbeat()
        self._schedule()
        self._schedule_poll()

    @callback
    def async_stop(self) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

        if self._cancel_poll is not None:
            self._cancel_poll()
            self._cancel_poll = None

    def _schedule_poll(self) -> None:
        if self._cancel_poll is not None:
            self._cancel_poll()

        # Fixed at thirty seconds, no night-time backoff. It is the cloud that
        # knows a request is pending, not us.
        self._cancel_poll = async_track_time_interval(
            self._hass,
            self._async_poll_tick,
            timedelta(seconds=self._poll_interval),
        )

    async def _async_poll_tick(self, _now: Any) -> None:
        await self.async_poll_once()

    def _schedule(self) -> None:
        self.async_stop()

        if self._paused:
            # Paused means paused: no interval at all, one delayed retry.
            self._cancel_timer = async_call_later(
                self._hass, PAUSED_RETRY_SECONDS, self._async_resume
            )

            return

        self._cancel_timer = async_track_time_interval(
            self._hass,
            self._async_tick,
            timedelta(seconds=self._state_interval),
        )

    async def _async_resume(self, _now: Any) -> None:
        self._paused = False
        await self.async_heartbeat()
        self._schedule()

    async def _async_tick(self, _now: Any) -> None:
        await self.async_heartbeat()

    async def async_resolve_update_entity(self, entity_id: Any) -> str | None:
        """See StateCollector.resolve_update_entity.

        Right after a start no round has run yet, and the panel may already be
        acting on the state it got before the restart: one round first.
        """
        if not self._collector.updates_known:
            await self._collector.async_build_state()

        return self._collector.resolve_update_entity(entity_id)

    def set_log_level(self, log_level: str) -> None:
        """Change how much of the log leaves the house, at once.

        Reached from a config_push command so that a change made in the panel
        takes effect within thirty seconds rather than at the next heartbeat.
        Never above what the house chose: the panel turning `counts` into
        `full` would send out what the family was told stays home, while the
        notice in their Repairs still said otherwise (D41).
        """
        self._collector.with_log_level(self.capped_log_level(log_level))

    def capped_log_level(self, log_level: str) -> str:
        if log_level not in LOG_LEVELS:
            return self._log_ceiling

        return min(log_level, self._log_ceiling, key=LOG_LEVELS.index)

    @property
    def offset(self) -> int:
        """Where inside the interval this instance sends. Stable forever."""
        return offset_seconds(self._site_uuid, self._state_interval)

    # -------------------------------------------------------------------- poll

    async def async_poll_once(self) -> str | None:
        """One trip round the command channel.

        Separated from the heartbeat because the two run at different rates for
        different reasons: telemetry every five minutes because that is often
        enough to notice a problem, commands every thirty seconds because a
        consent request made at eleven at night has to reach the house while
        somebody is still holding their phone.
        """
        if self._paused or self._dispatcher is None:
            return None

        try:
            return await self._dispatcher.async_poll_once()
        except TokenRevoked:
            self._paused = True
            self._last_error = "token_revoked"
            self._raise_issue(ISSUE_TOKEN_REVOKED)
            self._schedule()
        except SiteInactive:
            self._paused = True
            self._last_error = "site_inactive"
            self._raise_issue(ISSUE_SITE_INACTIVE)
            self._schedule()

        return None

    # ---------------------------------------------------------------- heartbeat

    async def async_heartbeat(self) -> bool:
        """One send. Returns whether the cloud heard us."""
        if self._paused:
            return False

        try:
            state = await self._collector.async_build_state()
            volatile = await self._collector.async_build_volatile()
        except Exception:  # noqa: BLE001
            # The collector already guards every field; reaching here means
            # something structural went wrong, and it is still not a reason to
            # stop the integration.
            _LOGGER.exception("Could not build the telemetry payload")

            return False

        payload = {
            "sent_at": iso(self._clock.now()),
            "event_uid": self._new_uid(),
            "state": state,
            "volatile": volatile,
        }

        try:
            response = await self._api.send_state(payload, state_hash(state))
        except TokenRevoked:
            # Stop, and make sure somebody in the house can see why.
            self._connected = False
            self._last_error = "token_revoked"
            self._paused = True
            self._raise_issue(ISSUE_TOKEN_REVOKED)
            self._schedule()
            self._notify()

            return False
        except SiteInactive:
            self._connected = False
            self._last_error = "site_inactive"
            self._paused = True
            self._raise_issue(ISSUE_SITE_INACTIVE)
            self._schedule()
            self._notify()

            return False
        except (HaimApiError, OSError) as error:
            # A network that comes and goes is the normal condition of a house,
            # not an incident. Note it and try again next interval.
            self._connected = False
            self._last_error = type(error).__name__
            _LOGGER.debug("Heartbeat failed", exc_info=True)
            self._notify()

            return False

        self._observe_clock(response.server_time)

        if response.status not in (200, 204):
            # A 500, a 422, a proxy's 502: the panel did NOT take this. It used
            # to be counted as a success - "connected", and the queue flushed
            # at a server that had just failed (verifica 03).
            self._connected = False
            self._last_error = f"http_{response.status}"
            _LOGGER.debug("Heartbeat answered with %s", response.status)
            self._notify()

            return False

        self._connected = True
        self._last_error = None
        self._last_success = dt_util.utcnow()

        self._absorb_config(response.data, response.config_version)
        self._absorb_rotated_key(response.rotated_key)

        self._notify()

        await self._async_flush_queue()

        return True

    # -------------------------------------------------------------------- queue

    async def async_queue_event(self, event_type: str, severity: str, payload: dict[str, Any] | None = None) -> None:
        moment, verified = self._clock.stamp()

        await self._queue.async_add(event_type, severity, payload, iso(moment), verified)

    async def _async_flush_queue(self) -> None:
        """Hand over whatever has been waiting, oldest first."""
        if self._clock.can_correct:
            # Events stamped before the server's time was known, by a clock
            # that has since been found impossible, are re-stamped now. Not
            # exact - the event happened earlier than this - but a time the
            # panel can store, rather than a 1970 that fails the whole batch.
            await self._queue.async_retime(self._retime)

        events = await self._queue.async_peek(self._event_batch_max)

        if not events:
            return

        try:
            response = await self._api.send_events(events)
        except (HaimApiError, OSError):
            # Left in the queue on purpose. Nothing is dropped because the
            # network was briefly unavailable.
            _LOGGER.debug("Could not flush the event queue", exc_info=True)

            return

        self._observe_clock(response.server_time)

        data = response.data if response.status == 200 and isinstance(response.data, dict) else None

        if data is None:
            # The batch failed as a whole. One strike on the oldest only, which
            # then goes alone next time (queue.py): a neighbour of the event
            # the server cannot take is never punished for it.
            _LOGGER.debug("Event batch answered with %s", response.status)
            await self._queue.async_strike([str(events[0].get("event_uid"))])

            return

        accepted = _uids(data.get("accepted"))
        duplicate = _uids(data.get("duplicate"))

        # Duplicates count as delivered: it means an earlier attempt got through
        # and only its answer was lost, which is the commonest way this goes
        # wrong on a bad connection.
        await self._queue.async_acknowledge([*accepted, *duplicate])

        # Answered for and neither taken nor known: the server read it and
        # passed over it. That is a strike - otherwise it is sent forever.
        answered = set(accepted) | set(duplicate)
        ignored = [str(event.get("event_uid")) for event in events if event.get("event_uid") not in answered]

        if ignored:
            await self._queue.async_strike(ignored)

    def _retime(self, occurred_at: Any, verified: bool = False) -> str | None:
        moment = parse(occurred_at)

        if verified and moment is not None and not self._clock.in_the_future(moment):
            # Checked against the panel when it was stamped (D44): it keeps its
            # time however late it is sent. A house offline for two days sends
            # events two days old, and the panel takes up to fourteen.
            return None

        if moment is not None and self._clock.plausible(moment):
            if not self._clock.in_the_future(moment):
                return None

            # A clock a few minutes to a few hours fast is "plausible" for
            # the repair issue, but an event cannot have happened after now:
            # the panel would answer it as a duplicate and store nothing.
            reference = self._clock.server_now()

            return iso(reference) if reference is not None else None

        return iso(self._clock.now())

    # -------------------------------------------------------------------- clock

    def _observe_clock(self, server_time: str | None) -> None:
        """Every answer carries the panel's time. Use it to judge ours."""
        self._clock.observe(server_time)

        skewed = self._clock.skewed

        if skewed == self._clock_issue:
            return

        self._clock_issue = skewed

        if skewed:
            _LOGGER.warning("The system clock is wrong; using the HAIM panel's time for events")
            self._raise_issue(ISSUE_CLOCK_SKEWED, ir.IssueSeverity.WARNING)
        else:
            try:
                ir.async_delete_issue(self._hass, DOMAIN, ISSUE_CLOCK_SKEWED)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Could not clear the clock issue", exc_info=True)

    # ------------------------------------------------------------------- config

    def _absorb_config(self, data: dict[str, Any] | None, config_version: str | None) -> None:
        """Take the intervals the cloud sends, within reason.

        This is how a whole fleet is reconfigured without shipping a release to
        three hundred houses and waiting for HACS to catch up. The bounds are
        ours, not theirs: a server bug that sent zero would turn every instance
        into a denial of service against us.
        """
        self._config_version = config_version or self._config_version

        config = (data or {}).get("config")

        if not isinstance(config, dict):
            return

        interval = config.get("state_interval")

        if isinstance(interval, int) and 60 <= interval <= 3600 and interval != self._state_interval:
            _LOGGER.debug("State interval changed to %ss", interval)
            self._state_interval = interval
            self._schedule()

        batch = config.get("event_batch_max")

        if isinstance(batch, int) and 1 <= batch <= 200:
            self._event_batch_max = batch

        log_level = config.get("log_level")

        if isinstance(log_level, str):
            self._collector.with_log_level(self.capped_log_level(log_level))

    def _absorb_rotated_key(self, rotated: str | None) -> None:
        """Keep the new key on disk first, and only then use it.

        The panel repeats `X-HAIM-Token-Rotate` on every answer until the new
        key is used, and stops the moment it is. A key adopted only in memory
        was therefore lost at the next restart of Home Assistant: back on the
        old key, no header any more, and seven days later locked out for good.

        So: the first time a key is seen it goes into the config entry and
        nothing else happens. It is used on a later answer, once the entry has
        had time to reach the disk. If Home Assistant restarts in between, it
        starts with the new key straight from the entry - which the panel
        accepts, since it minted it.
        """
        if not isinstance(rotated, str) or rotated == getattr(self._api, "agent_key", None):
            return

        if not re.fullmatch(AGENT_KEY_PATTERN, rotated):
            _LOGGER.warning("The HAIM panel offered a replacement key in a shape it never issues; ignored")

            return

        if self._persist_key is None:
            return

        if rotated != self._pending_key:
            try:
                self._persist_key(rotated)
            except Exception:  # noqa: BLE001
                # Not kept, so not used. The header comes back on the next
                # answer and this is tried again.
                _LOGGER.warning("Could not store the replacement HAIM key; still using the current one")

                return

            _LOGGER.debug("Stored a rotated agent key; it is used from a later answer")
            self._pending_key = rotated
            self._pending_since = time.monotonic()

            return

        if time.monotonic() - self._pending_since < ROTATED_KEY_ADOPT_AFTER_SECONDS:
            return

        _LOGGER.debug("Adopting the rotated agent key")
        self._api.with_key(rotated)
        self._pending_key = None

    # ------------------------------------------------------------------- issues

    def _raise_issue(self, issue_id: str, severity: Any = None) -> None:
        """Tell the household, in their own Home Assistant.

        A component that has been cut off and says so only in a debug log is a
        component nobody knows has stopped working.
        """
        try:
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=severity or ir.IssueSeverity.ERROR,
                translation_key=issue_id,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not raise the repair issue %s", issue_id, exc_info=True)

    @staticmethod
    def _new_uid() -> str:
        import uuid

        return str(uuid.uuid4())


def _uids(value: Any) -> list[str]:
    """The contract says an array of strings. Anything else is no answer."""
    return [str(uid) for uid in value if isinstance(uid, str)] if isinstance(value, list) else []
