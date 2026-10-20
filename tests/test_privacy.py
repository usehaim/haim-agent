"""Verifica 03: nothing that describes the people leaves the house.

One fake Home Assistant, furnished the way a real family furnishes theirs -
rooms named after the children, a public address with the surname in it, an
email as an account name, the house's coordinates - and then EVERYTHING the
component can send is serialised and searched for any of it: the hashed state,
the volatile block at the most generous log level, the enrolment description,
and the result of a command that failed with a message full of names.

Searching the output is the point. A list of the fields we think are safe is a
proxy; this is the thing itself, and it is what caught the backup failure
reason, the log source and the firmware page at a house address.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from custom_components.haim_agent.collector import StateCollector
from custom_components.haim_agent.const import LOG_LEVEL_CATEGORIES, LOG_LEVEL_FULL

from test_collector import FakeHass, state

# The installation's pseudonym key (D40), fixed so that expectations can be
# computed. In a house it is random and lives in the config entry.
KEY = "0" * 64


def _p(value: str) -> str:
    import hashlib
    import hmac

    return hmac.new(KEY.encode(), value.encode(), hashlib.sha256).hexdigest()[:12]


GIULIA_UPDATE = "update.haim_addon_" + _p("update.allarme_di_giulia_update")

# Every string here identifies this family, this house or where it is. None of
# them may appear anywhere in anything the component sends.
PERSONAL = [
    "Giulia", "giulia",
    "Rossi", "rossi",
    "cucina",
    "Camera di Giulia",
    "casa-rossi.duckdns.org",
    "anna.rossi@example.com",
    "45.4064", "11.8768",
    "CasaRossi_5G",
    "192.168.1.77",
    "aa:bb:cc:dd:ee:ff",
    "nas-rossi",
]


def household() -> FakeHass:
    states = {
        "light.cucina_rossi": state("light.cucina_rossi", "on", friendly_name="Luce cucina Rossi"),
        "light.camera_di_giulia": state("light.camera_di_giulia", "unavailable", friendly_name="Camera di Giulia"),
        "person.giulia_rossi": state("person.giulia_rossi", "home", friendly_name="Giulia Rossi",
                                     latitude=45.4064, longitude=11.8768),
        "device_tracker.telefono_giulia": state("device_tracker.telefono_giulia", "home", ip="192.168.1.77",
                                                mac="aa:bb:cc:dd:ee:ff"),
        "automation.sveglia_giulia": state("automation.sveglia_giulia", "on", friendly_name="Sveglia di Giulia"),
        "script.buonanotte_rossi": state("script.buonanotte_rossi", "off"),
        "sensor.wifi_ssid": state("sensor.wifi_ssid", "CasaRossi_5G"),
        "zone.home": state("zone.home", "1", friendly_name="Casa Rossi", latitude=45.4064, longitude=11.8768),
        "update.home_assistant_core_update": state(
            "update.home_assistant_core_update", "on",
            installed_version="2026.8.2", latest_version="2026.8.4", supported_features=3,
            release_url="https://www.home-assistant.io/blog/",
        ),
        "sensor.backup_backup_manager_state": state("sensor.backup_backup_manager_state", "idle", agents=["local"]),
        "event.backup_automatic_backup": state(
            "event.backup_automatic_backup", "2026-08-24T03:00:00+00:00",
            failed_reason="Upload to smb://nas-rossi.local/backup failed for anna.rossi@example.com",
        ),
    }

    records = {
        1: SimpleNamespace(
            level="ERROR",
            source=("/config/custom_components/giulia_alarm/__init__.py", 12),
            name="custom_components.giulia_alarm",
            message=["Error in automation automation.sveglia_giulia: light.camera_di_giulia unavailable"],
            timestamp=1724490000.0,
            count=3,
        ),
        2: SimpleNamespace(
            level="ERROR",
            source=("homeassistant/components/http/ban.py", 40),
            name="homeassistant.components.http.ban",
            message=["Login attempt or request with invalid authentication from casa-rossi.duckdns.org (192.168.1.77). "
                     "Requested URL: '/api/states?token=abc123'. (Mozilla/5.0) user anna.rossi@example.com"],
            timestamp=1724490060.0,
        ),
        3: SimpleNamespace(
            level="CRITICAL",
            source=("homeassistant/components/zone/__init__.py", 1),
            name="homeassistant.components.zone",
            message=["Home zone at 45.4064, 11.8768 for Casa Rossi; wifi 'CasaRossi_5G' mac aa:bb:cc:dd:ee:ff"],
            timestamp=1724490120.0,
        ),
    }

    hass = FakeHass(
        states=states,
        entries=[
            SimpleNamespace(domain="hue", state=None, title="Hue di casa Rossi"),
            SimpleNamespace(domain="mobile_app", state=None, title="iPhone di Giulia"),
        ],
        data={"system_log": SimpleNamespace(records=records)},
        components={"system_log", "backup", "mobile_app"},
    )

    hass.config.location_name = "Casa Rossi"
    hass.config.latitude = 45.4064
    hass.config.longitude = 11.8768
    hass.config.external_url = "https://casa-rossi.duckdns.org:8123"

    return hass


@pytest.fixture
def registries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registries full of names, so that the counts are counted FROM names."""
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers import issue_registry as ir

    monkeypatch.setattr(ar, "async_get", lambda hass: SimpleNamespace(
        areas={"a": SimpleNamespace(name="Camera di Giulia"), "b": SimpleNamespace(name="Cucina")}))
    monkeypatch.setattr(dr, "async_get", lambda hass: SimpleNamespace(
        devices={"d": SimpleNamespace(name="Lampada di Giulia", name_by_user="Lampada cucina Rossi")}))
    monkeypatch.setattr(er, "async_get", lambda hass: SimpleNamespace(
        entities={"e": SimpleNamespace(entity_id="light.cucina_rossi", original_name="Cucina")}))
    monkeypatch.setattr(ir, "async_get", lambda hass: SimpleNamespace(issues={
        1: SimpleNamespace(domain="hue", issue_id="unreachable_light_cucina_rossi",
                           translation_placeholders={"name": "Luce cucina Rossi"},
                           severity="IssueSeverity.warning", dismissed_version=None),
    }))


@pytest.fixture
def private_address(monkeypatch: pytest.MonkeyPatch) -> None:
    import homeassistant.components.network as network

    async def source_ip(hass):
        return "192.168.1.40"

    monkeypatch.setattr(network, "async_get_source_ip", source_ip, raising=False)


@pytest.mark.parametrize("level", [LOG_LEVEL_CATEGORIES, LOG_LEVEL_FULL])
async def test_nothing_personal_reaches_any_part_of_the_heartbeat(level, registries, private_address) -> None:
    collector = StateCollector(household(), log_level=level)

    payload = {
        "state": await collector.async_build_state(),
        "volatile": await collector.async_build_volatile(),
    }

    everything = json.dumps(payload, ensure_ascii=False)

    for leak in PERSONAL:
        assert leak not in everything, f"the heartbeat carries {leak!r}"

    # And it is still a heartbeat worth having: counted, not emptied.
    assert payload["state"]["counts"]["areas"] == 2
    assert payload["state"]["counts"]["devices"] == 1
    assert payload["state"]["counts"]["automations"] == 1
    assert payload["state"]["repairs"] == [{"domain": "hue", "count": 1, "critical": 0}]
    assert payload["volatile"]["logs"]["errors"] == 3
    assert len(payload["volatile"]["logs"]["recent"]) == 3
    assert payload["state"]["backup"]["state"] == "failed"
    # The one address that is allowed out, by contract: the instance's own,
    # private, so an installer at the router knows which box to look at.
    assert payload["volatile"]["ip_local"] == "192.168.1.40"


async def test_the_backup_failure_reason_is_filtered(registries) -> None:
    """Found by the test above: the reason was sent verbatim, up to 200
    characters, and backup agents name where they write."""
    state_block = await StateCollector(household(), pseudonym_key=KEY).async_build_state()

    reason = state_block["backup"]["last_failure_reason"]

    assert reason is not None
    assert "nas-rossi" not in reason
    assert "anna.rossi@example.com" not in reason
    assert "Upload to" in reason


async def test_a_firmware_page_on_the_house_network_does_not_travel(registries) -> None:
    hass = household()
    hass.states._states["update.home_assistant_core_update"].attributes["release_url"] = (
        "http://192.168.1.77/fw/notes?key=abc123"
    )

    state_block = await StateCollector(hass, pseudonym_key=KEY).async_build_state()

    url = state_block["updates"][0]["release_url"]

    assert "192.168.1.77" not in url
    assert "abc123" not in url


async def test_the_enrolment_describes_the_instance_and_nothing_else() -> None:
    """What `/enroll` carries comes from here, and only from here."""
    from custom_components.haim_agent.system import async_describe_instance

    description = await async_describe_instance(household())

    assert set(description) == {
        "ha_version", "agent_version", "install_type", "python_version", "instance_uuid", "capabilities",
    }

    everything = json.dumps(description, ensure_ascii=False)

    for leak in PERSONAL:
        assert leak not in everything


async def test_a_failed_command_does_not_carry_names_back_in_its_error() -> None:
    """A command result's `error` is an exception's text, and Home Assistant's
    exceptions name the entity that failed - and sometimes the URL, with its
    password, that it failed to reach."""
    from custom_components.haim_agent.command_channel import CommandDispatcher

    reported: list[dict] = []

    class Channel:
        async def async_receive(self):
            return None

        async def async_report(self, command_uuid, status, error=None, data=None):
            reported.append({"status": status, "error": error, "data": data})

    dispatcher = CommandDispatcher(Channel())

    async def explode(_data):
        raise RuntimeError(
            "Service light.turn_on failed for light.camera_di_giulia via "
            "http://admin:S3cret@casa-rossi.duckdns.org:8123 (anna.rossi@example.com)"
        )

    dispatcher.register("self_test", explode)

    await dispatcher.async_dispatch({"action": "self_test", "command_uuid": "c-1", "data": {}})

    failed = reported[-1]

    assert failed["status"] == "failed"

    for leak in PERSONAL + ["S3cret"]:
        assert leak not in failed["error"]

    # A fixed code: the kind of failure, never the exception's text.
    assert failed["error"] == "command_failed:RuntimeError"


async def test_a_device_firmware_update_does_not_name_the_device(registries) -> None:
    hass = household()
    hass.states._states["update.lampada_camera_di_giulia_firmware"] = state(
        "update.lampada_camera_di_giulia_firmware", "on",
        installed_version="1.0.0", latest_version="1.1.0", supported_features=1, device_class="firmware",
    )

    collector = StateCollector(hass, pseudonym_key=KEY)
    state_block = await collector.async_build_state()

    assert "giulia" not in json.dumps(state_block)

    # Decided in D40: it travels under a pseudonym, and a command naming the
    # pseudonym still reaches the real device.
    neutral = "update.haim_device_" + _p("update.lampada_camera_di_giulia_firmware")
    assert [row["entity_id"] for row in state_block["updates"] if row["kind"] == "device"] == [neutral]
    assert collector.resolve_update_entity(neutral) == "update.lampada_camera_di_giulia_firmware"


# --------------------------------------------------------------------------
# Third security review: names that somebody CHOSE, in fields we thought were
# only software names.
# --------------------------------------------------------------------------


def _supervised(monkeypatch, addons, *, apps_list=True):
    import custom_components.haim_agent.collector as collector_module
    import homeassistant.components.hassio as hassio

    async def describe(hass):
        return {"ha_version": "2026.10.1", "agent_version": "1.0.0", "install_type": "os",
                "python_version": "3.14.0", "instance_uuid": "", "capabilities": ["supervisor"]}

    monkeypatch.setattr(collector_module, "async_describe_instance", describe)
    monkeypatch.setattr(hassio, "get_host_info", lambda hass: {"chassis": "vm"}, raising=False)
    monkeypatch.setattr(hassio, "get_os_info", lambda hass: {"version": "18.2", "board": "rpi5-64"}, raising=False)
    monkeypatch.setattr(hassio, "get_supervisor_info", lambda hass: {"version": "2026.10.0", "addons": addons},
                        raising=False)

    if apps_list:
        monkeypatch.setattr(hassio, "get_apps_list", lambda hass: addons, raising=False)
    else:
        def missing(hass):
            raise AttributeError("get_apps_list does not exist before 2026.4")

        monkeypatch.setattr(hassio, "get_apps_list", missing, raising=False)


async def test_a_local_add_on_does_not_carry_the_name_its_author_gave_it(monkeypatch) -> None:
    """`local_*` add-ons are built in the house, named by whoever built them."""
    _supervised(monkeypatch, [
        {"slug": "local_allarme_giulia", "name": "Allarme di Giulia", "version": "0.1.0", "state": "started"},
        {"slug": "core_mosquitto", "name": "Mosquitto broker", "version": "7.1.0", "state": "started"},
    ])

    payload = await StateCollector(household(), pseudonym_key=KEY).async_build_state()

    everything = json.dumps(payload)

    assert "giulia" not in everything.lower()

    local, published = payload["addons"]

    assert local["slug"] == "local_" + _p("local_allarme_giulia")
    assert local["name"] is None
    # Published software is named as it is: that is the point of the field.
    assert published == {"slug": "core_mosquitto", "name": "Mosquitto broker", "version": "7.1.0",
                         "update_available": False, "state": "started"}

    # Stable, or the state hash would move on every heartbeat (rule 12).
    again = await StateCollector(household(), pseudonym_key=KEY).async_build_state()

    assert again["addons"][0]["slug"] == local["slug"]


async def test_the_local_slug_cannot_be_reversed_by_guessing(monkeypatch) -> None:
    """Fourth review: `local_` + sha256(slug)[:8] had no secret in it.

    Whoever saw the digest hashed `local_allarme_giulia`, `local_casa_rossi`
    and the like until one matched. What travels now is keyed with a secret
    that never leaves the house (D40): without it, no guess reproduces it.
    """
    import hashlib

    slug = "local_allarme_giulia"
    _supervised(monkeypatch, [{"slug": slug, "name": "Allarme di Giulia", "version": "0.1.0"}])

    payload = await StateCollector(household(), pseudonym_key=KEY).async_build_state()
    everything = json.dumps(payload)

    sent = payload["addons"][0]["slug"]

    assert sent == "local_" + _p(slug)

    # The same slug under another house's key is another pseudonym.
    other = await StateCollector(household(), pseudonym_key="1" * 64).async_build_state()
    assert other["addons"][0]["slug"] != sent

    for guess in (slug, "allarme_giulia"):
        digest = hashlib.sha256(guess.encode()).hexdigest()

        assert digest[:8] not in everything
        assert digest[:8] not in sent

    # Two local add-ons are told apart, and the pseudonyms do not depend on
    # the order the Supervisor lists them in (rule 12).
    _supervised(monkeypatch, [
        {"slug": "local_zeta", "name": "Zeta", "version": "9"},
        {"slug": "local_alfa", "name": "Alfa", "version": "1"},
    ])
    first = (await StateCollector(household(), pseudonym_key=KEY).async_build_state())["addons"]

    _supervised(monkeypatch, [
        {"slug": "local_alfa", "name": "Alfa", "version": "1"},
        {"slug": "local_zeta", "name": "Zeta", "version": "9"},
    ])
    second = (await StateCollector(household(), pseudonym_key=KEY).async_build_state())["addons"]

    assert sorted(a["slug"] for a in first) == sorted(a["slug"] for a in second) == sorted(["local_" + _p("local_alfa"), "local_" + _p("local_zeta")])
    assert {a["version"]: a["slug"] for a in first} == {a["version"]: a["slug"] for a in second}


async def test_a_local_add_on_s_update_row_passes_the_same_gate(monkeypatch) -> None:
    """Fourth review, sibling-gate parity: the add-on row hid the name, its
    update row carried it as `slug` (the update entity's title)."""
    from homeassistant.helpers import entity_registry as er

    _supervised(monkeypatch, [
        {"slug": "local_allarme_giulia", "name": "Allarme di Giulia", "version": "0.1.0"},
        {"slug": "core_mosquitto", "name": "Mosquitto broker", "version": "7.1.0"},
    ])
    monkeypatch.setattr(er, "async_get", lambda hass: SimpleNamespace(
        entities={}, async_get=lambda entity_id: SimpleNamespace(platform="hassio")))

    hass = household()
    hass.states._states["update.local_addon_update"] = state(
        "update.local_addon_update", "on", title="Allarme di Giulia",
        installed_version="0.1.0", latest_version="0.2.0", supported_features=0,
    )
    hass.states._states["update.mosquitto_broker_update"] = state(
        "update.mosquitto_broker_update", "on", title="Mosquitto broker",
        installed_version="7.0.0", latest_version="7.1.0", supported_features=0,
    )

    payload = await StateCollector(hass, pseudonym_key=KEY).async_build_state()

    slugs = {row["entity_id"]: row["slug"] for row in payload["updates"] if row["kind"] == "addon"}

    # The local add-on's row travels under its pseudonym, entity id included
    # (fifth review: the entity id is built from the same name).
    assert slugs["update.haim_addon_" + _p("update.local_addon_update")] == payload["addons"][0]["slug"]
    assert payload["addons"][0]["slug"] == "local_" + _p("local_allarme_giulia")
    assert slugs["update.mosquitto_broker_update"] == "Mosquitto broker"
    assert "giulia" not in json.dumps(payload).lower()


async def test_a_custom_integration_named_after_the_house_loses_the_name(registries) -> None:
    hass = household()
    hass.config_entries._entries.append(SimpleNamespace(domain="casa_rossi_clima", state=None, title="Clima"))

    payload = await StateCollector(hass, pseudonym_key=KEY).async_build_state()

    assert "rossi" not in json.dumps(payload["integrations"]).lower()
    # The others are still there by their domain.
    assert {"hue", "mobile_app"} <= {row["domain"] for row in payload["integrations"]}


async def test_config_entry_titles_never_travel(registries) -> None:
    """Checked and excluded: titles are where people write names, and no field
    reads them. Kept as a test so that nobody adds one."""
    payload = await StateCollector(household(), pseudonym_key=KEY).async_build_state()

    everything = json.dumps(payload)

    assert "Hue di casa Rossi" not in everything
    assert "iPhone di Giulia" not in everything


# --------------------------------------------------------------------------
# Session 04, against real Home Assistant 2026.8.3 - 2026.10.0b4.
# --------------------------------------------------------------------------


async def test_an_integration_update_is_not_an_add_on(monkeypatch) -> None:
    """Every update entity carries `in_progress`; the platform says what it is."""
    from homeassistant.helpers import entity_registry as er

    _supervised(monkeypatch, [{"slug": "core_mosquitto", "name": "Mosquitto broker", "version": "7.1.0"}])

    platforms = {"update.hacs_update": "hacs", "update.mosquitto_broker_update": "hassio",
                 "update.shelly_firmware": "shelly"}

    monkeypatch.setattr(er, "async_get", lambda hass: SimpleNamespace(
        entities={}, async_get=lambda entity_id: SimpleNamespace(platform=platforms[entity_id])))

    hass = FakeHass(states={
        "update.hacs_update": state("update.hacs_update", "off", in_progress=False, title="HACS",
                                    installed_version="2.0.1", latest_version="2.0.1", supported_features=0),
        "update.mosquitto_broker_update": state("update.mosquitto_broker_update", "on", in_progress=False,
                                                title="Mosquitto broker", installed_version="7.0.0",
                                                latest_version="7.1.0", supported_features=0),
        "update.shelly_firmware": state("update.shelly_firmware", "off", in_progress=False, device_class="firmware",
                                        installed_version="1.4.0", latest_version="1.4.0", supported_features=0),
    })

    kinds = {row["entity_id"]: (row["kind"], row["slug"])
             for row in (await StateCollector(hass, pseudonym_key=KEY).async_build_state())["updates"]}

    assert kinds == {
        # HACS's own update too: a HACS update is named after a repository,
        # and nobody here can vouch for who wrote it (D41).
        "update.haim_integration_" + _p("update.hacs_update"): ("integration", None),
        "update.mosquitto_broker_update": ("addon", "Mosquitto broker"),
        "update.haim_device_" + _p("update.shelly_firmware"): ("device", None),
    }


async def test_backup_agents_are_counted_from_the_manager_configuration() -> None:
    """The sensor has no `agents` attribute: this was null everywhere."""
    hass = household()
    hass.data["backup"] = SimpleNamespace(config=SimpleNamespace(data=SimpleNamespace(
        create_backup=SimpleNamespace(agent_ids=["backup.local", "hassio.nas", "cloud.cloud"]))))
    hass.states._states["sensor.backup_backup_manager_state"] = state("sensor.backup_backup_manager_state", "idle")

    payload = await StateCollector(hass, pseudonym_key=KEY).async_build_state()

    assert payload["backup"]["agents_configured"] == 3


async def test_add_ons_come_from_get_apps_list_and_fall_back_before_2026_4(monkeypatch) -> None:
    import homeassistant.components.hassio as hassio

    _supervised(monkeypatch, [{"slug": "core_mosquitto", "version": "7.1.0", "state": "started"}])
    monkeypatch.setattr(hassio, "get_supervisor_info", lambda hass: {"version": "2026.10.0", "addons": []},
                        raising=False)

    # 2026.4 and later: the new list, even where the deprecated one is empty.
    assert [a["slug"] for a in (await StateCollector(FakeHass(), pseudonym_key=KEY).async_build_state())["addons"]] == ["core_mosquitto"]

    # Before 2026.4: no get_apps_list, the list inside supervisor info.
    _supervised(monkeypatch, [{"slug": "core_samba", "version": "12.0", "state": "started"}], apps_list=False)

    assert [a["slug"] for a in (await StateCollector(FakeHass(), pseudonym_key=KEY).async_build_state())["addons"]] == ["core_samba"]


async def test_one_missing_supervisor_source_costs_one_field(monkeypatch) -> None:
    """Rule 9: the host query failing used to wipe add-ons and versions too."""
    import homeassistant.components.hassio as hassio

    _supervised(monkeypatch, [{"slug": "core_mosquitto", "version": "7.1.0", "state": "started"}])

    def not_ready(hass):
        raise RuntimeError("HassioNotReadyError")

    monkeypatch.setattr(hassio, "get_host_info", not_ready, raising=False)

    payload = await StateCollector(FakeHass(), pseudonym_key=KEY).async_build_state()

    assert payload["os_version"] == "18.2"
    assert payload["supervisor_version"] == "2026.10.0"
    assert payload["board"] == "rpi5-64"
    assert [a["slug"] for a in payload["addons"]] == ["core_mosquitto"]


# --------------------------------------------------------------------------
# Fifth review: fail-open state drift. The add-on gate must hold on the first
# round, when the Supervisor does not answer, and when the set changes.
# --------------------------------------------------------------------------


def _with_local_update(monkeypatch):
    from homeassistant.helpers import entity_registry as er

    monkeypatch.setattr(er, "async_get", lambda hass: SimpleNamespace(
        entities={}, async_get=lambda entity_id: SimpleNamespace(platform="hassio")))

    hass = household()
    hass.states._states["update.allarme_di_giulia_update"] = state(
        "update.allarme_di_giulia_update", "on", title="Allarme di Giulia",
        installed_version="0.1.0", latest_version="0.2.0", supported_features=0,
        release_url="https://github.com/giulia/allarme",
    )

    return hass


LOCAL = {"slug": "local_allarme_giulia", "name": "Allarme di Giulia", "version": "0.1.0"}


async def test_the_first_round_after_a_start_names_nothing(monkeypatch) -> None:
    _supervised(monkeypatch, [LOCAL])
    hass = _with_local_update(monkeypatch)

    # A brand-new collector: nothing remembered from any earlier round.
    payload = await StateCollector(hass, pseudonym_key=KEY).async_build_state()

    assert "giulia" not in json.dumps(payload).lower()
    assert [row["entity_id"] for row in payload["updates"] if row["kind"] == "addon"] == [GIULIA_UPDATE]


async def test_a_supervisor_that_does_not_answer_fails_closed(monkeypatch) -> None:
    import homeassistant.components.hassio as hassio

    _supervised(monkeypatch, [LOCAL])
    hass = _with_local_update(monkeypatch)
    collector = StateCollector(hass, pseudonym_key=KEY)

    await collector.async_build_state()  # one good round, then silence

    def not_ready(hass):
        raise RuntimeError("HassioNotReadyError")

    monkeypatch.setattr(hassio, "get_apps_list", not_ready, raising=False)
    monkeypatch.setattr(hassio, "get_supervisor_info", not_ready, raising=False)

    payload = await collector.async_build_state()

    assert "giulia" not in json.dumps(payload).lower()

    row = next(row for row in payload["updates"] if row["kind"] == "addon")

    # Nothing confirmed this round, so nothing named: not the title, not the
    # entity id, not last round's local slug either.
    assert row["slug"] is None
    assert row["entity_id"] == GIULIA_UPDATE
    assert row["release_url"] is None


async def test_a_local_add_on_added_between_two_rounds_is_named_by_this_round(monkeypatch) -> None:
    _supervised(monkeypatch, [{"slug": "local_zeta", "name": "Zeta", "version": "1"}])
    hass = _with_local_update(monkeypatch)
    collector = StateCollector(hass, pseudonym_key=KEY)

    first = await collector.async_build_state()

    # Its update entity existed already, but the add-on list did not have it.
    assert "giulia" not in json.dumps(first).lower()

    _supervised(monkeypatch, [{"slug": "local_zeta", "name": "Zeta", "version": "1"}, LOCAL])

    second = await collector.async_build_state()

    assert "giulia" not in json.dumps(second).lower()

    by_version = {addon["version"]: addon["slug"] for addon in second["addons"]}
    assert by_version == {"0.1.0": "local_" + _p("local_allarme_giulia"), "1": "local_" + _p("local_zeta")}

    row = next(row for row in second["updates"] if row["kind"] == "addon")
    assert (row["slug"], row["entity_id"]) == ("local_" + _p("local_allarme_giulia"), GIULIA_UPDATE)

    # Zeta kept the name it had before allarme arrived. An ordinal would have
    # moved it, and a command on its way for zeta would have installed allarme.
    assert {a["slug"] for a in first["addons"]} == {"local_" + _p("local_zeta")}


async def test_an_update_command_naming_the_neutral_id_installs_the_real_entity(monkeypatch) -> None:
    from custom_components.haim_agent.commands import register_supported_commands

    _supervised(monkeypatch, [LOCAL])
    hass = _with_local_update(monkeypatch)
    collector = StateCollector(hass, pseudonym_key=KEY)
    await collector.async_build_state()

    installed: list[list[dict]] = []

    import custom_components.haim_agent.commands as commands

    async def install(hass, report, items):
        installed.append(items)

        return {"started": len(items), "failed": 0}

    monkeypatch.setattr(commands, "async_install_batch", install)

    class Coordinator:
        site_uuid = "s"

        async def async_resolve_update_entity(self, entity_id):
            return collector.resolve_update_entity(entity_id)

        async def async_heartbeat(self):
            return True

    handlers: dict = {}
    dispatcher = SimpleNamespace(register=lambda name, handler, **_options: handlers.__setitem__(name, handler), async_report_item=None)
    register_supported_commands(dispatcher, hass, Coordinator())

    await handlers["update_batch"]({"items": [{"uuid": "u1", "entity_id": GIULIA_UPDATE}]})

    assert installed[0][0]["entity_id"] == "update.allarme_di_giulia_update"


@pytest.mark.parametrize("pending", [True, False])
async def test_a_failed_update_of_a_local_add_on_reports_a_code_not_its_name(monkeypatch, pending) -> None:
    """Sixth review: the neutral id went in, and the failure came back out as
    "update.allarme_di_giulia_update has no update waiting any more."

    End to end, through the real dispatcher and the real install loop: what
    reaches the channel - every report, item and command - is searched.
    """
    from custom_components.haim_agent.command_channel import CommandDispatcher
    from custom_components.haim_agent.commands import register_supported_commands

    _supervised(monkeypatch, [LOCAL])
    hass = _with_local_update(monkeypatch)
    collector = StateCollector(hass, pseudonym_key=KEY)
    await collector.async_build_state()

    if not pending:
        # Installed from the interface in the meantime: "no update pending".
        hass.states._states["update.allarme_di_giulia_update"].state = "off"
    else:
        # Still pending, and Home Assistant's own call blows up with a message
        # naming the entity and a URL.
        async def explode(*args, **kwargs):
            raise RuntimeError("update.allarme_di_giulia_update: cannot reach http://admin:pw@casa-rossi.local")

        hass.services = SimpleNamespace(async_call=explode)

    reports: list[dict] = []

    class Channel:
        async def async_receive(self):
            return None

        async def async_report(self, command_uuid, status, error=None, data=None):
            reports.append({"uuid": command_uuid, "status": status, "error": error, "data": data})

    class Coordinator:
        site_uuid = "s"

        async def async_resolve_update_entity(self, entity_id):
            return collector.resolve_update_entity(entity_id)

        async def async_heartbeat(self):
            return True

    dispatcher = CommandDispatcher(Channel())
    register_supported_commands(dispatcher, hass, Coordinator())

    await dispatcher.async_dispatch({
        "action": "update_batch",
        "command_uuid": "c-1",
        "data": {"items": [{"uuid": "u1", "entity_id": GIULIA_UPDATE, "kind": "addon",
                            "backup": False}]},
    })

    everything = json.dumps(reports)

    assert "giulia" not in everything.lower()
    assert "update.allarme_di_giulia_update" not in everything
    assert "rossi" not in everything.lower()

    item = next(report for report in reports if report["uuid"] == "u1" and report["status"] == "failed")
    assert item["error"] == ("install_failed" if pending else "no_update_pending")


async def test_an_update_command_cannot_ask_about_any_other_entity(monkeypatch) -> None:
    """Independent review (D41): update_batch passed unknown ids through, so the
    panel could name `binary_sensor.porta_ingresso` and learn from the answer
    whether it exists and is on - and install was called on it for real."""
    from custom_components.haim_agent.command_channel import CommandDispatcher
    from custom_components.haim_agent.commands import register_supported_commands

    _supervised(monkeypatch, [LOCAL])
    hass = _with_local_update(monkeypatch)
    hass.states._states["binary_sensor.porta_ingresso"] = state("binary_sensor.porta_ingresso", "on")
    hass.states._states["light.camera_di_giulia"] = state("light.camera_di_giulia", "off")
    hass.states._states["update.never_reported"] = state("update.never_reported", "on")

    collector = StateCollector(hass, pseudonym_key=KEY)
    await collector.async_build_state()

    # Reported in this round, so it is not "never reported" any more: drop it
    # from what was sent, as if it appeared after the last heartbeat.
    collector._entity_aliases.pop("update.never_reported", None)

    reads: list[str] = []
    real_get = hass.states.get
    hass.states.get = lambda entity_id: reads.append(entity_id) or real_get(entity_id)

    calls: list = []

    async def record(*args, **kwargs):
        calls.append(args)

    hass.services = SimpleNamespace(async_call=record)

    reports: list[dict] = []

    class Channel:
        async def async_receive(self):
            return None

        async def async_report(self, command_uuid, status, error=None, data=None):
            reports.append({"uuid": command_uuid, "status": status, "error": error})

    class Coordinator:
        site_uuid = "s"

        async def async_resolve_update_entity(self, entity_id):
            return collector.resolve_update_entity(entity_id)

        async def async_heartbeat(self):
            return True

    dispatcher = CommandDispatcher(Channel())
    register_supported_commands(dispatcher, hass, Coordinator())

    probes = ["binary_sensor.porta_ingresso", "light.camera_di_giulia", "person.giulia_rossi",
              "update.never_reported"]

    await dispatcher.async_dispatch({
        "action": "update_batch",
        "command_uuid": "c-1",
        "data": {"items": [{"uuid": f"u{i}", "entity_id": probe, "backup": True}
                           for i, probe in enumerate(probes)]},
    })

    failed = {r["uuid"]: r["error"] for r in reports if r["uuid"].startswith("u") and r["status"] == "failed"}

    # The same answer for all four: nothing to tell them apart.
    assert failed == {f"u{i}": "not_found" for i in range(len(probes))}
    assert reads == []
    assert calls == []


async def test_the_pseudonym_key_is_made_once_and_kept_in_the_entry() -> None:
    """The wiring no other test sees (D40): a key that changed at every start
    would rename every pseudonym after each restart of Home Assistant."""
    import inspect

    import custom_components.haim_agent as component

    source = inspect.getsource(component.async_setup_entry)

    assert "if not entry.data.get(CONF_PSEUDONYM_KEY):" in source
    assert "pseudonym_key=entry.data.get(CONF_PSEUDONYM_KEY)" in source


async def test_the_pseudonym_key_never_travels(registries) -> None:
    payload = json.dumps(await StateCollector(household(), pseudonym_key=KEY).async_build_state())

    assert KEY not in payload


# --------------------------------------------------------------------------
# Independent review (D41): the gate on update rows was the wrong way round.
# A real id left unless something recognised it as household-named; now it
# leaves only when it is recognised as public software.
# --------------------------------------------------------------------------


def _platforms(monkeypatch, platforms):
    from homeassistant.helpers import entity_registry as er

    monkeypatch.setattr(er, "async_get", lambda hass: SimpleNamespace(
        entities={}, async_get=lambda entity_id: SimpleNamespace(platform=platforms[entity_id])
        if entity_id in platforms else None))


async def test_an_unreadable_registry_does_not_let_a_local_add_on_s_name_out(monkeypatch) -> None:
    _supervised(monkeypatch, [LOCAL])
    _platforms(monkeypatch, {})
    hass = household()
    hass.states._states["update.allarme_di_giulia_update"] = state(
        "update.allarme_di_giulia_update", "on", title="Allarme di Giulia",
        installed_version="0.1.0", latest_version="0.2.0", supported_features=0)

    payload = await StateCollector(hass, pseudonym_key=KEY).async_build_state()

    assert "giulia" not in json.dumps(payload).lower()


async def test_a_device_update_without_the_firmware_class_is_still_not_named(monkeypatch) -> None:
    _platforms(monkeypatch, {"update.nas_di_giulia_dsm_update": "synology_dsm"})
    hass = household()
    hass.states._states["update.nas_di_giulia_dsm_update"] = state(
        "update.nas_di_giulia_dsm_update", "on", installed_version="7.2", latest_version="7.3",
        supported_features=0, title="Synology DSM", release_url="https://nas-di-giulia.local/notes")

    collector = StateCollector(hass, pseudonym_key=KEY)
    payload = await collector.async_build_state()

    assert "giulia" not in json.dumps(payload).lower()

    row = next(row for row in payload["updates"] if row["kind"] == "integration")
    assert row["entity_id"] == "update.haim_integration_" + _p("update.nas_di_giulia_dsm_update")
    assert collector.resolve_update_entity(row["entity_id"]) == "update.nas_di_giulia_dsm_update"


async def test_the_house_name_passes_the_same_gate_in_every_field(monkeypatch) -> None:
    from homeassistant.helpers import issue_registry as ir

    _platforms(monkeypatch, {"update.casa_rossi_clima_update": "hacs", "update.frigate_update": "hacs"})
    monkeypatch.setattr(ir, "async_get", lambda hass: SimpleNamespace(issues={
        1: SimpleNamespace(domain="casa_rossi_clima", issue_id="x", severity="warning", dismissed_version=None)}))

    hass = household()
    hass.config_entries._entries.append(SimpleNamespace(domain="casa_rossi_clima", state=None, title="Clima"))
    hass.states._states["update.casa_rossi_clima_update"] = state(
        "update.casa_rossi_clima_update", "on", installed_version="1.0", latest_version="1.1",
        supported_features=0, release_url="https://github.com/casa-rossi/casa_rossi_clima/releases")
    hass.states._states["update.frigate_update"] = state(
        "update.frigate_update", "on", installed_version="5.0", latest_version="5.1",
        supported_features=0, release_url="https://github.com/blakeblackshear/frigate-hass-integration/releases")

    payload = await StateCollector(hass, pseudonym_key=KEY).async_build_state()

    for field in ("integrations", "repairs", "updates"):
        assert "rossi" not in json.dumps(payload[field]).lower(), field

    # Every HACS repository travels under a pseudonym, a public one too: an
    # allow-list of what we can vouch for, not a list of names we know (D41).
    assert {row["entity_id"] for row in payload["updates"] if row["kind"] == "integration"} == {
        "update.haim_integration_" + _p("update.casa_rossi_clima_update"),
        "update.haim_integration_" + _p("update.frigate_update"),
    }
    assert all(row["release_url"] is None for row in payload["updates"] if row["kind"] == "integration")

    # And a custom integration's domain is a pseudonym wherever it appears.
    custom = "custom_" + _p("casa_rossi_clima")
    assert custom in {row["domain"] for row in payload["integrations"]}
    assert custom in {row["domain"] for row in payload["repairs"]}


@pytest.mark.parametrize("slug", ["a1b2c3d4_allarme_giulia", "local_allarme_giulia"])
async def test_an_add_on_from_a_repository_the_family_added_is_not_named(monkeypatch, slug) -> None:
    _supervised(monkeypatch, [
        {"slug": slug, "name": "Allarme di Giulia", "version": "0.1"},
        {"slug": "a0d7b954_vscode", "name": "Studio Code Server", "version": "5.0"},
    ])
    _platforms(monkeypatch, {"update.allarme_di_giulia_update": "hassio"})
    hass = household()
    hass.states._states["update.allarme_di_giulia_update"] = state(
        "update.allarme_di_giulia_update", "on", title="Allarme di Giulia",
        installed_version="0.1", latest_version="0.2", supported_features=0)

    payload = await StateCollector(hass, pseudonym_key=KEY).async_build_state()

    assert "giulia" not in json.dumps(payload).lower()

    # Community add-ons are public software, and keep their names.
    assert {"slug": "a0d7b954_vscode", "name": "Studio Code Server"}.items() <= next(
        addon for addon in payload["addons"] if addon["slug"] == "a0d7b954_vscode").items()


async def test_by_default_no_line_of_the_log_leaves(registries, private_address) -> None:
    """D41: the first line of an error carried names - "Sveglia di Giulia:
    Error executing script" - and that was the default for every house."""
    hass = household()
    hass.data["system_log"] = SimpleNamespace(records={
        1: SimpleNamespace(level="ERROR", source=("homeassistant/helpers/script.py", 1),
                           name="homeassistant.components.automation",
                           message=["Sveglia di Giulia: Error executing script"], timestamp=1.0, count=1),
    })

    volatile = await StateCollector(hass, pseudonym_key=KEY).async_build_volatile()

    assert volatile["logs"]["level"] == "counts"
    assert volatile["logs"]["errors"] == 1
    assert "giulia" not in json.dumps(volatile).lower()


async def test_names_the_family_gave_are_taken_out_of_the_log_lines_they_chose_to_send(
    registries, private_address
) -> None:
    hass = household()
    hass.states._states["automation.sveglia"] = state("automation.sveglia", "on", friendly_name="Sveglia mattutina")
    hass.states._states["person.nonna"] = state("person.nonna", "home", friendly_name="Nonna Pina")
    hass.data["system_log"] = SimpleNamespace(records={
        1: SimpleNamespace(level="ERROR", source=("homeassistant/helpers/script.py", 1),
                           name="homeassistant.components.automation",
                           message=["Sveglia mattutina: Error executing script"], timestamp=1.0, count=1),
        2: SimpleNamespace(level="ERROR", source=("x.py", 1), name="custom_components.presence",
                           message=["Nonna Pina left Camera di Giulia"], timestamp=2.0, count=1),
    })

    volatile = await StateCollector(hass, LOG_LEVEL_FULL, pseudonym_key=KEY).async_build_volatile()
    everything = json.dumps(volatile).lower()

    for name in ("sveglia mattutina", "nonna pina", "giulia"):
        assert name not in everything, name

    # Still a line an installer can read.
    assert "error executing script" in everything
