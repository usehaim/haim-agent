"""The one thing this component says to the people who live here.

It can be dismissed, and it comes back at most once a week (D32). Not a
permanent banner: something that never goes away stops being read within a week,
and this is the text that decides whether a household trusts what is installed in
their house. It used to come back at every restart, which is the same mistake in
slower motion - a household that restarts Home Assistant twice in an evening saw
it twice, and learned to click it away without reading it.

The text was written, read and approved (D15). Two things it says now bind us:

1. "Nobody can see your system remotely unless you approve it, each time, and
   only for as long as you allow." True today because remote access does not
   exist yet. It stays true in the next phase ONLY if consent is a condition of
   every request rather than a pass issued once - which is exactly what §7 says
   of POST /internal/relay/verify. If anyone ever proposes checking consent only
   when the session opens, this sentence is the reason not to.
2. The repository address is github.com/usehaim/haim-agent, the same one
   written in manifest.json and the README.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    NOTICE_EVERY_DAYS,
    STORAGE_KEY_NOTICE,
    STORAGE_VERSION,
)
from .consent import ISSUE_PREFIX, ConsentRefused

_LOGGER = logging.getLogger(__name__)

ISSUE_WHAT_WE_SEND = "what_we_send"


async def async_announce_what_we_send(
    hass: HomeAssistant, log_level: str, site_uuid: str = ""
) -> None:
    """Raise the notice, at most weekly, in the language Home Assistant is set to.

    The date of the last one is on disk, so a restart is not a reason to say it
    again. Nothing else is kept: when it was said, and that is all.
    """
    store: Store[dict[str, str]] = Store(
        hass, STORAGE_VERSION, f"{STORAGE_KEY_NOTICE}.{site_uuid or 'default'}"
    )

    try:
        said = await store.async_load() or {}
        last = dt_util.parse_datetime(said.get("what_we_send_at") or "")

        if last is not None and dt_util.utcnow() - last < timedelta(days=NOTICE_EVERY_DAYS):
            return

        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_WHAT_WE_SEND,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_WHAT_WE_SEND,
            translation_placeholders={"log_level": log_level},
        )

        await store.async_save({"what_we_send_at": dt_util.utcnow().isoformat()})
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not raise the transparency notice", exc_info=True)


class AccessRequestFlow(RepairsFlow):
    """Answering a request to come in, from inside Home Assistant.

    The channel that survives a phone with notifications turned off, and the
    only one of the three that can ask "for how long" - two buttons on a lock
    screen cannot, so this is where a household cuts six hours down to one.

    Dismissing the issue is NOT an answer. Home Assistant lets any repair issue
    be ignored, and an ignored one here simply leaves the request to expire,
    which grants nothing. Silence must never be a yes.
    """

    def __init__(self, hass: HomeAssistant, request_uuid: str) -> None:
        self._hass = hass
        self._request_uuid = request_uuid

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        return await self.async_step_decide()

    async def async_step_decide(self, user_input: dict | None = None) -> FlowResult:
        consent = _consent(self._hass)
        pending = (consent.pending if consent is not None else None) or {}

        # What is shown and what is answered are the same request, always. A
        # card whose request is no longer the one waiting says so, and takes
        # nothing: showing one request and answering another is how somebody
        # says yes to something they never read.
        if consent is None or str(pending.get("request_uuid") or "") != self._request_uuid:
            if consent is not None:
                self._take_down()

            return self.async_abort(reason="no_longer_valid")

        if user_input is not None:
            decision = user_input.get("decision")

            if decision not in ("approve", "reject"):
                return self.async_abort(reason="no_longer_valid")

            try:
                await consent.async_answer(
                    approved=decision == "approve",
                    via="repairs",
                    granted_minutes=int(user_input.get("granted_minutes") or 0) or None,
                    request_uuid=self._request_uuid,
                )
            except ConsentRefused:
                # A session is open, or the request changed under the form.
                return self.async_abort(reason="cannot_answer_now")

            return self.async_create_entry(data={})

        requested = int(pending.get("requested_minutes") or 60)

        # Only durations no longer than what was asked for. Offering more would
        # offer something the cloud then silently reduces, which reads as a bug
        # and feels like a trick.
        allowed = [minutes for minutes in (60, 120, 240, 360) if minutes <= requested] or [requested]

        # Two lists and not two dropdowns. This is read on a phone, in a
        # hallway, by somebody deciding whether to let a stranger into their
        # house: every option has to be visible at once, without a tap and
        # without a scroll. The translation keys are what put the options in
        # the household's own language - a bare vol.In() shows the raw values,
        # which is how "approve" and "reject" ended up on an Italian screen.
        return self.async_show_form(
            step_id="decide",
            data_schema=vol.Schema(
                {
                    # No default. Whoever opens this has to choose: a form that
                    # arrives with "yes" already ticked is a yes by inertia.
                    vol.Required("decision"): SelectSelector(
                        SelectSelectorConfig(
                            options=["approve", "reject"],
                            translation_key="decision",
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Optional("granted_minutes", default=str(requested)): SelectSelector(
                        SelectSelectorConfig(
                            options=[str(minutes) for minutes in allowed],
                            translation_key="granted_minutes",
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            description_placeholders={
                "installer": str(pending.get("installer") or "?"),
                "subject": str(pending.get("subject") or ""),
                "reason": str(pending.get("reason") or ""),
                "minutes": str(requested),
            },
        )

    def _take_down(self) -> None:
        """A card for a request nobody is asking any more has nothing behind it."""
        try:
            ir.async_delete_issue(self._hass, DOMAIN, ISSUE_PREFIX + self._request_uuid)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not take down a stale request card", exc_info=True)


def _consent(hass: HomeAssistant):
    """The one consent manager on this instance, whichever entry set it up."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        coordinator = getattr(entry, "runtime_data", None)
        consent = getattr(coordinator, "consent", None)

        if consent is not None:
            return consent

    return None


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data) -> RepairsFlow:
    """One issue can be acted on; the rest are acknowledge-only.

    The notice is a notice - there is nothing for Home Assistant to fix on the
    household's behalf. A consent request is different: it is a question, and
    this is one of the three places it can be answered.
    """
    if issue_id.startswith(ISSUE_PREFIX):
        return AccessRequestFlow(hass, issue_id[len(ISSUE_PREFIX):])

    return ConfirmRepairFlow()
