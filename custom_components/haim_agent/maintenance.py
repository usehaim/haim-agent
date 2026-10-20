"""The account the installer works as, and the token that lasts one window.

Everything in here is deliberately narrow. It creates ONE account, parks it,
revives it, mints ONE token at a time and revokes it. It never touches the
household's own accounts, never changes anybody's password but its own, and
never leaves the account awake.

WHY A TOKEN IS MINTED EVERY TIME, and never kept (decision D27): Home
Assistant's `async_deactivate_user()` removes every refresh token the user had,
long-lived ones included, and reviving the account does not bring them back.
Verified on the laboratory and in Home Assistant's own source. So a token kept
between sessions would already be dead when it was needed - and the version that
works is also the stronger one, because outside an authorised window there is no
key to this house anywhere.

Every call sits in its own try/except (rule 9). Home Assistant's auth internals
are not a public API, and a release that moves one of them must cost us a
session that will not open, never an instance that stops reporting.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import MAINTENANCE_NAME, MAINTENANCE_USERNAME, STORAGE_KEY_MAINTENANCE, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

GROUP_ADMIN = "system-admin"


class MaintenanceUserError(Exception):
    """The account could not be prepared. The session must not open."""


class MaintenanceUser:
    """One account, asleep unless somebody was let in."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY_MAINTENANCE)

    async def async_find(self):
        """The maintenance account, or None. Found by its id, never by its name.

        The name is something the household can change in Settings. Looked up
        by name, a renamed account was never parked again - and the next window
        created a second one next to it. The id Home Assistant gave it cannot be
        changed by anybody, so it is the id that is written down (D37).

        The name is read exactly once in the life of an installation: when no
        id has been written yet, which is an installation created before the id
        was. The account found that way is adopted and its id written down, and
        from then on the name means nothing.
        """
        user_id = await self._async_stored_id()

        if user_id is not None:
            try:
                return await self._hass.auth.async_get_user(user_id)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Could not look up the maintenance account", exc_info=True)

                return None

        try:
            users = await self._hass.auth.async_get_users()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not list the accounts", exc_info=True)

            return None

        for user in users:
            if user.name in (MAINTENANCE_NAME, MAINTENANCE_USERNAME):
                await self._async_remember(user)

                return user

        return None

    async def _async_stored_id(self) -> str | None:
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not read the maintenance account id", exc_info=True)

            return None

        user_id = stored.get("user_id") if isinstance(stored, dict) else None

        return user_id if isinstance(user_id, str) and user_id else None

    async def _async_remember(self, user) -> None:
        try:
            await self._store.async_save({"user_id": user.id})
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not write down the maintenance account id", exc_info=True)

    async def async_ensure(self):
        """The account exists and is parked. Called at setup, and idempotent."""
        user = await self.async_find()

        if user is not None:
            return user

        try:
            user = await self._hass.auth.async_create_user(
                name=MAINTENANCE_NAME,
                group_ids=[GROUP_ADMIN],
                local_only=False,
            )
        except Exception as error:  # noqa: BLE001
            raise MaintenanceUserError(f"Could not create the maintenance account: {error}") from error

        # Written down before anything else happens to it: an account this
        # component created and then could not find again is exactly the
        # account that would stay awake.
        await self._async_remember(user)

        # Created and immediately parked. There is no moment in its life where
        # it is awake and nobody has been asked.
        await self.async_park(user)

        return user

    async def async_wake(self, minutes: int) -> str:
        """Revive the account and mint a token that dies with the window.

        Returns the long-lived access token. It is handed straight to the cloud
        and never written down here.
        """
        user = await self.async_ensure()

        try:
            await self._hass.auth.async_activate_user(user)
        except Exception as error:  # noqa: BLE001
            raise MaintenanceUserError(f"Could not revive the maintenance account: {error}") from error

        return await self._async_mint(user, minutes)

    async def async_mint(self, minutes: int) -> str:
        """Another key for an account that is ALREADY awake, for the time left (D51).

        A way back in during a window: after a logout in Home Assistant, which
        revoked the key that browser had, or after a restart, which lost the one
        in memory. Never wakes the account - an asleep one means the window is
        closed, and this refuses. The keys already handed out stay valid: the
        installer's other tab is not logged out by coming back in.
        """
        user = await self.async_find()

        if user is None or not user.is_active:
            raise MaintenanceUserError("The maintenance account is asleep: no window is open.")

        return await self._async_mint(user, minutes)

    async def _async_mint(self, user: Any, minutes: int) -> str:
        try:
            refresh_token = await self._hass.auth.async_create_refresh_token(
                user,
                client_name=f"HAIM {secrets.token_hex(4)}",
                client_icon=None,
                token_type="long_lived_access_token",
                # Exactly the window: Home Assistant keeps the timedelta as
                # given, minutes included (checked against 2026.8-2026.10 by
                # verifica 04). The window is enforced by three other levels
                # anyway (§11.3).
                access_token_expiration=timedelta(minutes=max(minutes, 1)),
            )

            return self._hass.auth.async_create_access_token(refresh_token)
        except Exception as error:  # noqa: BLE001
            # Awake with no token is the one state worth cleaning up
            # immediately: an admin account nobody asked for.
            await self.async_park(user)

            raise MaintenanceUserError(f"Could not mint a token: {error}") from error

    async def async_park(self, user: Any = None) -> None:
        """Asleep, and with no key left. Safe to call at any time, any number of times.

        In Home Assistant deactivating also destroys every refresh token the
        account had, and with them the websocket of whoever was inside. That is
        the fact D27 rests on - and precisely because everything rests on it,
        it is not taken on trust: every token still attached afterwards is
        revoked one by one. Today that loop finds nothing to do. The day a
        release changes what deactivating means, it is the difference between
        a closed window and an admin key left in somebody's house.
        """
        user = user or await self.async_find()

        if user is None:
            return

        try:
            await self._hass.auth.async_deactivate_user(user)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not park the maintenance account", exc_info=True)

        try:
            remaining = list(getattr(user, "refresh_tokens", {}).values())
        except Exception:  # noqa: BLE001
            remaining = []

        for refresh_token in remaining:
            try:
                self._hass.auth.async_remove_refresh_token(refresh_token)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Could not revoke a key of the maintenance account", exc_info=True)

    async def async_remove(self) -> None:
        """The integration is being removed: the account goes with it.

        Parked first, so that if removing it fails half way there is still no
        awake account and no key. Then deleted, because an integration that is
        no longer installed has no business leaving an administrator account in
        somebody's house, even a sleeping one.
        """
        user = await self.async_find()

        if user is None:
            return

        await self.async_park(user)

        try:
            await self._hass.auth.async_remove_user(user)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not remove the maintenance account", exc_info=True)

            return

        try:
            await self._store.async_remove()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not forget the maintenance account id", exc_info=True)

    async def async_is_awake(self) -> bool:
        user = await self.async_find()

        return bool(user is not None and user.is_active)
