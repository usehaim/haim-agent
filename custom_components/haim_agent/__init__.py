"""HAIM agent - instance manager for Home Assistant.

This component tells the installer who maintains this instance how it is doing.
It reports counts, versions, system metrics, integration domains and add-on
slugs. It does not report entity ids, device names, room or area names, states,
history, energy figures, images, or anything about the people who live here.

That boundary is the product. It is what lets HAIM say "we do not see what
happens in your house, we see whether your system is healthy" and have it be
literally true - and this repository is public so that the claim can be checked
rather than believed.
"""

from __future__ import annotations

import logging
import secrets

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HaimApi
from .const import (
    CONF_AGENT_KEY,
    CONF_BASE_URL,
    CONF_LOG_LEVEL,
    CONF_PSEUDONYM_KEY,
    CONF_SITE_UUID,
    DEFAULT_LOG_LEVEL,
    DOMAIN,
)
from .command_channel import CommandDispatcher, HttpPollChannel
from .commands import register_supported_commands
from .consent import ConsentManager
from .coordinator import HaimCoordinator
from .remote_access import RemoteAccessManager, async_remove_installation
from .web import HaimOpenView
from .repairs import async_announce_what_we_send

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

type HaimConfigEntry = ConfigEntry[HaimCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: HaimConfigEntry) -> bool:
    """Set up HAIM from a config entry."""
    api = HaimApi(
        session=async_get_clientsession(hass),
        base_url=entry.data[CONF_BASE_URL],
        agent_key=entry.data.get(CONF_AGENT_KEY),
    )

    # Made once per installation and kept in the entry, so that a pseudonym
    # means the same thing after a restart (D40). Never sent anywhere.
    if not entry.data.get(CONF_PSEUDONYM_KEY):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_PSEUDONYM_KEY: secrets.token_hex(32)}
        )

    coordinator = HaimCoordinator(
        hass=hass,
        api=api,
        site_uuid=entry.data.get(CONF_SITE_UUID, ""),
        log_level=entry.data.get(CONF_LOG_LEVEL, DEFAULT_LOG_LEVEL),
        pseudonym_key=entry.data.get(CONF_PSEUDONYM_KEY),
        persist_key=key_persister(hass, entry),
    )

    # The household's side of consent. Started here rather than lazily on the
    # first request: a notification tapped before this exists would go nowhere.
    consent = ConsentManager(hass, api, entry.data.get(CONF_SITE_UUID, ""))
    consent.start()

    # The channel is an interface with one implementation on purpose: past
    # roughly two thousand instances the sensible transport is MQTT, and adding
    # it should touch command_channel.py and nothing else.
    # The household's side of a remote session, including the deadline written
    # to DISK: a restart must never be a way to lengthen a window somebody
    # agreed to, and this level keeps working even if our cloud is dead.
    remote = RemoteAccessManager(
        hass,
        api,
        site_uuid=entry.data.get(CONF_SITE_UUID, ""),
        base_url=entry.data[CONF_BASE_URL],
    )

    # While a window is open the installer is an administrator of this house:
    # nothing in it may answer a request in that time (D36).
    consent.attach_session(lambda: remote.is_open)

    dispatcher = CommandDispatcher(HttpPollChannel(api))
    register_supported_commands(dispatcher, hass, coordinator, consent, remote)
    coordinator.attach_commands(dispatcher)
    coordinator.attach_consent(consent)

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Started after the platforms so the entities exist before the first
    # heartbeat writes to them - otherwise the first thing a household sees is
    # a sensor that says "unknown" for five minutes.
    # At most once a week, and dismissible (D32). A banner that never goes away
    # stops being read inside a week, and this is the text that decides whether
    # a household trusts what has been installed in their house.
    await async_announce_what_we_send(
        hass,
        entry.data.get(CONF_LOG_LEVEL, DEFAULT_LOG_LEVEL),
        entry.data.get(CONF_SITE_UUID, ""),
    )

    # The door the installer's browser knocks on, served on Home Assistant's own
    # origin because that is the only place that can write into its storage
    # (D29). Registered once and harmless while no window is open: with no
    # session it answers 404 to everything.
    hass.http.register_view(HaimOpenView(remote))

    # Before anything else: a deadline already passed means Home Assistant was
    # restarted during a session and came back after it should have ended.
    await remote.async_restore()

    # And the question the household may have been in the middle of answering.
    # A restart used to lose it: the card stayed on their screen with nothing
    # behind it, and the installer waited for an answer nobody could give (D33).
    await consent.async_restore()

    # Disabling, reloading or removing the integration closes the window: the
    # account is parked and every key it had is revoked. It used to only stop
    # the timer, which left an awake account that nothing counted down (D37).
    entry.async_on_unload(remote.async_stop)
    entry.async_on_unload(consent.stop)
    entry.async_on_unload(coordinator.async_stop)
    entry.async_create_background_task(hass, coordinator.async_start(), f"{DOMAIN}_start")

    return True


def key_persister(hass: HomeAssistant, entry: ConfigEntry):
    """Writes a rotated agent key into the config entry.

    Home Assistant saves config entries with an atomic write (temporary file,
    then rename), so the file on disk holds either the old key or the new one,
    never half of each. The coordinator uses the new key only after this has
    had time to land.
    """

    def persist(agent_key: str) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_AGENT_KEY: agent_key})

    return persist


async def async_unload_entry(hass: HomeAssistant, entry: HaimConfigEntry) -> bool:
    """Unload a config entry."""
    entry.runtime_data.async_stop()

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: HaimConfigEntry) -> None:
    """The integration was deleted: park the account, and delete it if this was the last entry."""
    others = [other for other in hass.config_entries.async_entries(DOMAIN) if other.entry_id != entry.entry_id]

    await async_remove_installation(hass, entry.data.get(CONF_SITE_UUID, ""), last=not others)
