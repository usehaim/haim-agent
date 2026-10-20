"""The entities a household actually looks at.

One answers "is this thing talking to somebody right now". One answers "is
somebody asking to come in", and one "is somebody in". Both exist so that the questions can be
answered from inside their own Home Assistant, in an automation or on a
dashboard, without taking anybody's word for it - including ours.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HaimConfigEntry
from .entity import HaimEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HaimConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([
        HaimConnectionSensor(entry.runtime_data),
        HaimAccessRequestSensor(entry.runtime_data),
        HaimMaintenanceAccessSensor(entry.runtime_data),
    ])


class HaimConnectionSensor(HaimEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "cloud_connection")

    @property
    def is_on(self) -> bool:
        return self._coordinator.connected


class HaimAccessRequestSensor(HaimEntity, BinarySensorEntity):
    """On while somebody is waiting for an answer.

    It exists so a household can automate their own consent - a light in the
    hall, a message on a screen, a speaker announcement - rather than depending
    on a notification arriving. It reports that a question is pending and never
    what the answer was: the answer is theirs and belongs in their log, not in
    an entity another integration could read.
    """

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "access_request")

    @property
    def is_on(self) -> bool:
        consent = self._coordinator.consent

        return bool(consent is not None and consent.has_pending)


class HaimMaintenanceAccessSensor(HaimEntity, BinarySensorEntity):
    """On while a window is open: somebody may be inside, as an administrator.

    Read-only, like the rest. The way to end it is the repair issue, the portal
    or the panel - an entity another integration could switch off would be a
    way to close, or keep open, a window nobody in the house decided on.
    """

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "maintenance_access")

    @property
    def is_on(self) -> bool:
        remote = self._coordinator.remote

        return bool(remote is not None and remote.is_open)
