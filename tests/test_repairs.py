"""Le due cose che una famiglia vede, e in che lingua le vede.

Il dialogo delle Riparazioni e' l'unico dei tre canali di consenso che puo'
chiedere «per quanto tempo»: due bottoni su una schermata di blocco non ce la
fanno, quindi e' qui che una famiglia taglia sei ore a una. Se le opzioni sono
in inglese, o se per leggerle bisogna scorrere una tendina su un telefono in
corridoio, quel taglio non lo fa nessuno.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
import voluptuous as vol
from homeassistant.util import dt as dt_util

from conftest import FakeStore
from custom_components.haim_agent import repairs
from custom_components.haim_agent.consent import ConsentRefused
from custom_components.haim_agent.repairs import (
    ISSUE_WHAT_WE_SEND,
    AccessRequestFlow,
    async_announce_what_we_send,
)


@pytest.fixture
def raised(monkeypatch):
    """Quali avvisi sono stati alzati, senza un registro vero."""
    issues: list[str] = []

    FakeStore.files = {}

    monkeypatch.setattr(repairs, "Store", FakeStore)
    monkeypatch.setattr(
        repairs.ir,
        "async_create_issue",
        lambda hass, domain, issue_id, **kwargs: issues.append(issue_id),
    )

    return issues


async def test_the_household_is_told_what_leaves_the_house(raised) -> None:
    await async_announce_what_we_send(object(), "counts", "un-impianto")

    assert raised == [ISSUE_WHAT_WE_SEND]


async def test_a_restart_is_not_a_reason_to_say_it_again(raised) -> None:
    await async_announce_what_we_send(object(), "counts", "un-impianto")
    await async_announce_what_we_send(object(), "counts", "un-impianto")
    await async_announce_what_we_send(object(), "counts", "un-impianto")

    # Tre riavvii in una sera erano tre avvisi, ed e' cosi' che un testo scritto
    # per essere letto diventa un testo che si impara a togliere di mezzo.
    assert raised == [ISSUE_WHAT_WE_SEND]


async def test_it_comes_back_after_a_week(raised) -> None:
    await async_announce_what_we_send(object(), "counts", "un-impianto")

    key = "haim_agent.notice.un-impianto"
    FakeStore.files[key] = {
        "what_we_send_at": (dt_util.utcnow() - timedelta(days=8)).isoformat()
    }

    await async_announce_what_we_send(object(), "counts", "un-impianto")

    assert raised == [ISSUE_WHAT_WE_SEND, ISSUE_WHAT_WE_SEND]


async def test_two_instances_are_told_separately(raised) -> None:
    await async_announce_what_we_send(object(), "counts", "casa-uno")
    await async_announce_what_we_send(object(), "counts", "casa-due")

    assert raised == [ISSUE_WHAT_WE_SEND, ISSUE_WHAT_WE_SEND]


CARD = "11111111-1111-4111-8111-111111111111"


class FakeConsent:
    def __init__(self, pending: dict | None, refuse: bool = False) -> None:
        self.pending = pending
        self.answers: list[dict] = []
        self._refuse = refuse

    async def async_answer(self, **kwargs) -> None:
        if self._refuse:
            raise ConsentRefused("a session is open")

        self.answers.append(kwargs)


def _flow(
    requested_minutes: int = 120,
    pending_uuid: str = CARD,
    refuse: bool = False,
) -> AccessRequestFlow:
    consent = FakeConsent(
        {
            "request_uuid": pending_uuid,
            "installer": "Mario Rossi",
            "subject": "Sostituzione gateway Zigbee",
            "reason": "Non risponde da ieri sera.",
            "requested_minutes": requested_minutes,
        },
        refuse=refuse,
    )
    entry = SimpleNamespace(runtime_data=SimpleNamespace(consent=consent))
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda domain: [entry])
    )

    flow = AccessRequestFlow(hass, CARD)
    flow.consent = consent
    flow.hass = hass
    flow.flow_id = "un-flusso"
    flow.handler = "haim_agent"

    return flow


def _field(schema: vol.Schema, name: str):
    for key, value in schema.schema.items():
        if str(key) == name:
            return value

    raise AssertionError(f"il campo {name} non c'e'")


async def test_the_two_questions_are_asked_in_the_household_language() -> None:
    form = await _flow(360).async_step_decide()

    for name in ("decision", "granted_minutes"):
        selector = _field(form["data_schema"], name)

        # Senza translation_key Home Assistant mostra i valori grezzi, ed e'
        # cosi' che "approve" e "reject" sono finiti su uno schermo italiano.
        assert selector.config["translation_key"] == name

        # Un elenco, non una tendina: si legge su un telefono, in corridoio,
        # e ogni opzione deve essere visibile senza scorrere niente.
        assert selector.config["mode"] == "list"


async def test_it_never_offers_more_time_than_was_asked_for() -> None:
    form = await _flow(120).async_step_decide()

    assert _field(form["data_schema"], "granted_minutes").config["options"] == ["60", "120"]


async def test_six_hours_asked_means_six_hours_offerable() -> None:
    form = await _flow(360).async_step_decide()

    assert _field(form["data_schema"], "granted_minutes").config["options"] == [
        "60",
        "120",
        "240",
        "360",
    ]


async def test_every_duration_it_can_offer_has_a_label_in_both_languages() -> None:
    import json
    from pathlib import Path

    here = Path(__file__).resolve().parents[1] / "custom_components" / "haim_agent"

    for language in ("it", "en"):
        labels = json.loads((here / "translations" / f"{language}.json").read_text())

        for name, options in (
            ("decision", ["approve", "reject"]),
            ("granted_minutes", ["60", "120", "240", "360"]),
        ):
            translated = labels["selector"][name]["options"]

            for option in options:
                assert translated.get(option), f"{language}: manca {name}.{option}"


async def test_nobody_says_yes_by_inertia() -> None:
    # Il modulo arrivava con «Sì, può entrare» gia' selezionato: bastava
    # premere Invia senza leggere. Adesso chi lo apre deve scegliere.
    form = await _flow().async_step_decide()

    for key in form["data_schema"].schema:
        if str(key) == "decision":
            assert key.default is vol.UNDEFINED


@pytest.fixture
def taken_down(monkeypatch) -> list[str]:
    removed: list[str] = []

    monkeypatch.setattr(
        repairs.ir,
        "async_delete_issue",
        lambda hass, domain, issue_id: removed.append(issue_id),
    )

    return removed


async def test_a_card_for_another_request_shows_nothing_and_answers_nothing(taken_down) -> None:
    """La revisione del 14/10/2026: il riquadro mostrava i dati di
    consent.pending e rispondeva all'uuid del riquadro. Due richieste diverse."""
    flow = _flow(pending_uuid="22222222-2222-4222-8222-222222222222")

    shown = await flow.async_step_decide()

    assert shown["type"] == "abort"
    assert shown["reason"] == "no_longer_valid"

    submitted = await flow.async_step_decide({"decision": "approve", "granted_minutes": "60"})

    assert submitted["type"] == "abort"
    assert flow.consent.answers == []

    # Un riquadro senza niente dietro non resta a schermo.
    assert set(taken_down) == {"access_request_" + CARD}


async def test_a_card_whose_request_is_gone_answers_nothing(taken_down) -> None:
    flow = _flow()
    flow.consent.pending = None

    result = await flow.async_step_decide({"decision": "approve"})

    assert result["type"] == "abort"
    assert result["reason"] == "no_longer_valid"
    assert flow.consent.answers == []


async def test_the_card_answers_the_request_it_shows() -> None:
    flow = _flow(240)

    await flow.async_step_decide({"decision": "reject", "granted_minutes": "60"})

    assert flow.consent.answers == [
        {"approved": False, "via": "repairs", "granted_minutes": 60, "request_uuid": CARD}
    ]


async def test_while_a_session_is_open_the_card_says_so_and_sends_nothing() -> None:
    flow = _flow(refuse=True)

    result = await flow.async_step_decide({"decision": "approve", "granted_minutes": "60"})

    assert result["type"] == "abort"
    assert result["reason"] == "cannot_answer_now"


async def test_the_card_can_say_why_it_will_not_answer_in_both_languages() -> None:
    import json
    from pathlib import Path

    here = Path(__file__).resolve().parents[1] / "custom_components" / "haim_agent"

    for source in ("strings.json", "translations/it.json", "translations/en.json"):
        text = json.loads((here / source).read_text())
        abort = text["issues"]["access_request"]["fix_flow"]["abort"]

        for reason in ("no_longer_valid", "cannot_answer_now"):
            assert abort.get(reason), f"{source}: manca {reason}"
