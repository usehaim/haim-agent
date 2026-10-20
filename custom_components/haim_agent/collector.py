"""Builds the payload: what this instance is, and how it is doing.

Two blocks, and the split between them is the single most important thing in
this file.

    state     stable. Enters the hash. Changes only when something real changes.
    volatile  CPU, memory, disk, uptime, the local address, log counts. Changes
              on every single send, and therefore stays OUT of the hash.

If one volatile field ever finds its way into `state`, the hash differs on every
send, the cloud's "nothing changed" branch never fires, and ninety per cent of
its write saving evaporates. It is a mistake with no symptom at all until there
are a thousand instances, which is why there is a test on both sides pinning it.

EVERY field is collected in a try block of its own. An undocumented Home
Assistant API that changes in a release must turn one field null - never take
the whole heartbeat down with it. That is the number one reason components like
this stop working, and half the APIs below are not documented as public.

And the boundary, which is the product: counts, versions, system metrics,
integration domains and add-on slugs. Never an entity id, a device name, a room,
an area, a state, a history, an energy figure, an image, or a person.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import logging
import secrets
from typing import Any, Callable, TypeVar

from homeassistant.core import HomeAssistant

from . import bounds
from .const import CAPABILITY_SUPERVISOR, DEFAULT_LOG_LEVEL
from .metrics import async_collect_metrics, async_uptime_seconds
from .redaction import redact, summarise
from .system import async_capabilities, async_describe_instance

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

# The fields of `state` that the contract says are arrays. They leave as JSON
# arrays whatever happened on the way: a helper that starts returning a tuple,
# a set or a dict must not turn `[]` into `{}` on the far side, and a set would
# not even serialise - the whole heartbeat would go with it.
_LIST_FIELDS = ("capabilities", "integrations", "addons", "updates", "repairs")

# What the Supervisor may say about an add-on, and what the contract accepts.
# Anything else - `unknown`, a state added next year - travels as null rather
# than as a value the schema rejects.
_ADDON_STATES = frozenset({"started", "stopped", "error"})


def _safe(what: str, gather: Callable[[], T], default: T) -> T:
    """One field, one try block. This function is the rule made mechanical."""
    try:
        return gather()
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not collect %s", what, exc_info=True)

        return default


class StateCollector:
    """Assembles one telemetry payload."""

    def __init__(
        self,
        hass: HomeAssistant,
        log_level: str = DEFAULT_LOG_LEVEL,
        pseudonym_key: str | None = None,
    ) -> None:
        self._hass = hass
        self._log_level = log_level
        # The key of every pseudonym this house sends (D40). Kept in the config
        # entry and never sent; without one, a key for this process only - the
        # pseudonyms then change at the next start, which is safe: a command
        # naming an old one finds nothing and installs nothing.
        self._pseudonym_key = (pseudonym_key or secrets.token_hex(32)).encode()
        # Local add-ons of this house: raw slug -> what travels, and their
        # names -> the same. Rebuilt with every supervisor block.
        self._local_slugs: dict[str, str] = {}
        self._local_names: dict[str, str] = {}
        # Names of PUBLISHED add-ons seen this round; None when the add-on list
        # could not be read this round - and then nothing is confirmed.
        self._published_names: set[str] | None = None
        # What travels in place of an add-on update's real entity id -> the
        # real one, so an update command naming the neutral id still installs.
        # Every entity id sent in the last `updates` -> the real one. None
        # until a round has run. A command may name only these (D41).
        self._entity_aliases: dict[str, str] | None = None

    def with_log_level(self, log_level: str) -> None:
        self._log_level = log_level

    async def async_build_state(self) -> dict[str, Any]:
        """The hashed half: what this instance IS."""
        hass = self._hass
        description = await async_describe_instance(hass)
        capabilities = description["capabilities"]

        # First, because it decides how the add-ons built in this house are
        # called EVERYWHERE they appear - in `addons` and in `updates` alike
        # (fourth review: the same name must pass the same gate in every field
        # that carries it).
        supervisor = await self._async_supervisor_block(capabilities)

        state: dict[str, Any] = {
            "ha_version": description["ha_version"],
            "agent_version": description["agent_version"],
            "install_type": description["install_type"],
            "python_version": description["python_version"],
            "timezone": _safe("timezone", lambda: bounds.text(hass.config.time_zone, bounds.SHORT_TEXT_MAX), None),
            "capabilities": capabilities,
            "counts": self._counts(),
            "integrations": self._integrations(),
            "problems": self._problems(),
            "repairs": self._repairs(),
            "updates": self._updates(),
            "backup": self._backup(),
        }

        state.update(supervisor)

        for field in _LIST_FIELDS:
            state[field] = _as_list(state.get(field))

        return state

    async def async_build_volatile(self) -> dict[str, Any]:
        """The unhashed half: what this instance is DOING, right now."""
        return {
            "uptime_seconds": await async_uptime_seconds(self._hass),
            "resources": await async_collect_metrics(self._hass),
            "ip_local": await self._async_local_address(),
            "logs": self._logs(),
        }

    # ------------------------------------------------------------------ counts

    def _counts(self) -> dict[str, int]:
        """Counts only. Never a name, never an id."""
        hass = self._hass

        return {
            "entities": _safe("entity count", lambda: len(self._entity_registry().entities), 0),
            "devices": _safe("device count", lambda: len(self._device_registry().devices), 0),
            "automations": _safe(
                "automation count", lambda: len(hass.states.async_entity_ids("automation")), 0
            ),
            "scripts": _safe("script count", lambda: len(hass.states.async_entity_ids("script")), 0),
            "dashboards": _safe("dashboard count", self._dashboard_count, 0),
            "areas": _safe("area count", lambda: len(self._area_registry().areas), 0),
        }

    def _dashboard_count(self) -> int:
        """Lovelace keeps its dashboards in hass.data, which is internal.

        Wrapped like everything else: a rename here costs one count, not the
        heartbeat.
        """
        lovelace = self._hass.data.get("lovelace")
        dashboards = getattr(lovelace, "dashboards", None)

        if isinstance(dashboards, dict):
            return len(dashboards)

        if isinstance(lovelace, dict) and isinstance(lovelace.get("dashboards"), dict):
            return len(lovelace["dashboards"])

        return 0

    # ------------------------------------------------------------ integrations

    def _integrations(self) -> list[dict[str, Any]]:
        """Domains and load state. A domain says which protocol, not whose house.

        `zha` tells an installer there is Zigbee here. It says nothing about the
        family, which is exactly why it is allowed to travel.
        """

        def gather() -> list[dict[str, Any]]:
            from homeassistant.config_entries import ConfigEntryState

            states = {
                ConfigEntryState.LOADED: "loaded",
                ConfigEntryState.SETUP_ERROR: "setup_error",
                ConfigEntryState.SETUP_RETRY: "setup_retry",
                ConfigEntryState.MIGRATION_ERROR: "migration_error",
                ConfigEntryState.NOT_LOADED: "not_loaded",
                ConfigEntryState.FAILED_UNLOAD: "setup_error",
            }

            # Home Assistant has two states that mean "ask again in a moment":
            # an entry being set up and one being unloaded. Neither is a fact
            # about the instance, and reporting them - as this did, by falling
            # through to `not_loaded` - tells an installer an integration is
            # broken when it is starting.
            #
            # Found on a real instance: this component reported ITSELF as
            # not_loaded, in the very payload it had just sent.
            transient = {ConfigEntryState.SETUP_IN_PROGRESS, ConfigEntryState.UNLOAD_IN_PROGRESS}

            seen: dict[str, dict[str, Any]] = {}

            for entry in self._hass.config_entries.async_entries():
                if entry.state in transient:
                    # Left out of this heartbeat entirely. The next one, five
                    # minutes later, carries the truth - and a domain missing
                    # for one heartbeat says less wrong than a domain reported
                    # as broken.
                    continue

                status = states.get(entry.state, "not_loaded")
                existing = seen.get(entry.domain)

                # One row per domain, and a broken entry wins over a working
                # one: "mqtt is in trouble" is the useful statement even when
                # three other mqtt entries are fine.
                if existing is None or (existing["status"] == "loaded" and status != "loaded"):
                    seen[entry.domain] = {
                        # A custom integration's domain is whatever its author
                        # called it, and the author can be the household.
                        "domain": self._public_domain(entry.domain),
                        "status": status,
                        "version": None,
                        "source": self._integration_source(entry.domain),
                    }

            return sorted(seen.values(), key=lambda row: row["domain"])

        return _safe("integrations", gather, [])

    def _integration_source(self, domain: str) -> str:
        """core or custom - and nothing that identifies the household."""
        return "core" if _is_built_in(domain) else "custom"

    def _public_domain(self, domain: str) -> str:
        """A domain as it is if Home Assistant ships it; a pseudonym otherwise.

        An allow-list, not a filter of names we know (D41, eighth review): a
        custom integration is called whatever its author liked, the author can
        be the family, and a name we do not know is not a name that is safe.
        """
        if _is_built_in(domain) or domain in _PUBLIC_CUSTOM_DOMAINS:
            return domain

        return "custom_" + self._pseudonym(domain)

    # ---------------------------------------------------------------- problems

    def _problems(self) -> dict[str, int]:
        return {
            "unavailable_entities": _safe("unavailable entities", self._unavailable, 0),
            "config_entry_errors": _safe("config entry errors", self._config_entry_errors, 0),
            "repair_issues": _safe("repair issues", self._repair_issues, 0),
            "repair_issues_critical": _safe("critical repair issues", self._repair_issues_critical, 0),
        }

    def _unavailable(self) -> int:
        """How many entities are unavailable. The count, never which ones.

        A real instance sits at a hundred and forty of these on a good day - a
        gateway that is off, a device out of range - so this is a number to
        watch the shape of, not a fault in itself.
        """
        from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

        return sum(
            1
            for state in self._hass.states.async_all()
            if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        )

    def _config_entry_errors(self) -> int:
        from homeassistant.config_entries import ConfigEntryState

        broken = (ConfigEntryState.SETUP_ERROR, ConfigEntryState.SETUP_RETRY, ConfigEntryState.MIGRATION_ERROR)

        return sum(1 for entry in self._hass.config_entries.async_entries() if entry.state in broken)

    def _repair_issues(self) -> int:
        from homeassistant.helpers import issue_registry as ir

        return sum(1 for issue in ir.async_get(self._hass).issues.values() if not issue.dismissed_version)

    def _repair_issues_critical(self) -> int:
        from homeassistant.helpers import issue_registry as ir

        return sum(
            1
            for issue in ir.async_get(self._hass).issues.values()
            if not issue.dismissed_version and str(getattr(issue, "severity", "")).endswith("critical")
        )

    def _repairs(self) -> list[dict[str, Any]]:
        """How many repair issues each integration has raised. Contract 1.2.0.

        THE DOMAIN AND NOTHING ELSE, and the line is worth stating because it is
        fine enough to trip over. A domain - `shelly`, `zwave_js`, `hacs` -
        describes the instance: it turns "44 problemi" into "44 problemi, per lo
        piu' shelly", which is the difference between a number and something an
        installer can act on.

        What must NEVER travel from here:
          - `issue_id`, which integrations routinely build out of an entity id
            or a device name;
          - `translation_placeholders`, which is where the names actually live;
          - `translation_key`, which is usually generic but is not guaranteed to
            be, and we do not send fields we cannot promise.

        Sorted by domain, always. `state` is hashed, list order is part of the
        hash, and a dict iteration order that shifted would make every heartbeat
        look like a change (rule 12).
        """

        def gather() -> list[dict[str, Any]]:
            from homeassistant.helpers import issue_registry as ir

            counts: dict[str, dict[str, int]] = {}

            for issue in ir.async_get(self._hass).issues.values():
                if issue.dismissed_version:
                    continue

                domain = getattr(issue, "domain", None)

                if not isinstance(domain, str) or not domain:
                    continue

                row = counts.setdefault(domain, {"count": 0, "critical": 0})
                row["count"] += 1

                if str(getattr(issue, "severity", "")).endswith("critical"):
                    row["critical"] += 1

            return [
                {"domain": self._public_domain(domain), "count": row["count"], "critical": row["critical"]}
                for domain, row in sorted(counts.items())
            ]

        return _safe("repairs", gather, [])

    # ----------------------------------------------------------------- updates

    def _updates(self) -> list[dict[str, Any]]:
        """Read from update entities, with the feature flags read from the enum.

        supported_features is read from Home Assistant's own enum and never
        hardcoded as an integer. Those numbers have been renumbered before, and
        a hardcoded 3 that silently becomes something else means offering an
        installer a backup that will not be taken.
        """

        def gather() -> list[dict[str, Any]]:
            from homeassistant.components.update import UpdateEntityFeature

            updates: list[dict[str, Any]] = []
            aliases: dict[str, str] = {}

            for entity_id in self._hass.states.async_entity_ids("update"):
                state = self._hass.states.get(entity_id)

                if state is None:
                    continue

                features = state.attributes.get("supported_features") or 0
                platform = self._update_platform(entity_id)
                kind = self._update_kind(entity_id, state.attributes, platform)
                title = _string(state.attributes.get("title"))
                release_url = _string(state.attributes.get("release_url"))

                row = {
                        "kind": kind,
                        # The one entity id that travels. For Core, OS,
                        # Supervisor, published add-ons and HACS it names
                        # software; device firmware and unconfirmed add-ons
                        # get a pseudonym below (D40).
                        "entity_id": bounds.text(entity_id),
                        "slug": self._update_slug(title) if kind == "addon" else None,
                        # 40: the width of update_commands.from_version and
                        # to_version, which is where these end up.
                        "installed": bounds.version(_string(state.attributes.get("installed_version")), bounds.UPDATE_VERSION_MAX),
                        "latest": bounds.version(_string(state.attributes.get("latest_version")), bounds.UPDATE_VERSION_MAX),
                        "available": state.state == "on",
                        "supports_specific_version": bool(features & UpdateEntityFeature.SPECIFIC_VERSION),
                        "supports_backup": bool(features & UpdateEntityFeature.BACKUP),
                        # A firmware page can live on the device itself, at
                        # its address on the house network.
                        "release_url": bounds.text(redact(release_url, self._known_names()), bounds.URL_MAX)
                        if release_url else None,
                    }

                if not self._names_public_software(entity_id, kind, platform, title):
                    # Fail closed (fifth review, independent review D41). An
                    # update entity is named after what it updates, and in a
                    # house that is often something the family named: the
                    # device (`update.camera_di_giulia_firmware`), a local
                    # add-on, a custom integration. The real id leaves only
                    # when it is confirmed to name public software; every
                    # other row - including those whose platform could not be
                    # read this round - travels under a keyed pseudonym (D40).
                    if kind == "addon":
                        row["slug"] = self._local_names.get(title or "")

                    row["release_url"] = None
                    row["entity_id"] = f"update.haim_{kind}_{self._pseudonym(entity_id)}"

                aliases[row["entity_id"]] = entity_id
                updates.append(row)

            self._entity_aliases = aliases

            return updates

        return _safe("updates", gather, [])

    def _update_platform(self, entity_id: str) -> str | None:
        return _safe("update platform", lambda: self._entity_registry().async_get(entity_id).platform, None)

    def _names_public_software(self, entity_id: str, kind: str, platform: str | None, title: str | None) -> bool:
        """Whether this update entity's id may leave the house as it is."""
        if kind in ("core", "os", "supervisor"):
            return True

        if kind == "addon":
            return self._is_published(title)

        # Everything else - HACS repositories included - is named by somebody
        # we cannot vouch for, and a name we do not recognise as the family's
        # is not thereby safe (eighth review): a pseudonym.
        return False

    def _update_kind(self, entity_id: str, attributes: dict[str, Any], platform: str | None = None) -> str:
        """Which kind of software an update entity installs.

        By the platform that created it, read from the entity registry. The old
        test - "it has an `in_progress` attribute, so it is an add-on" - was
        true of EVERY update entity: Home Assistant writes `in_progress` on all
        of them, so every integration update was reported as an add-on (found
        by session 04 against real Home Assistant 2026.8 to 2026.10).
        """
        known = {
            "update.home_assistant_core_update": "core",
            "update.home_assistant_operating_system_update": "os",
            "update.home_assistant_supervisor_update": "supervisor",
        }

        if entity_id in known:
            return known[entity_id]

        if platform is None:
            platform = self._update_platform(entity_id)

        if platform == "hassio":
            # Core, OS and Supervisor are hassio entities too, and were
            # matched above by their fixed ids. What is left is an add-on.
            return "addon"

        if attributes.get("device_class") == "firmware":
            return "device"

        return "integration"

    # ------------------------------------------------------------------ backup

    def _backup(self) -> dict[str, Any]:
        """The four backup manager sensors, plus the event entity.

        These are readable with a non-admin token and WITHOUT a Supervisor,
        which is the whole reason we use them: the competing product does not
        report backups at all, and on a Container install there is no other way
        to know. The event entity carries the failure reason.
        """

        def gather() -> dict[str, Any]:
            backup: dict[str, Any] = {
                "last_success_at": bounds.text(self._sensor("sensor.backup_last_successful_automatic_backup"), 40),
                "last_attempt_at": bounds.text(self._sensor("sensor.backup_last_attempted_automatic_backup"), 40),
                "next_scheduled_at": bounds.text(self._sensor("sensor.backup_next_scheduled_automatic_backup"), 40),
                "state": self._backup_state(),
                "last_failure_reason": self._backup_failure_reason(),
                "agents_configured": self._backup_agents(),
            }

            return backup

        return _safe("backup", gather, {})

    def _sensor(self, entity_id: str) -> str | None:
        state = self._hass.states.get(entity_id)

        if state is None or state.state in ("unknown", "unavailable", ""):
            return None

        return state.state

    def _backup_state(self) -> str | None:
        """What the backup manager is doing, and only what it actually says.

        `blocked` deliberately maps to nothing. It means backups are temporarily
        not possible - during startup, during an update - and it is NOT a
        failure. Reading it as one fires a "backup failed" alert at every
        restart of an instance that has never taken a backup in its life, which
        is exactly what happened the first time this ran against the laboratory
        and is the reason this comment exists.

        A failure is only a failure when the event entity says so.
        """
        state = self._hass.states.get("sensor.backup_backup_manager_state")

        if state is None:
            return None

        if self._backup_failure_reason() is not None:
            return "failed"

        return {
            "idle": "idle",
            "create_backup": "in_progress",
            "receive_backup": "in_progress",
        }.get(state.state)

    def _backup_failure_reason(self) -> str | None:
        """From the event entity, which carries why the last one failed."""
        state = self._hass.states.get("event.backup_automatic_backup")

        if state is None:
            return None

        reason = state.attributes.get("failed_reason") or state.attributes.get("reason")

        if not reason:
            return None

        # Free text from whichever backup agent failed, and agents name where
        # they write: `smb://nas-rossi/backup`, a cloud account's email. It is
        # filtered like a log line, because that is what it is.
        return redact(str(reason), self._known_names())[:200] or None

    def _backup_agents(self) -> int | None:
        """How many places the automatic backup is written to. A count only.

        The manager-state sensor has no `agents` attribute and never had one:
        this was null on every real instance (session 04, HA 2026.8 to
        2026.10). The number lives in the backup manager's configuration, which
        is internal - hence its own try block, and the old attribute kept as a
        fallback in case a release ever exposes it.
        """

        def from_manager() -> int | None:
            manager = self._hass.data.get("backup")
            agent_ids = manager.config.data.create_backup.agent_ids

            return len(agent_ids) if isinstance(agent_ids, (list, tuple, set)) else None

        configured = _safe("backup agents", from_manager, None)

        if configured is not None:
            return configured

        state = self._hass.states.get("sensor.backup_backup_manager_state")
        agents = state.attributes.get("agents") if state is not None else None

        return len(agents) if isinstance(agents, (list, dict)) else None

    # -------------------------------------------------------------- supervisor

    async def _async_supervisor_block(self, capabilities: list[str]) -> dict[str, Any]:
        """Versions and add-ons, only where a Supervisor exists.

        On Container and Core these come back null and empty, and the cloud
        degrades what it offers. Refusing the instance would be the wrong answer
        to a smaller instance.
        """
        empty: dict[str, Any] = {
            "supervisor_version": None,
            "os_version": None,
            "board": None,
            "addons": [],
        }

        # Forgotten at the start of every round: a map left over from the last
        # one would name this round's add-ons with last round's knowledge.
        self._local_slugs = {}
        self._local_names = {}
        self._published_names = None

        if CAPABILITY_SUPERVISOR not in capabilities:
            return empty

        # One try per source, not one for the block (rule 9, found by session
        # 04): a Supervisor that has not answered the host query yet used to
        # wipe the add-on list and both versions with it.
        def hassio(name: str) -> Any:
            import homeassistant.components.hassio as module

            return getattr(module, name)

        host = _safe("host info", lambda: hassio("get_host_info")(self._hass) or {}, {})
        os_info = _safe("os info", lambda: hassio("get_os_info")(self._hass) or {}, {})
        supervisor = _safe("supervisor info", lambda: hassio("get_supervisor_info")(self._hass) or {}, {})

        return {
            "supervisor_version": _safe("supervisor version", lambda: bounds.version(_string(supervisor.get("version"))), None),
            "os_version": _safe("os version", lambda: bounds.version(_string(os_info.get("version"))), None),
            "board": _safe(
                "board",
                lambda: bounds.text(_string(os_info.get("board")) or _string(host.get("chassis")), bounds.SHORT_TEXT_MAX),
                None,
            ),
            "addons": _safe("addons", lambda: self._addons(hassio, supervisor), []),
        }

    def _addons(self, hassio: Callable[[str], Any], supervisor: dict[str, Any]) -> list[dict[str, Any]]:
        """`get_apps_list` from 2026.4; the list inside supervisor info before.

        The old place is deprecated since 2026.4.0 and will go; the new one does
        not exist on older releases. Whichever answers, in that order.
        """
        try:
            listed = hassio("get_apps_list")(self._hass)
        except Exception:  # noqa: BLE001 - older than 2026.4, or not ready yet
            listed = supervisor.get("addons")

        addons = [addon for addon in _as_list(listed) if isinstance(addon, dict) and addon.get("slug")]

        # Local add-ons travel as `local_` plus a keyed pseudonym (D40). Not a
        # bare digest: `local_allarme_giulia` hashed without a key is reversed
        # by anyone who guesses the name (fourth review). Not an ordinal
        # either: an ordinal moves when another add-on is added, and then the
        # same name points at a different add-on.
        local = sorted(str(addon["slug"]) for addon in addons if _is_local(addon.get("slug")))
        self._local_slugs = {
            slug: ("local_" if slug.startswith("local_") else "custom_") + self._pseudonym(slug) for slug in local
        }
        self._local_names = {
            str(addon["name"]): self._local_slugs[str(addon["slug"])]
            for addon in addons
            if _is_local(addon.get("slug")) and isinstance(addon.get("name"), str)
        }
        self._published_names = {
            str(addon["name"])
            for addon in addons
            if not _is_local(addon.get("slug")) and isinstance(addon.get("name"), str)
        }

        return [
            {
                # Slugs, not contents. `core_mosquitto` says an MQTT broker
                # runs here and nothing else - but see _addon_slug.
                "slug": self._addon_slug(str(addon.get("slug", ""))),
                "name": None if _is_local(addon.get("slug")) else self._chosen_name(_string(addon.get("name"))),
                "version": bounds.version(addon.get("version", ""), bounds.UPDATE_VERSION_MAX) or "",
                "update_available": bool(addon.get("update_available", False)),
                "state": addon.get("state") if addon.get("state") in _ADDON_STATES else None,
            }
            for addon in addons
        ]

    def _addon_slug(self, slug: str) -> str:
        """A published add-on's slug as it is; a local one, never.

        `local_*` add-ons are built in this house from a folder somebody made,
        and they are named however that person liked: `local_allarme_giulia`.
        A published slug names software anyone can install; a local one can
        name a person.
        """
        if _is_local(slug):
            return self._local_slugs.get(slug, "local_" + self._pseudonym(slug))

        return self._chosen_name(slug) or ""

    def _update_slug(self, title: str | None) -> str | None:
        """The `slug` of an add-on's update row, which is the add-on's NAME.

        Only for a published add-on confirmed this round; every other case is
        handled, fail closed, where the row is built.
        """
        return self._chosen_name(title) if self._is_published(title) else None

    def _is_published(self, title: str | None) -> bool:
        return title is not None and self._published_names is not None and title in self._published_names

    def _pseudonym(self, value: str) -> str:
        """Stable in this house, meaningless and irreversible outside it."""
        return hmac.new(self._pseudonym_key, value.encode(), hashlib.sha256).hexdigest()[:12]

    @property
    def updates_known(self) -> bool:
        return self._entity_aliases is not None

    def resolve_update_entity(self, entity_id: Any) -> str | None:
        """The real entity id behind one sent in `updates`, or None.

        Only ids this house sent. Passing anything else through let the panel
        name any entity - `binary_sensor.porta_ingresso` - and learn from the
        answer whether it exists and whether it is on (independent review).
        """
        if not isinstance(entity_id, str) or not self._entity_aliases:
            return None

        return self._entity_aliases.get(entity_id)

    def _chosen_name(self, value: str | None) -> str | None:
        """A name somebody may have chosen, minus what identifies this house."""
        if value is None:
            return None

        known = self._known_names()

        return bounds.text(redact(value, known) if known else value, bounds.SHORT_TEXT_MAX)

    # -------------------------------------------------------------------- logs

    def _logs(self) -> dict[str, Any]:
        """Counts always; messages only if the household chose to send them."""

        def gather() -> dict[str, Any]:
            handler = self._hass.data.get("system_log")
            records = list(getattr(handler, "records", {}).values()) if handler else []

            errors = sum(1 for record in records if _level(record) in ("ERROR", "CRITICAL"))
            warnings = sum(1 for record in records if _level(record) == "WARNING")

            entries = [
                {
                    "level": "critical" if _level(record) == "CRITICAL" else "error",
                    "source": _source(record),
                    "message": _message(record),
                    "at": _timestamp(record),
                    "count": int(getattr(record, "count", 1) or 1),
                }
                for record in records
                if _level(record) in ("ERROR", "CRITICAL")
            ]

            return summarise(entries, self._log_level, errors, warnings, self._known_names())

        return _safe("logs", gather, summarise([], self._log_level, 0, 0))

    def _known_names(self) -> tuple[str, ...]:
        """What this instance knows identifies it, so the filter can remove it.

        The name the household gave the instance - very often their surname -
        and the hostnames of its own internal and external addresses. Read
        only to be taken OUT of free text; none of it is ever sent.
        """
        from urllib.parse import urlsplit

        config = self._hass.config
        names: list[str] = []

        location = _safe("location name", lambda: getattr(config, "location_name", None), None)

        if isinstance(location, str):
            names.append(location)

        for attribute in ("external_url", "internal_url"):
            url = _safe(attribute, lambda attribute=attribute: getattr(config, attribute, None), None)

            if isinstance(url, str) and url:
                host = _safe("hostname", lambda url=url: urlsplit(url).hostname, None)

                if host:
                    names.append(host)
                    # `casa-rossi` alone, too: a log line can name the host
                    # without its domain.
                    names.append(host.split(".", 1)[0])

        names.extend(self._household_names())

        return tuple(names)

    def _household_names(self) -> list[str]:
        """Every name the family gave something, as Home Assistant knows them.

        Areas, devices, people, zones, and the aliases of automations
        and scripts - which Home Assistant puts in front of their own error
        messages ("Sveglia di Giulia: Error executing script"). Read only to be
        taken out of free text (D41). Not every entity's friendly name: there
        are thousands, most are generated, and the ones the family chose are
        mostly the device's and the area's anyway. Each source on its own
        (rule 9), and cached for a minute: this runs for every field.
        """
        import time

        now = time.monotonic()
        cached = getattr(self, "_household_cache", None)

        if cached is not None and now - cached[0] < 60:
            return cached[1]

        hass = self._hass
        names: list[str] = []

        def areas() -> list[Any]:
            from homeassistant.helpers import area_registry as ar

            return [area.name for area in ar.async_get(hass).areas.values()]

        def devices() -> list[Any]:
            return [
                name
                for device in _device_entries(self._device_registry())
                for name in (getattr(device, "name_by_user", None), getattr(device, "name", None))
            ]

        def friendly(domain: str) -> Callable[[], list[Any]]:
            def read() -> list[Any]:
                return [
                    state.attributes.get("friendly_name")
                    for entity_id in hass.states.async_entity_ids(domain)
                    if (state := hass.states.get(entity_id)) is not None
                ]

            return read

        sources: dict[str, Callable[[], list[Any]]] = {
            "area names": areas,
            "device names": devices,
            "person names": friendly("person"),
            "zone names": friendly("zone"),
            "automation aliases": friendly("automation"),
            "script aliases": friendly("script"),
        }

        for label, source in sources.items():
            for name in _safe(label, source, []):
                # Four letters at least: a shorter name ("Tv", "Box") is a
                # word that would be cut out of half of every log line.
                if isinstance(name, str) and len(name.strip()) >= 4:
                    names.append(name.strip())

        result = sorted(set(names))
        self._household_cache = (now, result)

        return result

    # --------------------------------------------------------------- addresses

    async def _async_local_address(self) -> str | None:
        """The instance's own address on the house network, and only that.

        Asked of Home Assistant's network component rather than probed with a
        raw socket: it is the supported way, it does not block the event loop,
        and it already knows about the adapter the user configured.

        A private address, never a public one, and never a network name or a
        MAC. It is here because an installer standing at a customer's router
        needs to know which box to look at.
        """
        try:
            from homeassistant.components.network import async_get_source_ip

            address = await async_get_source_ip(self._hass)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not determine the local address", exc_info=True)

            return None

        return address if isinstance(address, str) and _is_private(address) else None

    # ---------------------------------------------------------------- registries

    def _entity_registry(self):
        from homeassistant.helpers import entity_registry as er

        return er.async_get(self._hass)

    def _device_registry(self):
        from homeassistant.helpers import device_registry as dr

        return dr.async_get(self._hass)

    def _area_registry(self):
        from homeassistant.helpers import area_registry as ar

        return ar.async_get(self._hass)


def _string(value: Any) -> str | None:
    return str(value) if value not in (None, "", "unknown", "unavailable") else None


# Add-on repositories whose slugs name public software (D41): the official
# store and a few well-known public repositories, by the prefix the Supervisor
# derives from the repository URL. Anything else - `local_*`, or a repository
# the family added from somebody's GitHub - is named in the house and travels
# under a pseudonym. A prefix missing here costs a name on the panel, never a
# name out of the house.
_PUBLIC_ADDON_REPOSITORIES = (
    "core_",      # official add-on store
    "a0d7b954_",  # Home Assistant Community Add-ons
    "5c53de3b_",  # ESPHome
    "45df7312_",  # Zigbee2MQTT
    "ccab4aaf_",  # Frigate
    "d5369777_",  # Music Assistant
)


# Custom integrations that are public software under a fixed name, and that
# an installer needs to see by name. Kept short on purpose (D41).
_PUBLIC_CUSTOM_DOMAINS = frozenset({"hacs"})


@functools.lru_cache(maxsize=512)
def _is_built_in(domain: str) -> bool:
    """Whether Home Assistant itself ships this integration.

    Asked of the import system, not of a list we keep: present in
    `homeassistant.components` means public software, under its own name.
    Anything that cannot be answered is treated as custom.
    """
    import importlib.util

    if not isinstance(domain, str) or not domain.isidentifier():
        return False

    try:
        return importlib.util.find_spec(f"homeassistant.components.{domain}") is not None
    except Exception:  # noqa: BLE001
        return False


def _device_entries(registry: Any) -> list[Any]:
    """Every device entry, on Home Assistant from 2024.6 to after 2027.9.

    Iterating `registry.devices` yields the entries from 2026.10, where using it
    as a mapping (`.values()`, `[id]`) is deprecated and breaks in 2027.9. Before
    that, iterating it yields device ids, and `.values()` is the way.
    """
    devices = registry.devices
    entries = list(devices)

    if entries and isinstance(entries[0], str):
        return list(devices.values())

    return entries


def _is_local(slug: Any) -> bool:
    """Not public software: built in this house, or from a repository it added."""
    return isinstance(slug, str) and not slug.startswith(_PUBLIC_ADDON_REPOSITORIES)


def _as_list(value: Any) -> list[Any]:
    """An array, always. A set is sorted so the hash does not depend on luck."""
    if isinstance(value, list):
        return value

    if isinstance(value, tuple):
        return list(value)

    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)

    return []


def _level(record: Any) -> str:
    return str(getattr(record, "level", "")).upper()


def _source(record: Any) -> str:
    source = getattr(record, "source", None)

    if isinstance(source, (list, tuple)) and source:
        # (path, line). The path is our own file layout, not the household's.
        return str(source[0]).split("/")[-1]

    return str(getattr(record, "name", "unknown"))


def _message(record: Any) -> str:
    message = getattr(record, "message", "")

    # system_log holds the message as a deduplicated list, newest last.
    if isinstance(message, (list, tuple)):
        return str(message[-1]) if message else ""

    return str(message)


def _timestamp(record: Any) -> str:
    from datetime import UTC, datetime

    timestamp = getattr(record, "timestamp", None)

    try:
        return datetime.fromtimestamp(float(timestamp), tz=UTC).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _is_private(address: str) -> bool:
    import ipaddress

    try:
        return ipaddress.ip_address(address).is_private
    except ValueError:
        return False
