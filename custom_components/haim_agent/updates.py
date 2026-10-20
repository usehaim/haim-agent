"""Installing updates, with the safeguards the cloud already decided on.

The division of labour matters and is easy to get backwards. The cloud decides
WHETHER and WHEN: the advisory gate, the maintenance window, whether a backup is
required, and whether this entity can be given a specific version. By the time a
batch arrives here every one of those questions has been answered, and this file
does what it is told - carefully.

What is decided HERE, because only the house knows it:

* Whether the entity still offers the update it was asked about. The inventory
  the cloud reasoned over is as fresh as the last heartbeat, and thirty seconds
  is long enough for somebody standing in front of the machine to have installed
  it themselves.
* Whether the backup actually finished. `update.install` with `backup: true`
  only works where the entity supports it; everywhere else the backup is a
  separate service call that has to be waited for, and "waited for" is the whole
  difference between a safeguard and a ticked box.

Three rules from §10.1 shape the loop:

1. `update.install` is not blocking. An update of Core takes Home Assistant down
   with it, so we report `started` and let the next heartbeat prove what
   happened - there is no completion callback to wait for and pretending
   otherwise would just hang.
2. Capabilities are read, never presumed. Passing `version` to an entity that
   does not accept one makes Home Assistant refuse the call outright, and the
   installer gets an error about a service schema instead of an update.
3. One failure does not stop the chain. If the third of five fails, the other
   two still go in.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# What restarts Home Assistant under our feet. These go LAST in a batch, so that
# everything else in it has already been done by the time the lights go out.
RESTARTING_KINDS = frozenset({"core", "os", "supervisor"})

# How long to wait for a backup before giving up on it. Generous, because a full
# backup of a house with a year of history is not quick, and the alternative to
# waiting is installing over a backup that never completed.
BACKUP_TIMEOUT_SECONDS = 900

BACKUP_POLL_SECONDS = 5

BACKUP_STATE_ENTITY = "sensor.backup_backup_manager_state"

BACKUP_EVENT_ENTITY = "event.backup_automatic_backup"


class UpdateFailed(Exception):
    """One item of a batch could not be installed. The others still run.

    It carries a CODE, and only the code goes back to the panel (sixth
    review). The message names the entity - and an add-on's update entity is
    named after the add-on, `update.allarme_di_giulia_update`, which is exactly
    what the neutral id in `updates` exists to keep in the house (D38). The
    message stays here, in the debug log.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


async def async_install_batch(
    hass: HomeAssistant,
    report,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run one batch, reporting each item on its own uuid.

    Each item is acknowledged separately because a batch containing a Core
    update restarts the instance halfway through: a single report at the end
    would simply never be sent.
    """
    ordered = sorted(items, key=lambda item: str(item.get("kind")) in RESTARTING_KINDS)

    installed = 0
    failed = 0

    for item in ordered:
        uuid = item.get("uuid")

        if not isinstance(uuid, str):
            # Nothing to report against. Skipping is the only honest option:
            # acting on it would install something we could never account for.
            _LOGGER.debug("Skipping an update item with no id")
            continue

        await report(uuid, "started")

        try:
            await _async_install_one(hass, item)
        except Exception as error:  # noqa: BLE001
            _LOGGER.debug("Update %s failed", item.get("entity_id"), exc_info=True)
            # A fixed code, never the exception's text: Home Assistant's own
            # exceptions name entities, paths and URLs.
            await report(uuid, "failed", error.code if isinstance(error, UpdateFailed) else "install_failed")
            failed += 1

            continue

        installed += 1

        # No `succeeded` here, deliberately, whatever the kind. The call is not
        # blocking and returning from it says only that Home Assistant accepted
        # it. What it actually did shows up in the next state, and that is the
        # only evidence worth recording.

    return {"started": installed, "failed": failed}


async def _async_install_one(hass: HomeAssistant, item: dict[str, Any]) -> None:
    entity_id = item.get("entity_id")

    if not isinstance(entity_id, str):
        raise UpdateFailed("missing_entity", "The update item carried no entity.")

    if not entity_id.startswith("update."):
        # Not one of ours: never read its state, never call install on it.
        raise UpdateFailed("not_found", "Not an update entity this house reported.")

    state = hass.states.get(entity_id)

    if state is None:
        raise UpdateFailed("not_found", f"{entity_id} does not exist on this instance.")

    if state.state != "on":
        # Somebody installed it from the interface in the meantime. Calling
        # install anyway is harmless but reporting success would be a lie about
        # what this component did.
        raise UpdateFailed("no_update_pending", f"{entity_id} has no update waiting any more.")

    wants_backup = bool(item.get("backup", True))
    supports_backup = bool(item.get("supports_backup", False))

    if wants_backup and not supports_backup:
        # The entity cannot back itself up, so we do it and WAIT. Without the
        # waiting, "backup required" is a box that was ticked while the update
        # ran alongside a backup that had not finished.
        await _async_backup_and_wait(hass)

    payload: dict[str, Any] = {"entity_id": entity_id}

    version = item.get("version")

    if isinstance(version, str) and version:
        # Only ever set by the cloud where the entity reported
        # SPECIFIC_VERSION. Sending it otherwise makes the call fail outright.
        payload["version"] = version

    if supports_backup:
        payload["backup"] = wants_backup

    # Not blocking, on purpose: a Core update restarts the instance, and waiting
    # for a call that will be killed by the restart hangs the poll loop for
    # nothing.
    await hass.services.async_call("update", "install", payload, blocking=False)


async def _async_backup_and_wait(hass: HomeAssistant) -> None:
    """Take a backup and do not come back until it has finished, or failed."""
    before = _backup_failure(hass)

    await hass.services.async_call("backup", "create_automatic", {}, blocking=True)

    waited = 0

    while waited < BACKUP_TIMEOUT_SECONDS:
        failure = _backup_failure(hass)

        if failure is not None and failure != before:
            raise UpdateFailed("backup_failed", f"The backup failed: {failure}")

        if not _backup_running(hass):
            return

        await asyncio.sleep(BACKUP_POLL_SECONDS)
        waited += BACKUP_POLL_SECONDS

    raise UpdateFailed("backup_timeout", "The backup did not finish in time; nothing was installed.")


def _backup_running(hass: HomeAssistant) -> bool:
    state = hass.states.get(BACKUP_STATE_ENTITY)

    if state is None:
        # No backup manager to watch. It answered the service call, so
        # something took it; there is nothing here to wait on.
        return False

    return state.state in ("create_backup", "receive_backup")


def _backup_failure(hass: HomeAssistant) -> str | None:
    state = hass.states.get(BACKUP_EVENT_ENTITY)

    if state is None:
        return None

    reason = state.attributes.get("failed_reason") or state.attributes.get("reason")

    return str(reason)[:200] if reason else None
