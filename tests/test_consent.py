"""Asking the household, and taking their answer.

The file the whole product rests on. The notice this component shows in the
house says nobody gets in unless they authorise it, every time, and only for as
long as they decide - and that sentence is true only because the question is
asked here and the answer travels back from here.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.util import dt as dt_util

from conftest import FakeStore
from custom_components.haim_agent import consent as consent_module
from custom_components.haim_agent.consent import (
    ACTION_APPROVE,
    ACTION_REJECT,
    ISSUE_PREFIX,
    ConsentManager,
)

REQUEST = {
    "request_uuid": "11111111-1111-4111-8111-111111111111",
    "subject": "Sostituzione gateway Zigbee",
    "reason": "Il coordinatore non risponde da ieri sera.",
    "requested_minutes": 120,
    "installer": "Mario Rossi",
    "locale": "it",
}


class FakeServices:
    def __init__(self, services: dict[str, dict[str, Any]] | None = None, failing: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._services = services if services is not None else {"notify": {"mobile_app_telefono": {}}}
        self._failing = failing or set()

    def async_services(self) -> dict[str, dict[str, Any]]:
        return self._services

    async def async_call(self, domain: str, service: str, payload: dict, blocking: bool = False) -> None:
        if f"{domain}.{service}" in self._failing:
            raise RuntimeError(f"{domain}.{service} is not available")

        self.calls.append((domain, service, payload))


class FakeBus:
    def __init__(self) -> None:
        self.listeners: dict[str, Any] = {}
        self.fired: list[tuple[str, dict, Any]] = []

    def async_listen(self, event: str, handler):
        self.listeners[event] = handler

        def unsubscribe() -> None:
            self.listeners.pop(event, None)

        return unsubscribe

    def async_fire(self, event: str, data: dict, context=None) -> None:
        self.fired.append((event, data, context))


def _user(user_id: str, system: bool = False, active: bool = True) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, system_generated=system, is_active=active)


# Who exists in this house. "mamma" is somebody who lives here; "supervisor" is
# Home Assistant talking to itself; "manutenzione" is the installer's account.
MAMMA = "user-mamma"
SUPERVISOR = "user-supervisor"
MAINTENANCE = "user-manutenzione"


class FakeAuth:
    def __init__(self) -> None:
        self.users = {
            MAMMA: _user(MAMMA),
            SUPERVISOR: _user(SUPERVISOR, system=True),
            MAINTENANCE: _user(MAINTENANCE),
        }

    async def async_get_user(self, user_id: str):
        return self.users.get(user_id)

    async def async_get_users(self):
        return list(self.users.values())


class FakeMaintenance:
    """maintenance.py's answer to "which account is the installer's", and is it awake."""

    def __init__(self, auth: FakeAuth) -> None:
        self._auth = auth
        self.awake = False

    async def async_find(self):
        return self._auth.users[MAINTENANCE]

    async def async_is_awake(self) -> bool:
        return self.awake


class FakeHass:
    def __init__(self, services: FakeServices | None = None) -> None:
        self.services = services or FakeServices()
        self.bus = FakeBus()
        self.config = SimpleNamespace(language="it")
        self.auth = FakeAuth()
        self.maintenance = FakeMaintenance(self.auth)


def _tag(request: dict = REQUEST) -> str:
    return consent_module.NOTIFICATION_PREFIX + request["request_uuid"]


def _tap(action: str, tag: str | None = None, user_id: str | None = MAMMA, parent_id: str | None = None):
    """The event the companion app fires: the phone's user in the context, no parent."""
    data = {"action": action}

    if tag is not None:
        data["tag"] = tag

    return SimpleNamespace(data=data, context=SimpleNamespace(user_id=user_id, parent_id=parent_id))


class FakeApi:
    def __init__(self) -> None:
        self.decisions: list[tuple[str, dict]] = []

    async def decide_access_request(self, request_uuid: str, payload: dict) -> None:
        self.decisions.append((request_uuid, payload))


@pytest.fixture(autouse=True)
def _disk(monkeypatch):
    """The pending request is written down (D33), and here that is a dict."""
    FakeStore.files = {}
    monkeypatch.setattr(consent_module, "Store", FakeStore)

    return FakeStore.files


@pytest.fixture(autouse=True)
def _issue_registry(monkeypatch):
    """The issue registry needs a real Home Assistant. Record the calls instead."""
    created: list[tuple[str, dict]] = []
    deleted: list[str] = []
    standing: dict[str, SimpleNamespace] = {}

    def create(hass, domain, issue_id, **kwargs):
        created.append((issue_id, kwargs))
        standing[issue_id] = SimpleNamespace(domain=domain, issue_id=issue_id)

    def delete(hass, domain, issue_id):
        deleted.append(issue_id)
        standing.pop(issue_id, None)

    monkeypatch.setattr("custom_components.haim_agent.consent.ir.async_create_issue", create)
    monkeypatch.setattr("custom_components.haim_agent.consent.ir.async_delete_issue", delete)
    monkeypatch.setattr(
        "custom_components.haim_agent.consent.ir.async_get",
        lambda hass: SimpleNamespace(issues=standing),
    )

    return created, deleted


@pytest.fixture
def manager():
    hass = FakeHass()
    api = FakeApi()
    consent = ConsentManager(hass, api, maintenance=hass.maintenance)
    consent.start()

    return consent, hass, api


async def test_it_asks_three_ways(manager, _issue_registry) -> None:
    consent, hass, _ = manager
    created, _deleted = _issue_registry

    result = await consent.async_ask(REQUEST)

    assert set(result["asked"]) == {"companion", "repairs", "notification"}
    assert consent.has_pending

    services = [(domain, service) for domain, service, _ in hass.services.calls]

    assert ("notify", "mobile_app_telefono") in services
    assert ("persistent_notification", "create") in services
    assert created and created[0][0].endswith(REQUEST["request_uuid"])


async def test_a_missing_companion_app_does_not_stop_the_others(_issue_registry) -> None:
    # One channel failing must never be the reason a household is not asked at
    # all: no companion app installed is the normal case, not an error.
    hass = FakeHass(FakeServices(services={"notify": {}}))
    consent = ConsentManager(hass, FakeApi())

    result = await consent.async_ask(REQUEST)

    assert "companion" not in result["asked"]
    assert set(result["asked"]) == {"repairs", "notification"}


async def test_it_refuses_a_request_with_no_id(manager) -> None:
    consent, _, _ = manager

    with pytest.raises(ValueError):
        await consent.async_ask({"subject": "x"})


async def test_it_never_answers_on_anybody_behalf(manager) -> None:
    # No timeout that approves, no default, no "the installer is trusted". A
    # request nobody answers expires, and expiring grants nothing.
    consent, _, api = manager

    await consent.async_ask(REQUEST)

    assert api.decisions == []
    assert consent.has_pending


async def test_a_tap_on_yes_sends_the_answer_and_takes_the_question_down(manager, _issue_registry) -> None:
    consent, hass, api = manager
    _created, deleted = _issue_registry

    await consent.async_ask(REQUEST)

    handler = hass.bus.listeners["mobile_app_notification_action"]
    await handler(_tap(ACTION_APPROVE, _tag()))

    assert api.decisions == [(REQUEST["request_uuid"], {"decision": "approved", "via": "companion"})]
    assert not consent.has_pending

    # And it is gone from everywhere it was put: a card still saying "waiting
    # for your answer" after they answered is how a household stops believing
    # what this component tells them.
    assert deleted and deleted[0].endswith(REQUEST["request_uuid"])
    assert ("persistent_notification", "dismiss") in [(d, s) for d, s, _ in hass.services.calls]


async def test_an_iphone_tap_carries_the_tag_in_action_data(manager) -> None:
    """The iOS app sends `action_data` back with a tap, not `tag` (D40)."""
    consent, hass, api = manager

    await consent.async_ask(REQUEST)

    sent = next(payload for domain, service, payload in hass.services.calls if domain == "notify")
    assert sent["data"]["action_data"] == {"tag": _tag()}

    handler = hass.bus.listeners["mobile_app_notification_action"]

    # A stale tag in action_data answers nothing, the same as in `tag`.
    stale = _tap(ACTION_APPROVE)
    stale.data["action_data"] = {"tag": consent_module.NOTIFICATION_PREFIX + "an-older-request"}
    await handler(stale)

    assert api.decisions == []

    tap = _tap(ACTION_APPROVE)
    tap.data["action_data"] = {"tag": _tag()}
    await handler(tap)

    assert api.decisions == [(REQUEST["request_uuid"], {"decision": "approved", "via": "companion"})]


async def test_a_tap_on_no_sends_a_refusal(manager) -> None:
    consent, hass, api = manager

    await consent.async_ask(REQUEST)

    handler = hass.bus.listeners["mobile_app_notification_action"]
    await handler(_tap(ACTION_REJECT, _tag()))

    assert api.decisions[0][1]["decision"] == "rejected"


async def test_a_tap_on_something_else_is_ignored(manager) -> None:
    consent, hass, api = manager

    await consent.async_ask(REQUEST)

    handler = hass.bus.listeners["mobile_app_notification_action"]
    await handler(_tap("SOME_OTHER_INTEGRATION", _tag()))

    assert api.decisions == []
    assert consent.has_pending


async def test_it_sends_a_reduced_duration_when_one_is_given(manager) -> None:
    consent, _, api = manager

    await consent.async_ask(REQUEST)
    await consent.async_answer(approved=True, via="repairs", granted_minutes=60)

    assert api.decisions[0][1] == {"decision": "approved", "via": "repairs", "granted_minutes": 60}


async def test_it_takes_the_question_down_when_it_was_answered_elsewhere(manager, _issue_registry) -> None:
    consent, hass, api = manager
    _created, deleted = _issue_registry

    await consent.async_ask(REQUEST)
    await consent.async_clear(REQUEST["request_uuid"])

    assert not consent.has_pending
    assert deleted
    # Nothing was reported: the answer was given somewhere else and is already
    # recorded. Sending one from here would overwrite it.
    assert api.decisions == []


async def test_it_tells_an_entity_the_moment_something_changes(manager) -> None:
    # Consent changes when a command arrives or a button is tapped, neither of
    # which is a heartbeat. Without this the sensor would only catch up on the
    # next one, up to thirty seconds late.
    consent, _, _ = manager
    changes: list[int] = []

    consent.on_change(lambda: changes.append(1))

    await consent.async_ask(REQUEST)
    assert len(changes) == 1

    await consent.async_clear(REQUEST["request_uuid"])
    assert len(changes) == 2


async def test_it_speaks_the_language_of_the_installation(manager) -> None:
    # Decision D35: the household reads the language set in Home Assistant,
    # whatever the request carries.
    consent, hass, _ = manager

    def last_title() -> str:
        return [payload for domain, service, payload in hass.services.calls if service == "create"][-1]["title"]

    await consent.async_ask({**REQUEST, "locale": "en"})

    assert "Mario Rossi" in last_title()
    assert "chiede di entrare" in last_title()

    for language, expected in (("en-GB", "is asking to connect"), ("de", "is asking to connect"), ("it", "chiede di entrare")):
        consent._pending = None
        hass.config.language = language

        await consent.async_ask(REQUEST)

        assert expected in last_title(), language


async def test_the_phone_has_to_be_unlocked_to_answer(manager) -> None:
    # The answer decides who gets into their house. It is worth making them
    # unlock the phone first.
    consent, hass, _ = manager

    await consent.async_ask(REQUEST)

    payload = next(payload for domain, service, payload in hass.services.calls if domain == "notify")

    assert payload["data"]["authenticationRequired"] is True
    assert [action["action"] for action in payload["data"]["actions"]] == [ACTION_APPROVE, ACTION_REJECT]


async def test_it_takes_the_question_down_on_a_refusal_from_elsewhere(_issue_registry) -> None:
    """Contract 1.1.0 added `connection_rejected` for exactly this.

    Before it there was only the approval, and a no given from the portal left
    the question on somebody's screen until it expired - harmless, since
    answering a closed request does nothing, but a lie in their kitchen.
    """
    from custom_components.haim_agent.commands import register_supported_commands

    hass = FakeHass()
    api = FakeApi()
    consent = ConsentManager(hass, api)

    registered: dict[str, object] = {}

    class FakeDispatcher:
        def register(self, action, handler, **_options):
            registered[action] = handler

    class FakeCoordinator:
        async def async_heartbeat(self):
            return True

    register_supported_commands(FakeDispatcher(), hass, FakeCoordinator(), consent)

    assert "connection_rejected" in registered
    assert "connection_approved" in registered

    await consent.async_ask(REQUEST)
    assert consent.has_pending

    await registered["connection_rejected"]({"request_uuid": REQUEST["request_uuid"]})

    assert not consent.has_pending
    # Nothing was reported back: the answer was given elsewhere and is already
    # recorded up there.
    assert api.decisions == []


# --------------------------------------------------------- surviving a restart


async def test_the_question_is_written_down_when_it_is_asked(manager, _disk) -> None:
    consent, _, _ = manager

    await consent.async_ask(REQUEST)

    written = _disk["haim_agent.consent.default"]

    assert written["request"]["request_uuid"] == REQUEST["request_uuid"]
    assert written["asked_at"]

    # The question and when it arrived. Never an answer, never a token: what is
    # on this disk must not be able to let anybody in.
    assert set(written) == {"request", "asked_at"}


async def test_a_restart_does_not_lose_a_question_somebody_was_answering(_disk, _issue_registry) -> None:
    created, _deleted = _issue_registry

    before = ConsentManager(FakeHass(), FakeApi())
    before.start()
    await before.async_ask(REQUEST)

    created.clear()

    # A new manager on a new process: this is what a restart looks like.
    after = ConsentManager(FakeHass(), FakeApi())
    after.start()
    await after.async_restore()

    assert after.has_pending
    assert after.pending["request_uuid"] == REQUEST["request_uuid"]

    # The card is back in Repairs, where a restart had left a dead one: the
    # issue survived in the registry but nothing behind it did, so tapping it
    # answered "Handler does not support user" to somebody in a hallway.
    assert [issue_id for issue_id, _ in created] == [ISSUE_PREFIX + REQUEST["request_uuid"]]


async def test_the_phone_is_not_told_twice(_disk) -> None:
    hass = FakeHass()

    before = ConsentManager(hass, FakeApi())
    await before.async_ask(REQUEST)

    hass.services.calls.clear()

    after = ConsentManager(hass, FakeApi())
    await after.async_restore()

    sent = [call for call in hass.services.calls if call[0] == "notify"]

    # The notification is still on the phone: a restart does not touch it. A
    # second "somebody wants to come into your house" is how a component gets
    # uninstalled.
    assert sent == []

    # The other two do not survive a restart, so those are put back.
    assert [call for call in hass.services.calls if call[0] == "persistent_notification"]


async def test_it_gives_up_on_a_question_too_old_to_be_answered(_disk) -> None:
    before = ConsentManager(FakeHass(), FakeApi())
    await before.async_ask(REQUEST)

    _disk["haim_agent.consent.default"]["asked_at"] = (
        dt_util.utcnow() - timedelta(minutes=91)
    ).isoformat()

    after = ConsentManager(FakeHass(), FakeApi())
    await after.async_restore()

    # The panel gives a request sixty minutes and the panel is the authority.
    # This is only the backstop that stops a question living on a family's
    # screen forever if the two ever lose touch.
    assert not after.has_pending
    assert "haim_agent.consent.default" not in _disk


async def test_an_answer_takes_it_off_the_disk_too(manager, _disk) -> None:
    consent, hass, _ = manager

    await consent.async_ask(REQUEST)

    handler = hass.bus.listeners["mobile_app_notification_action"]
    await handler(_tap(ACTION_APPROVE, _tag()))

    # Otherwise the next restart puts an answered question back on their screen.
    assert "haim_agent.consent.default" not in _disk


async def test_two_instances_do_not_write_over_each_other(_disk) -> None:
    await ConsentManager(FakeHass(), FakeApi(), "casa-uno").async_ask(REQUEST)
    await ConsentManager(FakeHass(), FakeApi(), "casa-due").async_ask(
        {**REQUEST, "request_uuid": "22222222-2222-4222-8222-222222222222"}
    )

    assert _disk["haim_agent.consent.casa-uno"]["request"]["request_uuid"] == REQUEST["request_uuid"]
    assert (
        _disk["haim_agent.consent.casa-due"]["request"]["request_uuid"]
        == "22222222-2222-4222-8222-222222222222"
    )


async def test_it_takes_the_question_down_when_nobody_answered_in_time(_disk, _issue_registry) -> None:
    """Contract 1.3.0 added `connection_expired` for exactly this.

    Da quando la domanda sopravvive al riavvio (D33), niente altro la toglieva
    piu' dallo schermo: una richiesta che nessuno aveva risposto restava in
    casa di una famiglia per sempre. Scaduta non e' rifiutata, ma qui fanno la
    stessa cosa - la differenza la tiene il pannello, che e' dove serve.
    """
    from custom_components.haim_agent.commands import register_supported_commands

    hass = FakeHass()
    api = FakeApi()
    consent = ConsentManager(hass, api)

    registered: dict[str, object] = {}

    class FakeDispatcher:
        def register(self, action, handler, **_options):
            registered[action] = handler

    class FakeCoordinator:
        async def async_heartbeat(self):
            return True

    register_supported_commands(FakeDispatcher(), hass, FakeCoordinator(), consent)

    assert "connection_expired" in registered

    await consent.async_ask(REQUEST)

    await registered["connection_expired"]({"request_uuid": REQUEST["request_uuid"]})

    assert not consent.has_pending

    # E nemmeno il prossimo riavvio la rimette a schermo.
    assert "haim_agent.consent.default" not in _disk

    # Niente e' stato risposto: nessuno ha detto niente, ed e' il punto.
    assert api.decisions == []


async def test_it_takes_down_a_card_left_over_from_a_previous_life(_disk, _issue_registry) -> None:
    """Home Assistant tiene il suo registro fra un riavvio e l'altro.

    Un riquadro alzato prima che la domanda venisse tenuta su disco (D33) resta
    a schermo con niente dietro: chi lo apre legge «Handler does not support
    user». Non lo toglie nessun altro, e ogni impianto che si aggiorna a D33 ne
    ha uno addosso.
    """
    created, deleted = _issue_registry

    vecchia = ConsentManager(FakeHass(), FakeApi())
    await vecchia.async_ask({**REQUEST, "request_uuid": "99999999-9999-4999-8999-999999999999"})

    # La domanda vera, quella che il riavvio deve ritrovare.
    nuova_hass = FakeHass()
    nuova = ConsentManager(nuova_hass, FakeApi())
    await nuova.async_ask(REQUEST)

    created.clear()
    deleted.clear()

    dopo_il_riavvio = ConsentManager(FakeHass(), FakeApi())
    await dopo_il_riavvio.async_restore()

    assert deleted == [ISSUE_PREFIX + "99999999-9999-4999-8999-999999999999"]

    # E quella ancora buona resta, altrimenti la pulizia sarebbe peggio del male.
    assert dopo_il_riavvio.pending["request_uuid"] == REQUEST["request_uuid"]
    assert [issue_id for issue_id, _ in created] == [ISSUE_PREFIX + REQUEST["request_uuid"]]


# ------------------------------------------------------- who may answer (D36)

OLD_REQUEST = {**REQUEST, "request_uuid": "00000000-0000-4000-8000-000000000000"}


async def test_the_proven_attack_an_old_tag_fired_as_maintenance_approves_nothing(manager) -> None:
    """La revisione del 14/10/2026: un evento con il tag di una richiesta
    vecchia, sparato col contesto dell'utente di manutenzione, approvava la
    richiesta nuova come via=companion."""
    consent, hass, api = manager

    await consent.async_ask(REQUEST)

    handler = hass.bus.listeners["mobile_app_notification_action"]
    await handler(_tap(ACTION_APPROVE, _tag(OLD_REQUEST), user_id=MAINTENANCE))

    assert api.decisions == []
    assert consent.has_pending


async def test_a_notification_left_from_an_old_request_does_not_answer_the_new_one(manager) -> None:
    # Anche se a toccarla e' davvero qualcuno di casa: quella notifica parlava
    # di un'altra richiesta, e questa non l'ha letta nessuno.
    consent, hass, api = manager

    await consent.async_ask(REQUEST)

    handler = hass.bus.listeners["mobile_app_notification_action"]
    await handler(_tap(ACTION_APPROVE, _tag(OLD_REQUEST)))
    await handler(_tap(ACTION_APPROVE, tag=None))

    assert api.decisions == []
    assert consent.has_pending


@pytest.mark.parametrize(
    ("user_id", "parent_id", "why"),
    [
        (MAINTENANCE, None, "the installer's own account"),
        (SUPERVISOR, None, "a system user"),
        ("user-che-non-esiste", None, "a user this house does not have"),
        (None, None, "nobody at all"),
        # Point 4: an automation left in the house during an earlier window.
        # Its events carry a parent and no user...
        (None, "01AUTOMAZIONE", "an automation"),
        # ...and even with a user in the context, a parent means it was set off
        # by something else, not by a finger on a phone.
        (MAMMA, "01AUTOMAZIONE", "an automation started by somebody"),
    ],
)
async def test_only_a_person_who_lives_here_can_answer(manager, user_id, parent_id, why) -> None:
    consent, hass, api = manager

    await consent.async_ask(REQUEST)

    handler = hass.bus.listeners["mobile_app_notification_action"]
    await handler(_tap(ACTION_APPROVE, _tag(), user_id=user_id, parent_id=parent_id))

    assert api.decisions == [], why
    assert consent.has_pending, why


async def test_an_event_with_no_context_answers_nothing(manager) -> None:
    consent, hass, api = manager

    await consent.async_ask(REQUEST)

    handler = hass.bus.listeners["mobile_app_notification_action"]
    await handler(SimpleNamespace(data={"action": ACTION_APPROVE, "tag": _tag()}))

    assert api.decisions == []


async def test_an_inactive_user_cannot_answer(manager) -> None:
    consent, hass, api = manager
    hass.auth.users[MAMMA].is_active = False

    await consent.async_ask(REQUEST)

    handler = hass.bus.listeners["mobile_app_notification_action"]
    await handler(_tap(ACTION_APPROVE, _tag()))

    assert api.decisions == []


async def test_nothing_in_the_house_answers_while_a_session_is_open(manager) -> None:
    # During a window the installer is an administrator here: he can fire any
    # event and open any repair. None of it may answer the next request.
    consent, hass, api = manager
    consent.attach_session(lambda: True)

    await consent.async_ask(REQUEST)

    handler = hass.bus.listeners["mobile_app_notification_action"]
    await handler(_tap(ACTION_APPROVE, _tag()))
    await handler(_tap(ACTION_REJECT, _tag()))

    with pytest.raises(consent_module.ConsentRefused):
        await consent.async_answer(approved=True, via="repairs", request_uuid=REQUEST["request_uuid"])

    assert api.decisions == []
    assert consent.has_pending


async def test_an_awake_maintenance_account_counts_as_an_open_session(manager) -> None:
    # The second sign, independent from the first: an administrator awake with
    # no session on record is somebody nobody accounted for.
    consent, hass, api = manager
    consent.attach_session(lambda: False)
    hass.maintenance.awake = True

    await consent.async_ask(REQUEST)

    with pytest.raises(consent_module.ConsentRefused):
        await consent.async_answer(approved=True, via="repairs")

    assert api.decisions == []


async def test_if_it_cannot_tell_whether_a_session_is_open_it_takes_no_answer(manager) -> None:
    consent, _, api = manager

    def broken() -> bool:
        raise RuntimeError("remote access is gone")

    consent.attach_session(broken)

    await consent.async_ask(REQUEST)

    with pytest.raises(consent_module.ConsentRefused):
        await consent.async_answer(approved=True, via="repairs")

    assert api.decisions == []


async def test_it_only_answers_the_request_that_is_waiting(manager) -> None:
    consent, _, api = manager

    await consent.async_ask(REQUEST)

    with pytest.raises(consent_module.ConsentRefused):
        await consent.async_answer(approved=True, via="repairs", request_uuid=OLD_REQUEST["request_uuid"])

    assert api.decisions == []
    assert consent.has_pending


async def test_who_answered_stays_in_the_house(manager) -> None:
    # Rule 4: the people who live here go in no payload. Who said yes is in
    # their own Logbook, and only there.
    consent, hass, api = manager

    await consent.async_ask(REQUEST)

    handler = hass.bus.listeners["mobile_app_notification_action"]
    await handler(_tap(ACTION_APPROVE, _tag()))

    assert api.decisions == [(REQUEST["request_uuid"], {"decision": "approved", "via": "companion"})]
    assert MAMMA not in repr(api.decisions)

    logbook = [(data, context) for event, data, context in hass.bus.fired if event == "logbook_entry"]

    assert len(logbook) == 1
    data, context = logbook[0]

    # The Logbook takes the person from the context; the text does not name them.
    assert context.user_id == MAMMA
    assert MAMMA not in repr(data)
    assert data["name"] == "Richiesta di accesso di Mario Rossi"
    assert data["message"] == "accolta dal telefono"


def _cleared(hass) -> list[dict]:
    return [
        payload
        for domain, service, payload in hass.services.calls
        if domain == "notify" and payload.get("message") == "clear_notification"
    ]


async def test_an_answer_takes_the_notification_off_the_phone(manager) -> None:
    consent, hass, _ = manager

    await consent.async_ask(REQUEST)

    handler = hass.bus.listeners["mobile_app_notification_action"]
    await handler(_tap(ACTION_REJECT, _tag()))

    assert _cleared(hass) == [{"message": "clear_notification", "data": {"tag": _tag()}}]


@pytest.mark.parametrize("action", ["connection_rejected", "connection_expired", "connection_approved"])
async def test_a_request_closed_elsewhere_comes_off_the_phone_too(action) -> None:
    # Otherwise the notification stays on the phone with buttons on it, for a
    # question nobody is asking any more.
    from custom_components.haim_agent.commands import register_supported_commands

    hass = FakeHass()
    consent = ConsentManager(hass, FakeApi(), maintenance=hass.maintenance)

    registered: dict[str, Any] = {}

    class FakeDispatcher:
        def register(self, name, handler, **_options):
            registered[name] = handler

    class FakeCoordinator:
        async def async_heartbeat(self):
            return True

    register_supported_commands(FakeDispatcher(), hass, FakeCoordinator(), consent)

    await consent.async_ask(REQUEST)
    await registered[action]({"request_uuid": REQUEST["request_uuid"]})

    assert _cleared(hass) == [{"message": "clear_notification", "data": {"tag": _tag()}}]


async def test_one_phone_that_cannot_be_reached_does_not_keep_it_on_the_others() -> None:
    services = FakeServices(services={"notify": {"mobile_app_spento": {}, "mobile_app_acceso": {}}})
    hass = FakeHass(services)
    original = services.async_call

    async def flaky(domain, service, payload, blocking=False):
        if service == "mobile_app_spento" and payload.get("message") == "clear_notification":
            raise RuntimeError("phone is off")

        await original(domain, service, payload, blocking)

    services.async_call = flaky

    consent = ConsentManager(hass, FakeApi(), maintenance=hass.maintenance)
    await consent.async_ask(REQUEST)
    await consent.async_clear(REQUEST["request_uuid"])

    assert [
        service for domain, service, payload in services.calls if payload.get("message") == "clear_notification"
    ] == ["mobile_app_acceso"]


async def test_a_yes_hurries_the_poll_and_a_no_does_not(manager) -> None:
    """20/10/2026: the installer pressed «Apri» seconds after the yes, and the
    house only came to collect the window on its next thirty-second round."""
    consent, _, _ = manager
    hurried: list[int] = []

    consent.on_approved(lambda: hurried.append(1))

    await consent.async_ask(REQUEST)
    await consent.async_answer(approved=False, via="repairs")

    assert hurried == []

    await consent.async_ask(REQUEST)
    await consent.async_answer(approved=True, via="repairs")

    assert hurried == [1]


async def test_a_hurry_that_fails_does_not_undo_the_answer(manager) -> None:
    consent, _, api = manager

    def broken() -> None:
        raise RuntimeError("no timer")

    consent.on_approved(broken)

    await consent.async_ask(REQUEST)
    await consent.async_answer(approved=True, via="repairs")

    assert api.decisions[0][1]["decision"] == "approved"
    assert not consent.has_pending
