"""The window, from inside the house: opening it, and closing it on time.

This is the second of the three independent expiries (§11.3), and the one we do
better than the competing product: **the deadline is written to disk.** Theirs
lives in memory, so a Home Assistant restart during a session loses it. A
restart must never be a way to lengthen a window somebody agreed to - nor to
shorten it by accident.

It is also the level that does not depend on us. If our cloud dies mid-session -
the relay unreachable, the poll failing, the whole platform down - this timer
still fires and this house still closes itself. The household's protection does
not rest on our uptime.

WHAT CLOSING MEANS HERE: parking the maintenance account. In Home Assistant that
single call also destroys every token the account had, so there is no ordering
to get wrong and no half-closed state - the browser tab of whoever was inside
dies at the same instant.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import MAX_WINDOW_MINUTES, SESSION_SYNC_SECONDS, STORAGE_KEY_SESSION, STORAGE_VERSION
from .maintenance import MaintenanceUser, MaintenanceUserError
from .web import fingerprint, ticket_matches

_LOGGER = logging.getLogger(__name__)


class RemoteAccessManager:
    """One window at a time, and never one that outlives its deadline."""

    def __init__(self, hass: HomeAssistant, api, site_uuid: str = "", base_url: str = "") -> None:
        self._hass = hass
        self._api = api
        self._base_url = base_url
        self._user = MaintenanceUser(hass)
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_SESSION}.{site_uuid or 'default'}"
        )
        self._session: dict[str, Any] | None = None
        self._cancel_timer = None
        self._cancel_sync = None
        self._listeners: list[Any] = []

        # The key handed to the cloud lives in memory only, and only while the
        # window is open (D27). The sha256 of the ticket goes to disk with the
        # deadline (D51): it opens nothing by itself - the ticket does, and the
        # ticket never reaches this house except in the installer's POST - and
        # without it a restart meant asking the household all over again.
        self._token: str | None = None
        self._ticket_hash: str | None = None

    # ------------------------------------------------------------------ state

    @property
    def session(self) -> dict[str, Any] | None:
        return self._session

    @property
    def is_open(self) -> bool:
        return self._session is not None

    @property
    def expires_at(self) -> datetime | None:
        """When the open window closes; None when none is. For the entities."""
        return _parse(self._session.get("expires_at")) if self._session else None

    def async_add_listener(self, listener) -> Any:
        """Called whenever a window opens, closes or gets a new deadline."""
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _changed(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("A window listener failed", exc_info=True)

    # ------------------------------------------------------------------ opening

    async def async_open(self, data: dict[str, Any]) -> dict[str, Any]:
        """Somebody was let in. Wake the account, arm the timer, hand over the token.

        The ORDER is the safeguard (D37). The deadline goes to disk and the
        timer is armed BEFORE the token leaves the house, and anything that
        goes wrong after the account wakes parks it again. There is no point in
        this method where an account is awake and nothing is counting it down.
        """
        session_uuid = data.get("session_uuid")
        cloud_deadline = _parse(data.get("expires_at"))

        if not isinstance(session_uuid, str) or not session_uuid or cloud_deadline is None:
            raise ValueError("The session carries no id or no deadline.")

        # Measured from now, on THIS house's clock, and never longer than six
        # hours whatever the cloud says (D37). The cloud's deadline is only
        # allowed to make it shorter: a house whose clock runs behind would
        # otherwise read a cloud deadline as hours away, and a cloud that is
        # wrong - or compromised - could hand out a window of any length.
        arrived = _now()
        minutes = _window_minutes(data.get("granted_minutes"), cloud_deadline, arrived)

        if minutes is None or cloud_deadline <= arrived:
            # A window that arrived already over. Refusing is the only honest
            # answer: opening it would put an admin account into the house for a
            # window nobody would be watching.
            raise ValueError("That window has already closed.")

        expires_at = min(arrived + timedelta(minutes=minutes), cloud_deadline)

        # Checked against the address this instance was enrolled with, never
        # trusted as it arrives (§11.4, point 3): a compromised backend must not
        # be able to point this house's tunnel wherever it likes. The competing
        # product does not do this.
        # Empty is the same as absent: a panel with no relay configured sends
        # "" (it did, on 20/10/2026, and every window was refused). Anything
        # else must be ours.
        relay = data.get("relay_url") or None

        if relay is not None and not self._relay_is_ours(str(relay)):
            raise ValueError("The relay address does not belong to the server this instance is enrolled with.")

        if self.is_open:
            # A second window while one is open. Everything the first one had
            # is revoked BEFORE the second exists: otherwise the first token
            # stays alive, nobody is counting it down any more, and it outlives
            # the deadline the household agreed to (D37).
            #
            # Parked rather than refused: a window restored after a restart is
            # still open but has no key - and the only way back in is the new
            # window this is opening. Refusing would lock the installer out
            # until the old deadline, for a window nobody can use.
            await self.async_close("superseded")
        else:
            # Nothing open as far as this process knows - and still parked
            # first, so that whatever key a crash or an older version left
            # behind dies here. One window, one key, never two.
            await self._user.async_park()

        try:
            token = await self._user.async_wake(minutes)

            # sha256 of the one-time ticket, sent by the cloud over the channel
            # this instance is already authenticated on. We never hold the
            # ticket itself, the same way the cloud never holds the agent key.
            self._ticket_hash = data.get("ticket_hash") if isinstance(data.get("ticket_hash"), str) else None
            self._token = token

            self._session = {
                "session_uuid": session_uuid,
                "slug": data.get("slug"),
                "relay_url": relay,
                "expires_at": expires_at.isoformat(),
                "ticket_hash": self._ticket_hash,
            }

            # Disk, then the timer, then - and only then - the cloud. A crash
            # between any two of them leaves a deadline the next start finds;
            # a cloud that does not answer leaves a timer that still fires.
            await self._store.async_save(self._session)

            self._arm(expires_at)

            # The id of the HAIM maintenance account - an account this
            # component made for itself, not a person of the house - which the
            # contract requires so the panel can tell windows apart.
            account = await self._user.async_find()

            await self._api.deliver_credentials(
                {
                    "session_uuid": session_uuid,
                    "ha_user_id": str(getattr(account, "id", "") or ""),
                    "ha_username": "haim-maintenance",
                    "token": token,
                    "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
                }
            )
        except BaseException:
            # Whatever failed - the account, the disk, the cloud, or the task
            # being cancelled - the account goes back to sleep and every key it
            # had goes with it. A window the house could not finish opening is
            # a window that is closed.
            await self.async_close("failed_to_open")

            raise

        self._changed()

        _LOGGER.info(
            "HAIM window open until %s, ticket %s, and the key expires with it",
            expires_at.isoformat(),
            (self._ticket_hash or "-")[:8],
        )

        return {"opened": True, "expires_at": self._session["expires_at"]}

    # ------------------------------------------------------------------ closing

    async def async_close(self, reason: str = "closed_by_user") -> dict[str, Any]:
        """Park the account, which in Home Assistant is also the revocation."""
        self._disarm()

        await self._user.async_park()

        # Dropped before anything else that can fail: the key must not outlive
        # the window in memory either.
        self._token = None
        self._ticket_hash = None

        self._session = None

        try:
            await self._store.async_remove()
        except Exception:  # noqa: BLE001
            # The account is already parked: a deadline left on disk only
            # means the next start parks it once more.
            _LOGGER.debug("Could not remove the deadline from disk", exc_info=True)

        _LOGGER.info("HAIM window closed (%s): the account is parked and every key it had is gone", reason)

        self._changed()

        return {"closed": True, "reason": reason}

    # ------------------------------------------------------------------ the door

    async def async_redeem(self, ticket: str) -> dict[str, Any] | None:
        """Check the ticket and hand over a key - a fresh one, every time (D51).

        Returns None for every failure, and the caller answers 404 to all of
        them: no window, no ticket issued, wrong ticket, window over. Telling
        them apart is how somebody works out which of the four they are hitting.

        A fresh key and not the one minted at opening: that one dies with a
        logout in Home Assistant, and with a restart, which keeps it nowhere.
        Either used to lock the installer out of a window the household had
        granted. Every key is minted for the time left and revoked with the
        account when the window closes.
        """
        if self._session is None:
            _LOGGER.debug("HAIM door: no window is open in this house")

            return None

        if self._ticket_hash is None:
            _LOGGER.debug("HAIM door: the window was opened without a ticket hash")

            return None

        if not ticket_matches(ticket, self._ticket_hash):
            _LOGGER.debug(
                "HAIM door: the ticket does not match the one this window was opened with (theirs %s, ours %s)",
                fingerprint(ticket),
                self._ticket_hash[:8],
            )

            return None

        expires_at = _parse(self._session.get("expires_at"))

        if expires_at is None or expires_at <= _now():
            _LOGGER.debug("HAIM door: the window closed at %s", expires_at)

            return None

        session_uuid = self._session.get("session_uuid")
        left = max(1, int((expires_at - _now()).total_seconds() // 60) + 1)

        try:
            token = await self._user.async_mint(left)
        except MaintenanceUserError:
            _LOGGER.debug("HAIM door: the maintenance account is asleep", exc_info=True)

            return None

        if self._session is None or self._session.get("session_uuid") != session_uuid:
            # The window closed while the key was being minted. Parked again,
            # so the key that just came into being goes with it.
            await self._user.async_park()

            return None

        # Deliberately NOT spent. One-time was protecting against the leak of
        # something that never leaves the installer's browser - it travels in a
        # POST body, never in a URL - and that dies with the window anyway.
        #
        # What it cost instead was real: re-entering meant asking the household
        # for permission again, or waiting for a new hash to arrive on the next
        # poll. A tool that makes you ask twice to reopen a tab you closed by
        # accident is a tool people stop using.
        #
        # So the ticket lives exactly as long as the window does, and not a
        # second more: the check below is what ends it.
        return {
            "token": token,
            "expires_at_ms": int(expires_at.timestamp() * 1000),
        }

    # ------------------------------------------------------------------ restart

    async def async_restore(self) -> None:
        """Called at every start, and it is the whole point of the disk.

        Three outcomes, and the middle one is the one that matters: a deadline
        already passed means Home Assistant was restarted during a session and
        came back after it should have ended - so it closes NOW, before anything
        else in this component runs.
        """
        stored = await self._store.async_load()

        if not stored:
            # Nothing was open. Park anyway: at every start the account is
            # forced back to asleep, so a crash mid-session cannot leave an
            # admin account awake in somebody's house.
            await self._user.async_park()

            return

        expires_at = _parse(stored.get("expires_at"))

        if expires_at is None or expires_at <= _now():
            await self.async_close("expired_while_restarting")

            return

        # Restored: the deadline and the ticket's hash survived, the key handed
        # to the cloud did not. The installer comes back in through the door,
        # which mints a new key (D51); and the first thing done is to ask the
        # panel whether the window is still open at all.
        #
        # And never further away than six hours from now: a deadline on disk
        # that says otherwise was not written by this version, or the clock of
        # this house moved backwards while it was down (D37).
        expires_at = min(expires_at, _now() + timedelta(minutes=MAX_WINDOW_MINUTES))

        self._session = {**stored, "expires_at": expires_at.isoformat()}
        ticket_hash = stored.get("ticket_hash")
        self._ticket_hash = ticket_hash if isinstance(ticket_hash, str) else None
        self._arm(expires_at)
        self._changed()

        await self.async_sync()

    # ------------------------------------------------------------------ timer

    def _arm(self, expires_at: datetime) -> None:
        self._disarm()

        async def fire(_now_) -> None:
            await self.async_close("expired")

        async def sync(_now_) -> None:
            await self.async_sync()

        self._cancel_timer = async_track_point_in_utc_time(self._hass, fire, expires_at)
        self._cancel_sync = async_track_time_interval(self._hass, sync, timedelta(seconds=SESSION_SYNC_SECONDS))

    def _disarm(self) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

        if self._cancel_sync is not None:
            self._cancel_sync()
            self._cancel_sync = None

    # ------------------------------------------------------------------ the panel

    async def async_sync(self) -> None:
        """Ask the panel whether the window is still open (D51, contract 1.5.0).

        The panel is where that is decided. Closed there: closed here, now.
        Open: its deadline may make ours SHORTER, never longer. No answer, or
        one this house does not understand: the deadline on disk stands, and
        the timer closes the window at it all the same. Never fail-open.
        """
        session = self._session

        if session is None or self._api is None or not hasattr(self._api, "session_status"):
            return

        session_uuid = session.get("session_uuid")

        try:
            response = await self._api.session_status(str(session_uuid))
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not ask the panel about the window; the deadline here stands", exc_info=True)

            return

        if self._session is None or self._session.get("session_uuid") != session_uuid:
            return

        data = getattr(response, "data", None)

        if getattr(response, "status", None) != 200 or not isinstance(data, dict):
            return

        if data.get("status") == "closed":
            reason = data.get("close_reason")

            await self.async_close(reason if isinstance(reason, str) and reason.isidentifier() else "closed_by_panel")

            return

        if data.get("status") != "open":
            return

        theirs = _parse(data.get("expires_at"))
        ours = _parse(self._session.get("expires_at"))

        if theirs is None or ours is None or theirs >= ours:
            return

        if theirs <= _now():
            await self.async_close("expired")

            return

        self._session = {**self._session, "expires_at": theirs.isoformat()}

        try:
            await self._store.async_save(self._session)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not write the shorter deadline to disk", exc_info=True)

        self._arm(theirs)
        self._changed()

    def _relay_is_ours(self, relay: str) -> bool:
        """The host we were enrolled with, or a subdomain of it, over https.

        The enrolled host IS the configured suffix, compared exactly (D37). It
        used to be "the last two labels", which is not a domain: for a panel on
        `haim.co.uk` it accepted any `*.co.uk`, and for one on a Tailscale name
        it accepted any `*.ts.net` - anybody's machine. Telling `haim.co.uk`
        from `panel.haim.example` would take the public suffix list, and a
        security check that depends on a list we would have to keep current is
        a check that one day is not current.

        So the relay lives under the panel's own host - `{slug}.tun.<host>` -
        or it is not ours. An address is matched only as itself.
        """
        from urllib.parse import urlparse

        try:
            target = urlparse(relay)
            ours = urlparse(self._base_url)
            target_host = (target.hostname or "").lower().rstrip(".")
            our_host = (ours.hostname or "").lower().rstrip(".")
        except ValueError:
            return False

        if target.scheme != "https" or not target_host or not our_host:
            return False

        if target_host == our_host:
            return True

        if _is_address(our_host):
            return False

        return target_host.endswith("." + our_host)

    async def async_stop(self) -> None:
        """The integration is being unloaded: disabled, reloaded or removed.

        It used to only disarm the timer, which left the account awake with a
        live token and the installer's websocket open - two hours after the
        deadline, nothing was counting it down any more (D37). Now the window
        closes, the account is parked and every key it had is revoked. A reload
        mid-session ends the session; the installer asks again. That is the
        safe half to lose.
        """
        await self.async_close("integration_unloaded")


async def async_remove_installation(hass: HomeAssistant, site_uuid: str, *, last: bool) -> None:
    """The integration is being deleted. Nothing of ours stays behind awake.

    Home Assistant unloads an entry before removing it, so by now the window is
    normally closed already. This does it again regardless - it costs nothing
    and does not depend on the unload having gone well - and then, when this
    was the last HAIM entry in the house, deletes the maintenance account
    altogether: an integration that is no longer installed has no business
    leaving an administrator account in somebody's house, even a sleeping one.
    """
    try:
        await RemoteAccessManager(hass, None, site_uuid=site_uuid).async_close("integration_removed")
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not close the window while removing the integration", exc_info=True)

    if last:
        await MaintenanceUser(hass).async_remove()


def _window_minutes(granted: Any, cloud_deadline: datetime, arrived: datetime) -> int | None:
    """How long this house keeps the window open: min(granted, six hours).

    `granted_minutes` is what the household agreed to. If it is missing - a
    panel older than the field - the time left to the cloud's deadline stands
    in for it, still capped at six hours. None for a window with no time left.
    """
    if isinstance(granted, int) and not isinstance(granted, bool) and granted > 0:
        minutes = granted
    else:
        remaining = (cloud_deadline - arrived).total_seconds()

        if remaining <= 0:
            return None

        minutes = max(1, int(remaining // 60))

    return min(minutes, MAX_WINDOW_MINUTES)


def _is_address(host: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False

    return True


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
