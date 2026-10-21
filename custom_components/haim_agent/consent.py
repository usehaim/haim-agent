"""Asking the household, and taking their answer.

This is the file the whole product rests on. The notice this component shows in
the house says: "nobody can get into your system remotely unless you authorise
it, every time, and only for as long as you decide" - and the only reason that
sentence is true is that the question is asked HERE, on their own screens, and
the answer travels back from here.

THREE WAYS OF ASKING, and all three are needed:

1. The companion app, as an actionable notification. It reaches somebody who is
   not at home, which is exactly when an installer needs an answer.
2. A repair issue in Home Assistant, for whoever administers the house. It is
   the one that survives a phone with notifications turned off.
3. A persistent notification, which is what anyone who opens Home Assistant at
   all will see.

The cloud also emails them, and that one is deliberately LAST in the order of
things we rely on: teaching people to grant access to their home by clicking a
link in an email is teaching them to fall for the attack.

WHAT THIS FILE MAY NOT DO, and the reason:

It may not answer on anybody's behalf. There is no timeout that approves, no
default, no "the installer is trusted so let them in". A request nobody answers
expires, and expiring is a refusal to grant - never a grant by silence.

WHAT IS ON DISK, and why (D33). The pending request, and the moment it arrived.
Nothing else - no answer, no token, nothing that could grant anything. It is
there because a Home Assistant restart used to lose the question: the card
stayed on the family's screen, the buttons had nothing behind them, and both
the household and the installer were left waiting for each other.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONSENT_MAX_AGE_MINUTES,
    STORAGE_KEY_CONSENT,
    STORAGE_VERSION,
)
from .maintenance import MaintenanceUser

_LOGGER = logging.getLogger(__name__)

ISSUE_PREFIX = "access_request_"

NOTIFICATION_PREFIX = "haim_access_request_"

ACTION_APPROVE = "HAIM_CONSENT_APPROVE"

ACTION_REJECT = "HAIM_CONSENT_REJECT"

# The event the companion app fires when somebody taps one of the buttons.
COMPANION_EVENT = "mobile_app_notification_action"

# Home Assistant's own event for a line in the Logbook. Fired on the bus rather
# than through the logbook component so this works whether or not it is loaded.
LOGBOOK_EVENT = "logbook_entry"


class ConsentRefused(Exception):
    """This answer may not be given from here, now. Nothing was sent."""


class ConsentManager:
    """Holds the one request that may be pending, and the ways of answering it.

    WHO MAY ANSWER (D36). An answer reaches the cloud only if all of this holds:

    - it answers the request that is waiting, and no other: a notification left
      on a phone from an earlier request cannot answer the current one;
    - it comes from a person: an event with no user behind it, fired by an
      automation (a context with a parent), by a system user or by the HAIM
      maintenance account is not somebody in this house saying yes;
    - no session is open. While a window is open the installer is an
      administrator of this house, and an administrator can fire any event and
      open any repair: nothing done in that time may answer the next request.

    Who answered never leaves the house (rule 4). It goes in the Logbook, which
    is the household's own record, and nowhere else.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api,
        site_uuid: str = "",
        maintenance: Any = None,
    ) -> None:
        self._hass = hass
        self._api = api
        self._pending: dict[str, Any] | None = None
        self._unsubscribe = None
        self._on_change = None
        self._on_approved = None
        # Asked, never looked up by name here: how the maintenance account is
        # recognised is maintenance.py's business, and it may change.
        self._maintenance_given = maintenance
        self._session_open: Callable[[], bool] | None = None
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_CONSENT}.{site_uuid or 'default'}"
        )

    # ------------------------------------------------------------------ state

    @property
    def pending(self) -> dict[str, Any] | None:
        return self._pending

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    def on_change(self, callback) -> None:
        """Told when the pending request appears or goes away."""
        self._on_change = callback

    def on_approved(self, callback) -> None:
        """Told when a yes given in this house has reached the panel."""
        self._on_approved = callback

    def _changed(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Could not announce the consent change", exc_info=True)

    @property
    def _maintenance(self) -> Any:
        # Built on first use, not here: MaintenanceUser keeps its own Store,
        # and this manager is created before anything has asked for one.
        if self._maintenance_given is None:
            self._maintenance_given = MaintenanceUser(self._hass)

        return self._maintenance_given

    def attach_session(self, is_open: Callable[[], bool]) -> None:
        """How to tell whether a remote session is open right now."""
        self._session_open = is_open

    def start(self) -> None:
        """Listen for taps on the companion app's buttons."""
        if self._unsubscribe is None:
            self._unsubscribe = self._hass.bus.async_listen(COMPANION_EVENT, self._async_companion_action)

    def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    # ------------------------------------------------------------ surviving

    async def async_restore(self) -> None:
        """Put back a question that was still waiting when this house restarted.

        The companion notification is NOT sent again: it is still on the phone,
        untouched by a restart, and a second copy of "somebody wants to come
        into your house" is the kind of thing that gets a component uninstalled.
        What it needed was the request back in memory, which is what makes its
        buttons work again. The other two channels do not survive a restart, so
        those are raised.
        """
        try:
            remembered = await self._store.async_load() or {}
            data = remembered.get("request")
            asked_at = dt_util.parse_datetime(remembered.get("asked_at") or "")

            if not isinstance(data, dict) or not data.get("request_uuid"):
                self._forget_other_questions()

                return

            # The panel decides whether a request is still answerable, and it
            # gives them sixty minutes. This is only the backstop that stops a
            # question sitting on somebody's screen forever if we lose touch.
            too_old = asked_at is None or dt_util.utcnow() - asked_at > timedelta(
                minutes=CONSENT_MAX_AGE_MINUTES
            )

            if too_old:
                await self._forget()
                self._forget_other_questions()

                return

            self._pending = dict(data)
            self._forget_other_questions(keep=str(data["request_uuid"]))
            self._changed()

            for channel, coroutine in (
                ("repairs", self._async_ask_repairs(data)),
                ("notification", self._async_ask_notification(data)),
            ):
                try:
                    await coroutine
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Could not put %s back", channel, exc_info=True)

            _LOGGER.info(
                "HAIM: a request for access was still waiting for an answer, and is back on screen"
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not restore the pending request", exc_info=True)

    def _forget_other_questions(self, keep: str | None = None) -> None:
        """Cards left over from a question this house no longer has.

        Home Assistant keeps its issue registry across a restart, so a request
        card from a previous life stays on screen with nothing behind it - which
        is what a household saw before the question itself was kept (D33), and
        what an instance upgrading to it still has sitting there. Nothing else
        would ever remove them.
        """
        try:
            registry = ir.async_get(self._hass)

            stale = [
                issue.issue_id
                for issue in list(registry.issues.values())
                if issue.domain == "haim_agent"
                and issue.issue_id.startswith(ISSUE_PREFIX)
                and issue.issue_id != (ISSUE_PREFIX + keep if keep else None)
            ]

            for issue_id in stale:
                ir.async_delete_issue(self._hass, "haim_agent", issue_id)

            if stale:
                _LOGGER.info(
                    "HAIM: %d access request card(s) left over from before a restart, taken down",
                    len(stale),
                )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not tidy up the old request cards", exc_info=True)

    async def _remember(self, data: dict[str, Any]) -> None:
        """The question and when it arrived. Never an answer."""
        try:
            await self._store.async_save(
                {"request": dict(data), "asked_at": dt_util.utcnow().isoformat()}
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not write the pending request down", exc_info=True)

    async def _forget(self) -> None:
        try:
            await self._store.async_remove()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not forget the pending request", exc_info=True)

    # -------------------------------------------------------------- asking

    async def async_ask(self, data: dict[str, Any]) -> dict[str, Any]:
        """Put the question in front of the household, three ways."""
        request_uuid = data.get("request_uuid")

        if not isinstance(request_uuid, str) or not request_uuid:
            raise ValueError("The consent request carried no id.")

        self._pending = dict(data)
        await self._remember(data)
        self._changed()

        asked = []

        for channel, coroutine in (
            ("companion", self._async_ask_companion(data)),
            ("repairs", self._async_ask_repairs(data)),
            ("notification", self._async_ask_notification(data)),
        ):
            # Each channel in its own try/except, like every field the collector
            # reads (rule 9). One of them failing - no companion app installed,
            # a notify service renamed - must never be the reason a household
            # is not asked at all.
            try:
                await coroutine
                asked.append(channel)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Could not ask through %s", channel, exc_info=True)

        if not asked:
            raise RuntimeError("Could not put the request in front of anybody in this house.")

        return {"asked": asked}

    async def _async_ask_companion(self, data: dict[str, Any]) -> None:
        services = self._hass.services.async_services().get("notify", {})

        targets = [name for name in services if name.startswith("mobile_app_")]

        if not targets:
            raise RuntimeError("No companion app on this instance.")

        for target in targets:
            await self._hass.services.async_call(
                "notify",
                target,
                {
                    "title": self._title(data),
                    "message": self._body(data),
                    "data": {
                        "tag": NOTIFICATION_PREFIX + str(data["request_uuid"]),
                        # The iOS app does not send `tag` back with a tap; it
                        # sends `action_data`. Android sends `tag` (D40).
                        "action_data": {"tag": NOTIFICATION_PREFIX + str(data["request_uuid"])},
                        "actions": [
                            {"action": ACTION_APPROVE, "title": self._label("approve")},
                            {"action": ACTION_REJECT, "title": self._label("reject"), "destructive": True},
                        ],
                        # The answer decides who gets into their house: it is
                        # worth making them unlock the phone first.
                        "authenticationRequired": True,
                    },
                },
                blocking=True,
            )

    async def _async_ask_repairs(self, data: dict[str, Any]) -> None:
        ir.async_create_issue(
            self._hass,
            "haim_agent",
            ISSUE_PREFIX + str(data["request_uuid"]),
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="access_request",
            translation_placeholders={
                "installer": str(data.get("installer") or "?"),
                "subject": str(data.get("subject") or ""),
                "reason": str(data.get("reason") or ""),
                "minutes": str(data.get("requested_minutes") or ""),
            },
            data={"request_uuid": data["request_uuid"]},
        )

    async def _async_ask_notification(self, data: dict[str, Any]) -> None:
        await self._hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": self._title(data),
                "message": self._body(data),
                "notification_id": NOTIFICATION_PREFIX + str(data["request_uuid"]),
            },
            blocking=True,
        )

    # ------------------------------------------------------------- answering

    async def async_answer(
        self,
        approved: bool,
        via: str,
        granted_minutes: int | None = None,
        request_uuid: str | None = None,
        context: Context | None = None,
    ) -> None:
        """Send the answer back and take the question off every screen.

        Every channel in this house comes through here, so the rules that do
        not depend on the channel live here: the answer is for the request that
        is waiting, and nothing in this house answers while a session is open.
        """
        pending_uuid = (self._pending or {}).get("request_uuid")
        uuid = request_uuid or pending_uuid

        if not isinstance(uuid, str) or not uuid:
            raise ValueError("There is no request to answer.")

        if uuid != pending_uuid:
            raise ConsentRefused("That request is not the one waiting for an answer.")

        if await self._async_session_is_open():
            raise ConsentRefused("A session is open: nothing in this house may answer now.")

        # Who answered is NOT in here, and must never be (rule 4): the people
        # who live here go in no payload. The Logbook has it, and only there.
        payload: dict[str, Any] = {
            "decision": "approved" if approved else "rejected",
            "via": via,
        }

        if approved and granted_minutes:
            payload["granted_minutes"] = int(granted_minutes)

        await self._api.decide_access_request(uuid, payload)

        if approved and self._on_approved is not None:
            # The installer's «Apri» is seconds away. Never a reason to fail
            # the answer, which has already been given.
            try:
                self._on_approved()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Could not hurry the poll after a yes", exc_info=True)

        self._log_answer(approved, via, context)

        await self.async_clear(uuid)

    async def _async_session_is_open(self) -> bool:
        """Open, or impossible to tell - which here counts as open.

        Two independent signs, because either one alone can be wrong: the
        session the remote access manager holds, and whether the maintenance
        account is awake. An account awake with no session is an administrator
        nobody accounted for, and that is not the moment to take an answer.
        """
        try:
            if self._session_open is not None and self._session_open():
                return True
        except Exception:  # noqa: BLE001
            _LOGGER.warning("HAIM: could not tell whether a session is open, so no answer is taken")

            return True

        try:
            return bool(await self._maintenance.async_is_awake())
        except Exception:  # noqa: BLE001
            _LOGGER.warning("HAIM: could not check the maintenance account, so no answer is taken")

            return True

    def _log_answer(self, approved: bool, via: str, context: Context | None) -> None:
        """One line in the household's own Logbook.

        Who answered travels in the context, which is how the Logbook knows who
        did something. It is not written into the message and goes nowhere else.
        """
        try:
            text = LOGBOOK_TEXT.get(self._locale(), LOGBOOK_TEXT["en"])

            self._hass.bus.async_fire(
                LOGBOOK_EVENT,
                {
                    "name": text["name"].format(
                        installer=(self._pending or {}).get("installer") or "?"
                    ),
                    "message": text[("approved_" if approved else "rejected_") + via],
                    "domain": "haim_agent",
                },
                context=context,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not write the answer in the Logbook", exc_info=True)

    async def async_clear(self, request_uuid: str | None = None) -> None:
        """Take the question down, wherever it was put.

        Called after an answer given here AND when the cloud says it was
        answered somewhere else - otherwise a card saying "waiting for your
        answer" sits in somebody's house after they already answered by email.
        """
        uuid = request_uuid or (self._pending or {}).get("request_uuid")

        if isinstance(uuid, str) and uuid:
            try:
                ir.async_delete_issue(self._hass, "haim_agent", ISSUE_PREFIX + uuid)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Could not remove the repair issue", exc_info=True)

            try:
                await self._hass.services.async_call(
                    "persistent_notification",
                    "dismiss",
                    {"notification_id": NOTIFICATION_PREFIX + uuid},
                    blocking=True,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Could not dismiss the notification", exc_info=True)

            await self._async_clear_companion(uuid)

        await self._forget()

        if self._pending is not None and (uuid is None or self._pending.get("request_uuid") == uuid):
            self._pending = None
            self._changed()

    async def _async_clear_companion(self, request_uuid: str) -> None:
        """Take the notification off the phones too.

        Otherwise it stays there with live-looking buttons after the question is
        gone. Each phone on its own: one that is switched off must not keep the
        notification on the others.
        """
        try:
            services = self._hass.services.async_services().get("notify", {})
            targets = [name for name in services if name.startswith("mobile_app_")]
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not list the phones", exc_info=True)

            return

        for target in targets:
            try:
                await self._hass.services.async_call(
                    "notify",
                    target,
                    {
                        "message": "clear_notification",
                        "data": {"tag": NOTIFICATION_PREFIX + request_uuid},
                    },
                    blocking=True,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Could not clear the notification on %s", target, exc_info=True)

    async def _async_companion_action(self, event) -> None:
        data = getattr(event, "data", None) or {}
        action = data.get("action")

        if action not in (ACTION_APPROVE, ACTION_REJECT):
            return

        if self._pending is None:
            return

        request_uuid = str(self._pending.get("request_uuid"))

        # The tag says which request the notification was for. A notification
        # left on a phone from an earlier request does not answer this one.
        action_data = data.get("action_data") if isinstance(data.get("action_data"), dict) else {}
        tag = data.get("tag") or action_data.get("tag")

        if tag != NOTIFICATION_PREFIX + request_uuid:
            _LOGGER.warning("HAIM: an answer meant for a different request was ignored")

            return

        context = getattr(event, "context", None)

        if not await self._async_from_a_person(context):
            _LOGGER.warning("HAIM: an answer that did not come from a person in this house was ignored")

            return

        try:
            await self.async_answer(
                approved=action == ACTION_APPROVE,
                via="companion",
                # No duration from a notification button: two buttons cannot
                # ask "how long", and inventing a number here would be us
                # choosing on their behalf. Tapping yes agrees to what was
                # asked; the repair flow and the portal are where it can be cut
                # down.
                request_uuid=request_uuid,
                context=context,
            )
        except ConsentRefused as refusal:
            _LOGGER.warning("HAIM: an answer from the companion app was not taken: %s", refusal)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not report the answer from the companion app", exc_info=True)

    async def _async_from_a_person(self, context: Any) -> bool:
        """A tap by somebody who lives here: not a machine, and not us.

        The companion app fires its event with the context of the user the
        phone is registered to, and with no parent. An automation's events
        carry a parent and no user; a system user is Home Assistant talking to
        itself; the maintenance account is the installer. None of those is the
        household saying yes - and every check that cannot be made is a no.
        """
        try:
            if context is None or getattr(context, "parent_id", None) is not None:
                return False

            user_id = getattr(context, "user_id", None)

            if not user_id:
                return False

            user = await self._hass.auth.async_get_user(user_id)

            if user is None or user.system_generated or not user.is_active:
                return False

            maintenance = await self._maintenance.async_find()

            return not (maintenance is not None and maintenance.id == user.id)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not tell who answered, so the answer is not taken", exc_info=True)

            return False

    # ------------------------------------------------------------------ text

    def _title(self, data: dict[str, Any]) -> str:
        return self._text("title", data)

    def _body(self, data: dict[str, Any]) -> str:
        return self._text("body", data)

    def _label(self, key: str) -> str:
        return CONSENT_TEXT.get(self._locale(), CONSENT_TEXT["en"])[key]

    def _text(self, key: str, data: dict[str, Any]) -> str:
        template = CONSENT_TEXT.get(self._locale(), CONSENT_TEXT["en"])[key]

        return template.format(
            installer=data.get("installer") or "?",
            subject=data.get("subject") or "",
            reason=data.get("reason") or "",
            minutes=data.get("requested_minutes") or "",
        )

    def _locale(self) -> str:
        # Decision D35: whatever reaches the household speaks the language of
        # the installation, everywhere in the world. Not the installer's, not
        # the one the panel sends along: the one the family set in Home
        # Assistant. A language we have no approved text for falls back to
        # English, never to a guess.
        language = str(self._hass.config.language or "en")

        if language in CONSENT_TEXT:
            return language

        base = language.replace("_", "-").split("-")[0].lower()

        return base if base in CONSENT_TEXT else "en"


# Approved on 25/08/2026 (decision D28). These ask somebody for permission to
# enter their home, so they are not rewritten in passing.
#
# They live here and not in strings.json because a notification does not go
# through Home Assistant's translation system: the message is a string we hand
# to the notify service, so the component has to carry both languages itself.
#
# "Nothing happens until you answer" is the load-bearing sentence, and it is
# true because nothing in this file answers on anybody's behalf.
CONSENT_TEXT: dict[str, dict[str, str]] = {
    "it": {
        "title": "{installer} chiede di entrare nel tuo impianto",
        "body": "Cosa deve fare: {subject}\nPerché: {reason}\nPer quanto: {minutes} minuti\n\nNon succede niente finché non rispondi tu.",
        "approve": "Sì, può entrare",
        "reject": "No",
    },
    "en": {
        "title": "{installer} is asking to connect to your system",
        "body": "What they need to do: {subject}\nWhy: {reason}\nFor how long: {minutes} minutes\n\nNothing happens until you answer.",
        "approve": "Yes, let them in",
        "reject": "No",
    },
}


# The line a household reads in its own Logbook after answering. Not one of the
# texts that ask (D28): this is the record afterwards. The person who answered
# is shown by the Logbook itself, from the event's context - never written here.
LOGBOOK_TEXT: dict[str, dict[str, str]] = {
    "it": {
        "name": "Richiesta di accesso di {installer}",
        "approved_companion": "accolta dal telefono",
        "rejected_companion": "respinta dal telefono",
        "approved_repairs": "accolta dalle Riparazioni",
        "rejected_repairs": "respinta dalle Riparazioni",
    },
    "en": {
        "name": "Access request from {installer}",
        "approved_companion": "approved from the phone",
        "rejected_companion": "turned down from the phone",
        "approved_repairs": "approved from Repairs",
        "rejected_repairs": "turned down from Repairs",
    },
}
