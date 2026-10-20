"""The commands this version of the component knows how to carry out.

Deliberately few. Everything to do with tunnels and remote sessions belongs to a
later phase, and it has to be built WITH its safeguards - consent first - never
before them.

Updates are here because their safeguards are: the advisory gate and the
maintenance window are applied in the cloud before a batch is ever sent, and the
backup is applied here, by waiting for it.

Anything not here is answered with an explicit refusal rather than silence. An
installer being told "that instance is too old to do this" can act on it; an
installer watching a command sit at 'sent' until it expires cannot tell that
from a house with no network.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant

from .const import LOG_LEVELS
from .updates import async_install_batch

_LOGGER = logging.getLogger(__name__)

# A close_reason is the panel's word for why (expired, revoked_by_owner,
# user_removed, ...). It goes into the local log and back in the result, and is
# shown nowhere in the house: a word, not a sentence.
_REASON = re.compile(r"[a-z_]{1,40}")


def register_supported_commands(dispatcher, hass: HomeAssistant, coordinator, consent=None, remote=None) -> None:
    """Wire up what v1 can actually do."""

    async def self_test(_data: dict[str, Any]) -> dict[str, Any]:
        """Prove the whole path works, end to end, on demand.

        This is what an installer runs while on the phone with a customer, and
        the answer they want is "yes, it went out just now".
        """
        delivered = await coordinator.async_heartbeat()

        return {"heartbeat_delivered": delivered, "site_uuid": coordinator.site_uuid}

    async def config_push(data: dict[str, Any]) -> dict[str, Any]:
        """Change what the household chose to send, when they change it there.

        Only the log level: everything else in the config block travels on the
        heartbeat response, and this exists so a change made in the panel takes
        effect within thirty seconds rather than five minutes.
        """
        log_level = data.get("log_level")

        if log_level not in LOG_LEVELS:
            raise ValueError(f"'{log_level}' is not a log level this component understands.")

        coordinator.set_log_level(log_level)

        # What actually applies, which may be less than what was asked (D41).
        return {"log_level": coordinator.capped_log_level(log_level)}

    async def clear_logs(_data: dict[str, Any]) -> dict[str, Any]:
        """Empty Home Assistant's error log after a problem has been dealt with.

        It is what an installer does at the end of a call-out so that the next
        alert is about something new rather than about the thing they just
        fixed.
        """
        await hass.services.async_call("system_log", "clear", blocking=True)

        return {"cleared": True}

    async def update_batch(data: dict[str, Any]) -> dict[str, Any]:
        """Install what the cloud has already decided may be installed.

        Whether and when were settled before this arrived. What is left is the
        part only the house can do: check the entity still offers the update,
        take the backup and WAIT for it where the entity cannot take its own,
        and launch the install non-blocking because a Core update takes this
        process down with it.

        Each item reports on its own uuid, so a batch interrupted by a restart
        still leaves an accurate record of everything before the restart.
        """
        items = data.get("items")

        if not isinstance(items, list):
            raise ValueError("The update batch carried no items.")

        # Only an id this house sent in `updates`, translated back to the real
        # one when it travelled under a pseudonym (D40, D41). Anything else
        # becomes "", which installs nothing and reads nothing.
        items = [
            {**item, "entity_id": await coordinator.async_resolve_update_entity(item.get("entity_id")) or ""}
            if isinstance(item, dict) else item
            for item in items
        ]

        outcome = await async_install_batch(hass, dispatcher.async_report_item, items)

        # Push a heartbeat now rather than waiting for the next tick: for
        # anything that did not restart the instance, this is what lets the
        # panel show the new version within seconds instead of within half a
        # minute. It is best effort - if the instance is already going down for
        # a Core update, this simply never returns, and that is fine.
        try:
            await coordinator.async_heartbeat()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not push a heartbeat after the batch", exc_info=True)

        return outcome

    async def connection_request(data: dict[str, Any]) -> dict[str, Any]:
        """Somebody is asking to come in. Put it in front of the household.

        This handler does NOT answer. It cannot: there is no timeout that
        approves, no default and no "the installer is trusted". It asks, three
        ways, and the answer travels back on its own when a person gives one.
        """
        if consent is None:
            raise RuntimeError("Consent is not available on this instance.")

        return await consent.async_ask(data)

    async def answered_elsewhere(data: dict[str, Any]) -> dict[str, Any]:
        """It was answered somewhere else - the portal, or in advance.

        All this does is take the question down. A card in somebody's house
        still saying "waiting for your answer" after they answered by email is
        how a household stops believing what this component tells them.

        Both outcomes land here. They are two actions in the contract rather
        than one with a field because an agent that does not know an action
        refuses it explicitly and carries on - which is what made adding the
        refusal in 1.1.0 a compatible change.
        """
        if consent is not None:
            await consent.async_clear(data.get("request_uuid"))

        return {"cleared": True}

    async def gave_up(data: dict[str, Any]) -> dict[str, Any]:
        """Nobody answered in time, and the question comes down.

        Not the same thing as a refusal, and not written as one: nobody in this
        house said no, nobody was reached at all. Here the two do the same
        thing - take the card off the screen - but the panel keeps them apart,
        because "they said no" and "we could not reach them" lead an installer
        to do opposite things next.

        Since a pending request survives a restart (D33), this is now the only
        thing that ever takes an unanswered one down.
        """
        if consent is not None:
            await consent.async_clear(data.get("request_uuid"))

        return {"cleared": True}

    dispatcher.register("self_test", self_test)
    dispatcher.register("connection_request", connection_request)
    async def connection_approved(data: dict[str, Any]) -> dict[str, Any]:
        """Somebody was let in, and this is where the window actually opens.

        Two things happen and the order matters: the question comes off every
        screen in the house, and then the account wakes up. If the second fails
        the command fails and the cloud sees it - a window the house could not
        open is a window that must not be reported as open.
        """
        outcome = await answered_elsewhere(data)

        if remote is None or not data.get("session_uuid"):
            # No session in it: this was only "answered elsewhere" (block 7).
            return outcome

        return {**outcome, **await remote.async_open(data)}

    async def close_session(data: dict[str, Any]) -> dict[str, Any]:
        """Shut the window now.

        Parking the maintenance account is the whole of it: in Home Assistant
        that one call also destroys every token it had, so the browser tab of
        whoever was inside dies at the same instant. There is no ordering to get
        wrong and no half-closed state.

        Only the window it names (prompt 05, point 6). The panel no longer
        folds close commands together - a close for window A still waiting
        must not swallow the close for window B - so a close for A can reach
        the house after B has opened. Closing whatever is open would end B,
        which the household agreed to, because of A. So: the window it names,
        or nothing open at all (closing then only parks the account again,
        which is always safe), or a command with no `session_uuid` from a
        panel older than that rule. Anything else is left alone and said so.
        """
        if remote is None:
            return {"closed": False}

        reason = data.get("close_reason")
        reason = reason if isinstance(reason, str) and _REASON.fullmatch(reason) else "closed_by_user"

        named = data.get("session_uuid")
        current = (remote.session or {}).get("session_uuid")

        if isinstance(named, str) and named and current is not None and named != current:
            _LOGGER.info("HAIM: a close for a window that is not the open one was ignored")

            return {"closed": False, "ignored": "not_the_open_session", "session_uuid": named}

        return await remote.async_close(reason)

    # A "let them in" that reaches the house after its own deadline is refused,
    # not carried out: nobody is waiting on it any more (D37).
    dispatcher.register("connection_approved", connection_approved, refuse_when_late=True)
    dispatcher.register("close_session", close_session)
    dispatcher.register("connection_rejected", answered_elsewhere)
    dispatcher.register("connection_expired", gave_up)
    dispatcher.register("update_batch", update_batch)
    dispatcher.register("config_push", config_push)
    dispatcher.register("clear_logs", clear_logs)
