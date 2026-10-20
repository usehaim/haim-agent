"""The undocumented Home Assistant APIs this component relies on, against a REAL one.

Every other test file runs against fakes, on purpose: the hash, the queue, the
redaction filter must be testable without a Home Assistant. The price of that is
that a fake cannot notice Home Assistant changing. A fake keeps answering the
way it was written to answer, the suite stays green, and the field turns null on
a customer's instance the day a release renames something.

So this file does the opposite. Each test starts the real component it talks
about, in the real Home Assistant that pytest-homeassistant-custom-component
pins, and asserts the exact shape the agent reads. CI runs it on the latest
stable, the previous one and the current beta: when one of these fails, it is
because the release changed, and the test name says what to go and look at.

The inventory, with what breaks and how, is in docs/verifica/04-api-non-documentate.md.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haim_agent.collector import StateCollector
from custom_components.haim_agent.const import CAPABILITY_SUPERVISOR
from custom_components.haim_agent.maintenance import MaintenanceUser
from custom_components.haim_agent.system import _INSTALL_TYPES, async_describe_instance
from custom_components.haim_agent.web import HaimOpenView


# ---------------------------------------------------------------- system_info


async def test_system_info_still_says_what_kind_of_instance_this_is(hass: HomeAssistant) -> None:
    """`helpers.system_info`: the version, the install type and the Python.

    If `installation_type` changes wording, every instance becomes `core`, the
    poorest profile: add-on management disappears from the panel for every
    customer on HA OS, silently.
    """
    from homeassistant.helpers.system_info import async_get_system_info

    info = await async_get_system_info(hass)

    assert info["version"] == HA_VERSION
    assert isinstance(info["python_version"], str) and info["python_version"]
    assert isinstance(info["installation_type"], str)

    # The four phrases we map are the four Home Assistant still produces. The
    # source of truth is the function itself, read here rather than remembered.
    import inspect

    from homeassistant.helpers import system_info

    source = inspect.getsource(system_info)

    for phrase in _INSTALL_TYPES:
        assert f'"{phrase}"' in source, f"Home Assistant no longer says {phrase!r}"

    described = await async_describe_instance(hass)

    assert described["ha_version"] == HA_VERSION
    assert described["install_type"] in set(_INSTALL_TYPES.values())


async def test_the_instance_uuid_is_still_where_we_read_it(hass: HomeAssistant) -> None:
    """`hass.data["core.uuid"]`: without it every reinstall looks like a new machine."""
    from homeassistant.helpers import instance_id

    expected = await instance_id.async_get(hass)
    described = await async_describe_instance(hass)

    assert described["instance_uuid"] == expected


# ------------------------------------------------------------- config_entries


async def test_broken_config_entries_are_still_counted(hass: HomeAssistant) -> None:
    """`config_entries.async_entries()` with no domain, and the three broken states."""
    MockConfigEntry(domain="mqtt", state=ConfigEntryState.SETUP_ERROR).add_to_hass(hass)
    MockConfigEntry(domain="zha", state=ConfigEntryState.SETUP_RETRY).add_to_hass(hass)
    # NOT_LOADED rather than LOADED: teardown would try to unload a real hue.
    MockConfigEntry(domain="hue", state=ConfigEntryState.NOT_LOADED).add_to_hass(hass)

    collector = StateCollector(hass)

    assert collector._config_entry_errors() == 2
    assert {row["domain"] for row in collector._integrations()} >= {"mqtt", "zha", "hue"}
    assert ConfigEntryState.MIGRATION_ERROR  # still exists, still counted as broken


async def test_custom_components_are_still_told_apart_from_core(hass: HomeAssistant) -> None:
    """`hass.data["custom_components"]` and `loader.async_get_custom_components`.

    The import sits inside the try block that decides the source. If it goes,
    every integration reports `core`, including the custom ones an installer
    most needs to know about.
    """
    from homeassistant.loader import DATA_CUSTOM_COMPONENTS, async_get_custom_components

    assert DATA_CUSTOM_COMPONENTS == "custom_components"

    await async_get_custom_components(hass)
    hass.data[DATA_CUSTOM_COMPONENTS] = {"haim_agent": object()}

    collector = StateCollector(hass)

    assert collector._integration_source("haim_agent") == "custom"
    assert collector._integration_source("mqtt") == "core"


# ------------------------------------------------------------------- repairs


async def test_repair_issues_still_carry_domain_severity_and_dismissal(hass: HomeAssistant) -> None:
    """The issue registry, read field by field: `domain`, `severity`, `dismissed_version`."""
    for issue_id, severity in (("a", ir.IssueSeverity.CRITICAL), ("b", ir.IssueSeverity.WARNING)):
        ir.async_create_issue(
            hass,
            "shelly",
            issue_id,
            is_fixable=False,
            severity=severity,
            translation_key="whatever",
        )

    ir.async_create_issue(
        hass, "zwave_js", "c", is_fixable=False, severity=ir.IssueSeverity.ERROR, translation_key="x"
    )
    ir.async_ignore_issue(hass, "zwave_js", "c", True)

    collector = StateCollector(hass)

    assert collector._repairs() == [{"domain": "shelly", "count": 2, "critical": 1}]
    assert collector._problems()["repair_issues"] == 2
    assert collector._problems()["repair_issues_critical"] == 1


async def test_a_fixable_issue_with_placeholders_and_data_can_still_be_raised(hass: HomeAssistant) -> None:
    """Exactly the keyword arguments consent.py, repairs.py and coordinator.py pass."""
    ir.async_create_issue(
        hass,
        "haim_agent",
        "access_request_x",
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="access_request",
        translation_placeholders={"installer": "x", "subject": "", "reason": "", "minutes": "60"},
        data={"request_uuid": "x"},
    )

    issue = ir.async_get(hass).async_get_issue("haim_agent", "access_request_x")

    assert issue is not None and issue.is_fixable and issue.data == {"request_uuid": "x"}

    ir.async_delete_issue(hass, "haim_agent", "access_request_x")

    assert ir.async_get(hass).async_get_issue("haim_agent", "access_request_x") is None

    from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow

    assert issubclass(ConfirmRepairFlow, RepairsFlow)


# -------------------------------------------------------------------- backup


@pytest.fixture
async def backup_ready(hass: HomeAssistant) -> HomeAssistant:
    assert await async_setup_component(hass, "backup", {})
    # The sensors live on a config entry that a discovery flow creates, and the
    # entry's first refresh runs in the background.
    await hass.async_block_till_done(wait_background_tasks=True)

    return hass


async def test_the_backup_manager_entities_still_have_the_ids_we_read(backup_ready: HomeAssistant) -> None:
    """Four sensors and one event entity, found by entity id.

    The ids come out of the English translation of each entity's name. A
    rename in Home Assistant's strings is enough to move them, and then every
    backup field turns null - which the panel shows as "never backed up".
    """
    hass = backup_ready

    for entity_id in (
        "sensor.backup_backup_manager_state",
        "sensor.backup_last_successful_automatic_backup",
        "sensor.backup_last_attempted_automatic_backup",
        "sensor.backup_next_scheduled_automatic_backup",
        "event.backup_automatic_backup",
    ):
        assert hass.states.get(entity_id) is not None, f"{entity_id} is gone"

    assert StateCollector(hass)._backup()["state"] == "idle"


async def test_the_backup_manager_states_are_still_the_ones_we_map(backup_ready: HomeAssistant) -> None:
    """`blocked` must stay distinct from a failure, and `create_backup` must stay what "running" means."""
    from homeassistant.components.backup.manager import BackupManagerState

    states = {state.value for state in BackupManagerState}

    assert {"idle", "create_backup", "receive_backup", "blocked"} <= states

    options = backup_ready.states.get("sensor.backup_backup_manager_state").attributes["options"]

    assert set(options) == states


async def test_the_backup_failure_still_carries_its_reason(backup_ready: HomeAssistant) -> None:
    """`event.backup_automatic_backup` with `failed_reason`, and the service we call to take one."""
    from homeassistant.components.backup.event import ATTR_FAILED_REASON

    assert ATTR_FAILED_REASON == "failed_reason"
    assert backup_ready.services.has_service("backup", "create_automatic")


async def test_the_number_of_backup_agents_is_actually_reported(backup_ready: HomeAssistant) -> None:
    assert StateCollector(backup_ready)._backup()["agents_configured"] is not None


# ------------------------------------------------------------ auth and users


@pytest.fixture
async def http_ready(hass: HomeAssistant) -> HomeAssistant:
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "api", {})
    assert await async_setup_component(hass, "websocket_api", {})
    await hass.async_block_till_done()

    return hass


async def test_the_maintenance_account_can_still_be_created_woken_and_parked(
    hass: HomeAssistant, hass_owner_user
) -> None:
    """`hass.auth`: create, activate, long-lived token, deactivate.

    And the property D27 rests on: parking the account destroys its tokens.
    If Home Assistant ever stops doing that, a token outlives its window and
    the promise in portal.promise is no longer true.

    `hass_owner_user` is not decoration: on an instance with no users at all
    Home Assistant makes the first one the owner, and an owner cannot be
    deactivated. Every real house has finished onboarding, so it has one.
    """
    account = MaintenanceUser(hass)
    token = await account.async_wake(30)

    user = await account.async_find()
    refresh = hass.auth.async_validate_access_token(token)

    assert user is not None and user.is_active and user.is_admin
    assert refresh is not None and refresh.user.id == user.id
    assert refresh.token_type == "long_lived_access_token"
    assert refresh.access_token_expiration == timedelta(minutes=30)

    await account.async_park()

    assert not (await account.async_find()).is_active
    assert hass.auth.async_validate_access_token(token) is None
    assert not (await account.async_find()).refresh_tokens


async def test_the_token_still_opens_the_rest_api_and_the_websocket(
    http_ready: HomeAssistant, hass_owner_user, hass_client_no_auth, hass_ws_client
) -> None:
    """The two doors the frontend uses once `hassTokens` is in localStorage.

    The frontend half (the localStorage key and its fields) lives in the
    home-assistant-frontend package and is checked by the CI step "Frontend
    hassTokens". This is the server half: the token we mint is accepted.
    """
    hass = http_ready
    token = await MaintenanceUser(hass).async_wake(30)

    client = await hass_client_no_auth()
    response = await client.get("/api/", headers={"Authorization": f"Bearer {token}"})

    assert response.status == 200

    websocket = await hass_ws_client(hass, access_token=token)
    await websocket.send_json({"id": 1, "type": "ping"})

    assert (await websocket.receive_json())["type"] == "pong"

    await websocket.close()

    await MaintenanceUser(hass).async_park()

    response = await client.get("/api/", headers={"Authorization": f"Bearer {token}"})

    assert response.status == 401


async def test_the_door_is_still_served_without_a_home_assistant_token(
    http_ready: HomeAssistant, hass_owner_user, hass_client_no_auth
) -> None:
    """`HomeAssistantView` with `requires_auth = False`, on the real HTTP server."""
    hass = http_ready
    token = await MaintenanceUser(hass).async_wake(30)

    class Door:
        async def async_redeem(self, ticket: str) -> dict[str, Any] | None:
            return {"token": token, "expires_at_ms": 4_102_444_800_000} if ticket == "good" else None

    hass.http.register_view(HaimOpenView(Door()))
    client = await hass_client_no_auth()

    refused = await client.post("/api/haim/open", data={"ticket": "bad"})
    opened = await client.post("/api/haim/open", data={"ticket": "good"})

    assert refused.status == 404
    assert opened.status == 200
    assert token in await opened.text()


# ---------------------------------------------------------------- supervisor


def _model(payload: dict[str, Any]) -> SimpleNamespace:
    """What the hassio coordinator keeps in hass.data: something with to_dict()."""
    return SimpleNamespace(to_dict=lambda: dict(payload))


async def test_the_add_on_list_is_still_folded_into_supervisor_info(hass: HomeAssistant) -> None:
    """`get_supervisor_info(hass)["addons"]` - deprecated since 2026.4.0.

    Home Assistant still folds the add-on list into the Supervisor info "for
    backwards compatibility", and says it can be removed after the deprecation
    period. The day it is, this test fails and the add-ons in the panel would
    otherwise go to an empty list without a single error. The replacement is
    `get_apps_list` / `get_addons_list` (architecture §16.3).
    """
    from homeassistant.components.hassio import const as hassio_const

    hass.data[hassio_const.DATA_SUPERVISOR_INFO] = _model({"version": "2026.10.0"})
    hass.data[hassio_const.DATA_HOST_INFO] = _model({"chassis": "embedded"})
    hass.data[hassio_const.DATA_OS_INFO] = _model({"version": "18.2", "board": "green"})
    hass.data[hassio_const.DATA_ADDONS_LIST] = [
        _model({"slug": "core_mosquitto", "name": "Mosquitto", "version": "6.5", "update_available": False, "state": "started"})
    ]

    block = await StateCollector(hass)._async_supervisor_block([CAPABILITY_SUPERVISOR])

    assert block["supervisor_version"] == "2026.10.0"
    assert block["os_version"] == "18.2"
    assert block["board"] == "green"
    assert [addon["slug"] for addon in block["addons"]] == ["core_mosquitto"]


async def test_the_supervisor_helpers_we_import_still_exist() -> None:
    """Including the `apps` names the 2026.2 rename introduced, which are the way forward."""
    from homeassistant.components import hassio

    for name in (
        "get_host_info",
        "get_os_info",
        "get_supervisor_info",
        "get_addons_list",
        "get_apps_list",
        "HassioNotReadyError",
    ):
        assert hasattr(hassio, name), f"homeassistant.components.hassio.{name} is gone"


async def test_a_supervisor_is_still_recognised_where_there_is_one(hass: HomeAssistant) -> None:
    """`is_hassio`, which left `homeassistant.components.hassio` for `helpers.hassio`.

    The agent imported it from the old place until this file existed, and on
    every supported release that import failed and every HA OS instance
    reported no Supervisor at all.
    """
    from custom_components.haim_agent.metrics import _async_from_supervisor
    from custom_components.haim_agent.system import async_capabilities, supervisor_present

    assert not supervisor_present(hass)
    assert CAPABILITY_SUPERVISOR not in async_capabilities(hass)

    hass.config.components.add("hassio")

    assert supervisor_present(hass)
    assert CAPABILITY_SUPERVISOR in async_capabilities(hass)

    # And the metrics side now gets as far as asking the Supervisor: with no
    # host info loaded it gives up quietly, as it should, instead of never asking.
    from homeassistant.components.hassio import const as hassio_const

    hass.data[hassio_const.DATA_HOST_INFO] = _model({"disk_total": 200.0, "disk_used": 50.0})

    assert (await _async_from_supervisor(hass))["disk"] == 25.0


# -------------------------------------------------------- lovelace, system_log


async def test_dashboards_are_still_counted_from_lovelace_data(hass: HomeAssistant) -> None:
    """`hass.data["lovelace"].dashboards` - a dataclass, internal, and the only count there is."""
    assert await async_setup_component(hass, "lovelace", {})
    await hass.async_block_till_done()

    lovelace = hass.data.get("lovelace")

    assert isinstance(getattr(lovelace, "dashboards", None), dict)
    assert StateCollector(hass)._dashboard_count() == len(lovelace.dashboards)


async def test_logged_errors_are_still_read_from_system_log(hass: HomeAssistant) -> None:
    """`hass.data["system_log"].records`, with level, source, message, timestamp and count."""
    assert await async_setup_component(hass, "system_log", {})
    await hass.async_block_till_done()

    logging.getLogger("homeassistant.components.haim_probe").error("probe failed")
    await hass.async_block_till_done()

    records = list(hass.data["system_log"].records.values())

    assert records, "system_log no longer keeps records where we read them"

    record = records[-1]

    for attribute in ("level", "source", "message", "timestamp", "count"):
        assert hasattr(record, attribute), f"system_log records lost `{attribute}`"

    logs = StateCollector(hass)._logs()

    assert logs["errors"] >= 1


# -------------------------------------------------------------------- update


async def test_update_feature_flags_and_service_fields_are_still_there(hass: HomeAssistant) -> None:
    """`UpdateEntityFeature.BACKUP` / `SPECIFIC_VERSION`, and `update.install` taking both."""
    from homeassistant.components.update import ATTR_BACKUP, ATTR_VERSION, UpdateEntityFeature

    assert UpdateEntityFeature.BACKUP and UpdateEntityFeature.SPECIFIC_VERSION
    assert (ATTR_BACKUP, ATTR_VERSION) == ("backup", "version")

    assert await async_setup_component(hass, "update", {})
    await hass.async_block_till_done()

    assert hass.services.has_service("update", "install")


async def test_an_integration_update_is_not_mistaken_for_an_add_on(hass: HomeAssistant) -> None:
    from homeassistant.components.update import UpdateEntity

    class IntegrationUpdate(UpdateEntity):
        _attr_installed_version = "2.0.0"
        _attr_latest_version = "2.1.0"
        _attr_title = "HACS"

    entity = IntegrationUpdate()
    entity.hass = hass
    attributes = entity.state_attributes

    assert StateCollector(hass)._update_kind("update.hacs_update", attributes) == "integration"
