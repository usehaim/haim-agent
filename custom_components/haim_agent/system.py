"""What kind of Home Assistant is this, and what can we do with it.

Capability negotiation lives here. The rule it exists to serve: the cloud
DEGRADES what it offers, it never refuses an instance. On a Container install
there is no Supervisor, so add-on management simply is not on the menu - that
is a smaller product for that customer, not a broken one.

Every lookup is in its own try block. An API of Home Assistant's that changes in
a release must turn one field null, never take the whole description down with
it. That is the number one reason components like this break.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from .const import (
    CAPABILITY_BACKUP_AGENTS,
    CAPABILITY_HACS,
    CAPABILITY_MOBILE_APP,
    CAPABILITY_SUPERVISOR,
    CAPABILITY_SYSTEM_LOG,
)
from .bounds import version
from .version import AGENT_VERSION

_LOGGER = logging.getLogger(__name__)

# Home Assistant reports the install type as a human phrase; the contract wants
# one of four slugs. Anything unrecognised is treated as `core`, which is the
# poorest profile: guessing generously would have us offer add-on management on
# an instance that has none.
_INSTALL_TYPES = {
    "Home Assistant OS": "os",
    "Home Assistant Container": "container",
    "Home Assistant Supervised": "supervised",
    "Home Assistant Core": "core",
}


async def async_describe_instance(hass: HomeAssistant) -> dict[str, Any]:
    """The identity block: versions, install type, capabilities."""
    info = await _async_system_info(hass)

    # Shortened to what the panel can store, and never cut through a number:
    # a development build reports `2026.11.0.dev202610140301`, twenty-five
    # characters against a varchar(20), and used to earn a 500 on every
    # heartbeat for as long as it stayed installed (bounds.py, verifica 03).
    # The same values go into the enrolment, so both paths are covered here.
    return {
        "ha_version": version(_string(info.get("version")) or HA_VERSION) or "unknown",
        "agent_version": version(AGENT_VERSION) or "unknown",
        "install_type": _install_type(info),
        "python_version": version(_string(info.get("python_version"))),
        "instance_uuid": _instance_uuid(hass),
        "capabilities": async_capabilities(hass),
    }


def async_capabilities(hass: HomeAssistant) -> list[str]:
    """What this instance can actually do, declared rather than assumed."""
    capabilities: list[str] = []

    for capability, present in (
        (CAPABILITY_SUPERVISOR, _has_supervisor(hass)),
        (CAPABILITY_HACS, _has_component(hass, "hacs")),
        (CAPABILITY_BACKUP_AGENTS, _has_component(hass, "backup")),
        (CAPABILITY_MOBILE_APP, _has_component(hass, "mobile_app")),
        (CAPABILITY_SYSTEM_LOG, _has_component(hass, "system_log")),
    ):
        if present:
            capabilities.append(capability)

    return capabilities


def _has_supervisor(hass: HomeAssistant) -> bool:
    try:
        return supervisor_present(hass)
    except Exception:  # noqa: BLE001 - a missing or changed helper is not a failure
        _LOGGER.debug("Could not determine whether a Supervisor is present", exc_info=True)

        return False


def supervisor_present(hass: HomeAssistant) -> bool:
    """Is there a Supervisor behind this instance. The one place that asks.

    `is_hassio` moved to `homeassistant.helpers.hassio` in 2024.11 and the old
    name in `homeassistant.components.hassio` was later dropped. Importing it
    from the old place raised ImportError on every supported release, the try
    block above turned that into False, and every HA OS instance reported
    itself as having no Supervisor - no add-ons, no OS version, no board -
    without a single error. Found by tests/test_ha_api.py, against a real Home
    Assistant; the fakes had patched the missing name in with raising=False.

    The old location stays as a fallback for 2024.6-2024.10, the oldest
    releases hacs.json still admits.
    """
    try:
        from homeassistant.helpers.hassio import is_hassio
    except ImportError:
        from homeassistant.components.hassio import is_hassio  # type: ignore[attr-defined,no-redef]

    return bool(is_hassio(hass))


def _has_component(hass: HomeAssistant, domain: str) -> bool:
    try:
        return domain in hass.config.components
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not check for the %s component", domain, exc_info=True)

        return False


async def _async_system_info(hass: HomeAssistant) -> dict[str, Any]:
    try:
        from homeassistant.helpers.system_info import async_get_system_info

        return dict(await async_get_system_info(hass))
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not read the system info block", exc_info=True)

        return {}


def _install_type(info: dict[str, Any]) -> str:
    reported = _string(info.get("installation_type")) or ""

    return _INSTALL_TYPES.get(reported, "core")


def _instance_uuid(hass: HomeAssistant) -> str:
    """Home Assistant's own instance id.

    Not a secret and not personal: it identifies the installation, not the
    people. It lets the cloud tell "same house, agent reinstalled" from "this is
    a different machine", which matters the day remote access is on the table.
    """
    try:
        instance = hass.data.get("core.uuid")

        if isinstance(instance, str) and instance:
            return instance
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not read the instance uuid", exc_info=True)

    return ""


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
