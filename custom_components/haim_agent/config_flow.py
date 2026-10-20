"""Config flow: three questions, and one of them is the important one.

The base URL is asked for and has no value baked into the component. That is
what makes staging, on-premise installs and white-label resellers possible
without building a different component for each - and it is the reason there
will never be a "which environment am I on" branch anywhere in this code.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CertificateRejected, HaimApi, HaimApiError, PairingRejected
from .const import (
    CONF_AGENT_KEY,
    CONF_BASE_URL,
    CONF_LOCALE,
    CONF_LOG_LEVEL,
    CONF_PAIRING_CODE,
    CONF_SITE_UUID,
    DEFAULT_LOG_LEVEL,
    DOMAIN,
    LOG_LEVELS,
)
from .system import async_describe_instance

_LOGGER = logging.getLogger(__name__)


def normalise_pairing_code(typed: str) -> str:
    """The code as the panel minted it, from however it was typed.

    The panel shows twelve characters in three groups (K3M9 P2XA 7H4Q, D47):
    an installer may copy the spaces, put dashes between the groups, or type
    in lower case. None of that is part of the code.
    """
    return "".join(character for character in typed.upper() if not character.isspace() and character != "-")


class HaimConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pair this instance with a HAIM panel."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            base_url = user_input[CONF_BASE_URL].strip().rstrip("/")

            # https only, and there is no way round it - not for loopback, not
            # for the laboratory, not behind a flag. The contract says in so
            # many words that no version of this client has an option to allow
            # plain HTTP or to skip certificate verification, and a local
            # exception is exactly the switch that stops being local.
            if not base_url.startswith("https://"):
                errors[CONF_BASE_URL] = "insecure_url"
            else:
                try:
                    return await self._enroll(base_url, user_input)
                except PairingRejected:
                    errors[CONF_PAIRING_CODE] = "pairing_rejected"
                except CertificateRejected:
                    # Its own message on purpose. "Cannot connect" is what you
                    # see when you mistyped the address; this is what you see
                    # when the address is right and the certificate is not, and
                    # the fix is on the server, never in this component.
                    _LOGGER.debug("The panel's certificate did not verify", exc_info=True)
                    errors[CONF_BASE_URL] = "certificate_rejected"
                except HaimApiError:
                    # Deliberately not the exception text: it can contain a URL
                    # with credentials in it, and this string goes on a screen.
                    _LOGGER.debug("Enrolment failed", exc_info=True)
                    errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=self._schema(user_input),
            errors=errors,
        )

    async def _enroll(self, base_url: str, user_input: dict[str, Any]) -> ConfigFlowResult:
        session = async_get_clientsession(self.hass)
        api = HaimApi(session, base_url)

        description = await async_describe_instance(self.hass)

        response = await api.enroll(
            {
                "pairing_code": normalise_pairing_code(user_input[CONF_PAIRING_CODE]),
                "ha_version": description["ha_version"],
                "install_type": description["install_type"],
                "agent_version": description["agent_version"],
                "instance_uuid": description["instance_uuid"],
                "capabilities": description["capabilities"],
            }
        )

        data = response.data or {}
        site_uuid = str(data.get("site_uuid", ""))

        # One config entry per instance. Adding the same one twice would mean
        # two heartbeats, two sets of alerts and one very confused installer.
        await self.async_set_unique_id(site_uuid or description["instance_uuid"])
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title="HAIM",
            data={
                CONF_BASE_URL: base_url,
                # The key is written into the config entry and never logged,
                # never shown in a form, and never put into a diagnostics dump.
                CONF_AGENT_KEY: data.get("agent_key"),
                CONF_SITE_UUID: site_uuid,
                CONF_LOCALE: user_input.get(CONF_LOCALE) or self.hass.config.language,
                CONF_LOG_LEVEL: user_input.get(CONF_LOG_LEVEL, DEFAULT_LOG_LEVEL),
            },
        )

    def _schema(self, user_input: dict[str, Any] | None) -> vol.Schema:
        user_input = user_input or {}

        return vol.Schema(
            {
                vol.Required(
                    CONF_PAIRING_CODE, default=user_input.get(CONF_PAIRING_CODE, "")
                ): str,
                vol.Required(
                    # No default: the address is the installer's to give. A
                    # built-in one was the laboratory's, and it was offered to
                    # every real house.
                    CONF_BASE_URL, default=user_input.get(CONF_BASE_URL, "")
                ): str,
                vol.Optional(
                    CONF_LOCALE, default=user_input.get(CONF_LOCALE, self.hass.config.language)
                ): vol.In(["it", "en"]),
                vol.Optional(
                    CONF_LOG_LEVEL, default=user_input.get(CONF_LOG_LEVEL, DEFAULT_LOG_LEVEL)
                ): vol.In(list(LOG_LEVELS)),
            }
        )
