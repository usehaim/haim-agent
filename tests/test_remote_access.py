"""The window, from inside the house.

The second of the three independent expiries (§11.3), and the one where we do
better than the competing product: the deadline is on DISK. Theirs is in memory,
so a Home Assistant restart during a session loses it.

The promise these tests hold the code to (D27, D37): outside an authorised
window there is no key to this house anywhere, and no administrator account is
awake. Not after a restart, not after the integration is disabled, not when our
cloud is unreachable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.haim_agent.api import HaimApiError
from custom_components.haim_agent.remote_access import RemoteAccessManager, async_remove_installation

SESSION_KEY = "haim_agent.session.lab"
MAINTENANCE_KEY = "haim_agent.maintenance"


class FakeStore:
    """Home Assistant's Store, reduced to what this uses - and it keeps the data
    across instances, which is the whole point of the test."""

    files: dict[str, Any] = {}

    def __init__(self, hass, version, key) -> None:
        self._key = key

    async def async_load(self):
        return FakeStore.files.get(self._key)

    async def async_save(self, data) -> None:
        FakeStore.files[self._key] = data

    async def async_remove(self) -> None:
        FakeStore.files.pop(self._key, None)


class FakeUser:
    def __init__(self, user_id: str, name: str = "HAIM Manutenzione", is_active: bool = False) -> None:
        self.id = user_id
        self.name = name
        self.is_active = is_active
        self.is_owner = False
        self.refresh_tokens: dict[str, SimpleNamespace] = {}

    @property
    def tokens(self) -> list[str]:
        return list(self.refresh_tokens)


class FakeAuth:
    """Home Assistant's auth manager, including the behaviour D27 rests on:
    deactivating a user destroys every refresh token it had.

    `deactivate_revokes=False` is a Home Assistant release that changed that -
    the case the explicit revocation in `async_park` exists for.
    """

    def __init__(self, deactivate_revokes: bool = True) -> None:
        self.deactivate_revokes = deactivate_revokes
        # An installation that already has the account, created by a version
        # that did not write its id down: the name is how it is found once.
        self.user = FakeUser("maint-1")
        self.users: dict[str, FakeUser] = {self.user.id: self.user}
        self._minted = 0

    async def async_get_users(self):
        return list(self.users.values())

    async def async_get_user(self, user_id):
        return self.users.get(user_id)

    async def async_create_user(self, name, group_ids=None, local_only=None):
        user = FakeUser(f"maint-{len(self.users) + 1}", name=name, is_active=True)
        self.users[user.id] = user

        return user

    async def async_remove_user(self, user) -> None:
        self.users.pop(user.id, None)

    async def async_activate_user(self, user) -> None:
        user.is_active = True

    async def async_deactivate_user(self, user) -> None:
        user.is_active = False

        if self.deactivate_revokes:
            # This is the fact the whole model turns on, verified on the
            # laboratory and in Home Assistant's source.
            user.refresh_tokens.clear()

    async def async_create_refresh_token(self, user, **kwargs):
        self._minted += 1
        refresh_token = SimpleNamespace(id=f"refresh-{self._minted}", user=user)
        user.refresh_tokens[refresh_token.id] = refresh_token

        return refresh_token

    def async_remove_refresh_token(self, refresh_token) -> None:
        refresh_token.user.refresh_tokens.pop(refresh_token.id, None)

    def async_create_access_token(self, refresh_token) -> str:
        return "eyJ-un-token-di-prova"

    def awake(self) -> list[FakeUser]:
        return [user for user in self.users.values() if user.is_active]

    def keys(self) -> list[str]:
        return [token for user in self.users.values() for token in user.tokens]


class FakeHass:
    def __init__(self, deactivate_revokes: bool = True) -> None:
        self.auth = FakeAuth(deactivate_revokes)


class FakeApi:
    def __init__(self, down: bool = False) -> None:
        self.credentials: list[dict] = []
        self.down = down
        self.seen_at_delivery: list[dict] = []

    async def deliver_credentials(self, payload: dict) -> None:
        # What the house had already done by the time the token left it.
        self.seen_at_delivery.append({"on_disk": SESSION_KEY in FakeStore.files})

        if self.down:
            raise HaimApiError("the cloud is unreachable")

        self.credentials.append(payload)


@pytest.fixture(autouse=True)
def _fake_storage(monkeypatch):
    FakeStore.files = {}
    monkeypatch.setattr("custom_components.haim_agent.remote_access.Store", FakeStore)
    monkeypatch.setattr("custom_components.haim_agent.maintenance.Store", FakeStore)

    fired: list = []

    def track(hass, action, when):
        entry = (action, when)
        fired.append(entry)

        def cancel() -> None:
            if entry in fired:
                fired.remove(entry)

        return cancel

    monkeypatch.setattr("custom_components.haim_agent.remote_access.async_track_point_in_utc_time", track)

    return fired


def manager(
    base_url: str = "https://haim.example",
    hass: FakeHass | None = None,
    api: FakeApi | None = None,
) -> tuple[RemoteAccessManager, FakeHass, FakeApi]:
    hass = hass or FakeHass()
    api = api or FakeApi()

    return RemoteAccessManager(hass, api, site_uuid="lab", base_url=base_url), hass, api


def window(minutes: int = 60, **extra: Any) -> dict[str, Any]:
    return {
        "session_uuid": "11111111-1111-4111-8111-111111111111",
        "slug": "abcdefghijklmnopqrstuvwxyz",
        "relay_url": "https://tun.haim.example",
        "granted_minutes": minutes,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(),
        **extra,
    }


def deadline_of(remote: RemoteAccessManager) -> datetime:
    return datetime.fromisoformat(remote.session["expires_at"])


def minutes_from_now(when: datetime) -> float:
    return (when - datetime.now(timezone.utc)).total_seconds() / 60


async def test_it_wakes_the_account_and_hands_the_token_to_the_cloud() -> None:
    remote, hass, api = manager()

    result = await remote.async_open(window())

    assert result["opened"] is True
    assert hass.auth.user.is_active is True
    assert api.credentials[0]["session_uuid"] == "11111111-1111-4111-8111-111111111111"
    assert api.credentials[0]["token"]
    # Required by the contract: the maintenance account's id, never a person's.
    assert api.credentials[0]["ha_user_id"]
    assert api.credentials[0]["ha_user_id"] == (await remote._user.async_find()).id


async def test_closing_parks_the_account_and_takes_the_tokens_with_it() -> None:
    # Parking IS the revocation: one call that cannot half-succeed, and the
    # browser tab of whoever was inside dies at the same instant.
    remote, hass, api = manager()

    await remote.async_open(window())
    assert hass.auth.user.tokens

    await remote.async_close("closed_by_user")

    assert hass.auth.user.is_active is False
    assert hass.auth.user.tokens == []
    assert remote.is_open is False


async def test_the_deadline_survives_a_restart() -> None:
    """The whole reason it is on disk.

    The competing product keeps this in memory: a Home Assistant restart during
    a session loses the timer, and the window silently becomes unbounded.
    """
    remote, hass, api = manager()
    await remote.async_open(window(minutes=60))

    # A restart: a brand new manager, same disk, same Home Assistant.
    revived, _, _ = manager(hass=hass)
    await revived.async_restore()

    assert revived.is_open is True
    assert revived.session["session_uuid"] == "11111111-1111-4111-8111-111111111111"


async def test_a_restart_after_the_deadline_closes_immediately() -> None:
    # Home Assistant was restarted during a session and came back after it
    # should have ended. It must not resume.
    remote, hass, api = manager()
    await remote.async_open(window(minutes=60))
    assert hass.auth.user.is_active is True

    FakeStore.files[SESSION_KEY]["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

    revived, _, _ = manager(hass=hass)
    await revived.async_restore()

    assert revived.is_open is False
    assert hass.auth.user.is_active is False
    assert hass.auth.user.tokens == []


async def test_a_start_with_nothing_open_parks_the_account_anyway() -> None:
    # A crash mid-session must not leave an admin account awake in somebody's
    # house.
    remote, hass, _ = manager()
    hass.auth.user.is_active = True

    await remote.async_restore()

    assert hass.auth.user.is_active is False


async def test_a_restored_deadline_is_never_more_than_six_hours_away() -> None:
    # A deadline on disk that says otherwise was not written by this version,
    # or the clock of this house moved backwards while it was down.
    remote, hass, _ = manager()
    FakeStore.files[SESSION_KEY] = {
        "session_uuid": "11111111-1111-4111-8111-111111111111",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
    }

    await remote.async_restore()

    assert minutes_from_now(deadline_of(remote)) <= 360


async def test_it_refuses_a_window_that_is_already_over() -> None:
    remote, hass, api = manager()

    with pytest.raises(ValueError):
        await remote.async_open(window(minutes=-5))

    assert hass.auth.user.is_active is False
    assert api.credentials == []


async def test_it_refuses_a_relay_on_somebody_else_domain() -> None:
    # §11.4 point 3: a compromised backend must not be able to point this
    # house's tunnel wherever it likes. The competing product does not check.
    remote, hass, api = manager(base_url="https://haim.example")

    with pytest.raises(ValueError):
        await remote.async_open(window(relay_url="https://tun.attaccante.example"))

    assert hass.auth.user.is_active is False
    assert api.credentials == []


async def test_it_refuses_a_relay_that_is_not_https() -> None:
    remote, hass, api = manager()

    with pytest.raises(ValueError):
        await remote.async_open(window(relay_url="http://tun.haim.example"))

    assert api.credentials == []


async def test_it_accepts_a_relay_under_our_own_host() -> None:
    # `{slug}.tun.<host>` (§11.1): a different host by design, so this cannot
    # be an exact match - but it is under the enrolled host, exactly.
    remote, hass, api = manager(base_url="https://haim.example")

    await remote.async_open(window(relay_url="https://abc123.tun.haim.example"))

    assert hass.auth.user.is_active is True


@pytest.mark.parametrize(
    ("base_url", "relay", "accepted"),
    [
        # The bug: "the last two labels" of haim.co.uk are co.uk, so any
        # *.co.uk was ours - and on a Tailscale name, anybody's *.ts.net.
        ("https://haim.co.uk", "https://tun.attaccante.co.uk", False),
        ("https://panel.haim.co.uk", "https://tun.attaccante.co.uk", False),
        ("https://mac.tail1234.ts.net", "https://other.tail9999.ts.net", False),
        ("https://haim.co.uk", "https://abc.tun.haim.co.uk", True),
        ("https://haim.co.uk", "https://haim.co.uk", True),
        # A sibling is not under the enrolled host: the suffix is exact.
        ("https://panel.haim.example", "https://tun.haim.example", False),
        # A suffix match on the string, not on the labels, would take these.
        ("https://haim.example", "https://tun.nothaim.example", False),
        ("https://haim.example", "https://haim.example.attaccante.example", False),
        # An address is not a domain: only itself.
        ("https://10.0.0.5", "https://10.0.0.6", False),
        ("https://10.0.0.5", "https://x.10.0.0.5", False),
    ],
)
async def test_the_relay_must_be_under_exactly_our_host(base_url: str, relay: str, accepted: bool) -> None:
    remote, hass, api = manager(base_url=base_url)

    if accepted:
        await remote.async_open(window(relay_url=relay))
        assert remote.is_open is True
    else:
        with pytest.raises(ValueError):
            await remote.async_open(window(relay_url=relay))

        assert hass.auth.awake() == []
        assert api.credentials == []


async def test_it_arms_a_timer_for_the_deadline(_fake_storage) -> None:
    remote, hass, api = manager()

    await remote.async_open(window(minutes=30))

    assert len(_fake_storage) == 1
    _action, when = _fake_storage[0]
    assert when > datetime.now(timezone.utc)


async def test_the_timer_closes_the_window(_fake_storage) -> None:
    remote, hass, api = manager()

    await remote.async_open(window(minutes=30))

    action, _when = _fake_storage[0]
    await action(None)

    assert remote.is_open is False
    assert hass.auth.user.is_active is False


async def test_the_ticket_opens_the_door_once_and_only_inside_a_window() -> None:
    """Decision D29: no tunnel, so the door is the instance's own address and
    the ticket is what stands in front of it."""
    from custom_components.haim_agent.web import ticket_hash

    remote, hass, api = manager()

    assert remote.redeem("qualunque") is None, "chiuso: nessuna finestra aperta"

    await remote.async_open(window(ticket_hash=ticket_hash("un-ticket-vero")))

    assert remote.redeem("sbagliato") is None

    spent = remote.redeem("un-ticket-vero")
    assert spent is not None
    assert spent["token"] == "eyJ-un-token-di-prova"

    # And again, for as long as the window lasts: reopening a tab you closed by
    # accident must not mean asking the household for permission a second time.
    assert remote.redeem("un-ticket-vero") is not None


async def test_closing_the_window_takes_the_key_out_of_memory() -> None:
    from custom_components.haim_agent.web import ticket_hash

    remote, hass, api = manager()
    await remote.async_open(window(ticket_hash=ticket_hash("un-ticket-vero")))

    await remote.async_close("revoked_by_owner")

    assert remote.redeem("un-ticket-vero") is None
    assert hass.auth.user.is_active is False


async def test_a_restart_keeps_the_deadline_and_loses_the_key() -> None:
    """The safe half is the one that survives.

    The deadline is on disk, so the window still closes on time even if Home
    Assistant restarted in the middle. The key was in memory on purpose, so
    nobody can walk through the window any more - the installer asks again and
    the household says yes again.
    """
    from custom_components.haim_agent.web import ticket_hash

    remote, hass, api = manager()
    await remote.async_open(window(minutes=60, ticket_hash=ticket_hash("un-ticket-vero")))

    revived, _, _ = manager(hass=hass)
    await revived.async_restore()

    assert revived.is_open is True
    assert revived.redeem("un-ticket-vero") is None


# ------------------------------------------------------- D37, point 1: unload


async def test_disabling_the_integration_mid_window_parks_the_account(_fake_storage) -> None:
    """It used to only disarm the timer.

    Proved on 14/10/2026: two hours after the deadline the account was
    `is_active=True` with its refresh token alive, and the installer's websocket
    still open - because the only thing that would have closed it was the timer
    the unload had just cancelled.
    """
    remote, hass, api = manager()
    await remote.async_open(window(minutes=60))

    await remote.async_stop()

    assert hass.auth.awake() == []
    assert hass.auth.keys() == []
    assert _fake_storage == [], "no timer left behind to fire on a manager nobody holds"
    assert SESSION_KEY not in FakeStore.files
    assert remote.redeem("qualunque") is None


async def test_parking_revokes_every_key_even_if_deactivating_stops_doing_it() -> None:
    """D27 rests on a fact about Home Assistant: deactivating a user destroys its
    refresh tokens. A release that changes it must not leave a key behind."""
    remote, hass, api = manager(hass=FakeHass(deactivate_revokes=False))
    await remote.async_open(window(minutes=60))
    assert hass.auth.keys()

    await remote.async_close("closed_by_user")

    assert hass.auth.awake() == []
    assert hass.auth.keys() == []


async def test_removing_the_integration_deletes_the_account() -> None:
    remote, hass, api = manager()
    await remote.async_open(window(minutes=60))

    await async_remove_installation(hass, "lab", last=True)

    assert hass.auth.users == {}, "an uninstalled integration leaves no admin account behind"
    assert SESSION_KEY not in FakeStore.files
    assert MAINTENANCE_KEY not in FakeStore.files


async def test_removing_one_of_two_entries_parks_but_keeps_the_account() -> None:
    remote, hass, api = manager()
    await remote.async_open(window(minutes=60))

    await async_remove_installation(hass, "lab", last=False)

    assert list(hass.auth.users) == ["maint-1"]
    assert hass.auth.awake() == []
    assert hass.auth.keys() == []


# ------------------------------------------------ D37, point 2: the delivery


async def test_the_deadline_is_on_disk_and_counted_before_the_token_leaves(_fake_storage) -> None:
    remote, hass, api = manager()

    await remote.async_open(window(minutes=60))

    assert api.seen_at_delivery == [{"on_disk": True}]
    assert len(_fake_storage) == 1


async def test_a_cloud_that_fails_to_take_the_token_leaves_nothing_awake(_fake_storage) -> None:
    """It used to leave an awake account with a token, nothing on disk and no
    timer: a key nobody was counting down."""
    remote, hass, api = manager(api=FakeApi(down=True))

    with pytest.raises(HaimApiError):
        await remote.async_open(window(minutes=60))

    assert hass.auth.awake() == []
    assert hass.auth.keys() == []
    assert remote.is_open is False
    assert _fake_storage == []
    assert SESSION_KEY not in FakeStore.files


async def test_a_disk_that_fails_leaves_nothing_awake(monkeypatch, _fake_storage) -> None:
    async def broken(self, data) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(FakeStore, "async_save", broken)
    remote, hass, api = manager()

    with pytest.raises(OSError):
        await remote.async_open(window(minutes=60))

    assert hass.auth.awake() == []
    assert hass.auth.keys() == []
    assert api.credentials == [], "the token never left the house"
    assert _fake_storage == []


# ------------------------------------------- D37, point 3: a second window


async def test_a_second_window_revokes_everything_the_first_one_had(_fake_storage) -> None:
    """Chosen: park before reopening, rather than refuse.

    Refusing would lock the installer out after a restart: the restored window
    is still open but has no key, and the new one is the only way back in.
    """
    remote, hass, api = manager()
    await remote.async_open(window(minutes=60))
    first_key = hass.auth.keys()

    await remote.async_open(window(minutes=30, session_uuid="22222222-2222-4222-8222-222222222222"))

    assert len(first_key) == 1
    assert first_key[0] not in hass.auth.keys(), "the first token outlived its window"
    assert len(hass.auth.keys()) == 1
    assert len(_fake_storage) == 1, "one window, one timer"
    assert remote.session["session_uuid"] == "22222222-2222-4222-8222-222222222222"


async def test_a_key_left_behind_by_anything_else_dies_when_a_window_opens() -> None:
    remote, hass, api = manager()
    leftover = await hass.auth.async_create_refresh_token(hass.auth.user)
    hass.auth.user.is_active = True

    await remote.async_open(window(minutes=60))

    assert leftover.id not in hass.auth.keys()
    assert len(hass.auth.keys()) == 1


async def test_a_new_window_after_a_restart_replaces_the_keyless_one() -> None:
    remote, hass, api = manager()
    await remote.async_open(window(minutes=60))

    revived, _, _ = manager(hass=hass, api=api)
    await revived.async_restore()

    await revived.async_open(window(minutes=60, session_uuid="22222222-2222-4222-8222-222222222222"))

    assert revived.redeem("qualunque") is None
    assert len(hass.auth.keys()) == 1


# ------------------------------------ D37, points 4 and 6: the length of it


async def test_the_window_is_never_longer_than_six_hours() -> None:
    remote, hass, api = manager()

    await remote.async_open(window(minutes=60 * 24))

    assert minutes_from_now(deadline_of(remote)) <= 360


async def test_the_window_lasts_what_was_granted_whatever_the_cloud_deadline_says() -> None:
    """Measured from arrival on this house's clock (point 6).

    A house whose clock runs two hours behind reads a cloud deadline as two
    hours further away than it is. The length that was granted does not move.
    """
    remote, hass, api = manager()
    behind = datetime.now(timezone.utc) + timedelta(minutes=30 + 120)

    await remote.async_open(window(minutes=30, expires_at=behind.isoformat()))

    assert 29 <= minutes_from_now(deadline_of(remote)) <= 30


async def test_the_cloud_deadline_can_only_shorten_the_window() -> None:
    remote, hass, api = manager()
    sooner = datetime.now(timezone.utc) + timedelta(minutes=10)

    await remote.async_open(window(minutes=60, expires_at=sooner.isoformat()))

    assert minutes_from_now(deadline_of(remote)) <= 10


async def test_a_command_without_granted_minutes_is_still_capped() -> None:
    remote, hass, api = manager()
    data = window(minutes=60 * 10)
    data.pop("granted_minutes")

    await remote.async_open(data)

    assert minutes_from_now(deadline_of(remote)) <= 360


class RecordingChannel:
    def __init__(self) -> None:
        self.reports: list[tuple[str, str, str | None]] = []

    async def async_receive(self):
        return None

    async def async_report(self, command_uuid, status, error=None, data=None) -> None:
        self.reports.append((command_uuid, status, error))


async def test_a_let_them_in_that_arrives_after_its_deadline_is_not_carried_out() -> None:
    from custom_components.haim_agent.command_channel import CommandDispatcher
    from custom_components.haim_agent.commands import register_supported_commands

    remote, hass, api = manager()
    channel = RecordingChannel()
    dispatcher = CommandDispatcher(channel)
    register_supported_commands(dispatcher, hass, SimpleNamespace(), None, remote)

    late = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

    await dispatcher.async_dispatch(
        {"action": "connection_approved", "command_uuid": "c-1", "expires_at": late, "data": window()}
    )

    assert hass.auth.awake() == []
    assert api.credentials == []
    assert channel.reports[-1][:2] == ("c-1", "failed")

    # And on time, it is.
    on_time = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()

    await dispatcher.async_dispatch(
        {"action": "connection_approved", "command_uuid": "c-2", "expires_at": on_time, "data": window()}
    )

    assert channel.reports[-1][:2] == ("c-2", "succeeded")
    assert hass.auth.awake() != []


async def test_other_commands_are_not_refused_for_arriving_late() -> None:
    # Opt-in per action: a house with a wrong clock must not refuse everything.
    from custom_components.haim_agent.command_channel import CommandDispatcher

    channel = RecordingChannel()
    dispatcher = CommandDispatcher(channel)

    async def handler(_data):
        return {}

    dispatcher.register("self_test", handler)
    late = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

    await dispatcher.async_dispatch({"action": "self_test", "command_uuid": "c-3", "expires_at": late})

    assert channel.reports[-1][:2] == ("c-3", "succeeded")


# ---------------------------------------------- D37, point 5: by id, not name


async def test_a_renamed_account_is_still_the_one_that_is_parked() -> None:
    """Looked up by name, a renamed account was never parked again - and the
    next window created a second one next to it."""
    remote, hass, api = manager()
    await remote.async_open(window(minutes=60))
    await remote.async_close("closed_by_user")

    hass.auth.user.name = "Il tecnico"

    await remote.async_open(window(minutes=60))
    assert list(hass.auth.users) == ["maint-1"], "a second account was created"
    assert hass.auth.user.is_active is True

    await remote.async_close("closed_by_user")
    assert hass.auth.awake() == []
    assert hass.auth.keys() == []


async def test_the_id_is_written_down_and_the_name_no_longer_matters() -> None:
    from custom_components.haim_agent.maintenance import MaintenanceUser

    hass = FakeHass()

    found = await MaintenanceUser(hass).async_find()
    assert found is hass.auth.user
    assert FakeStore.files[MAINTENANCE_KEY] == {"user_id": "maint-1"}

    # Somebody creates an account with the same name: it is not ours.
    impostor = FakeUser("someone-else", name="HAIM Manutenzione", is_active=True)
    hass.auth.users = {"someone-else": impostor, **hass.auth.users}
    hass.auth.user.name = "Rinominato"

    assert await MaintenanceUser(hass).async_find() is hass.auth.user

    await MaintenanceUser(hass).async_park()
    assert impostor.is_active is True, "somebody else's account was parked"


async def test_a_new_account_has_its_id_written_down_when_it_is_created() -> None:
    from custom_components.haim_agent.maintenance import MaintenanceUser

    hass = FakeHass()
    hass.auth.users = {}

    created = await MaintenanceUser(hass).async_ensure()

    assert FakeStore.files[MAINTENANCE_KEY] == {"user_id": created.id}
    assert created.is_active is False, "created and immediately parked"


# ----------------------------------------------------------- the final proof


BRANCHES = [
    "nothing_happens",
    "home_assistant_restarted",
    "home_assistant_restarted_after_the_deadline",
    "integration_disabled",
    "cloud_unreachable_at_delivery",
    "cloud_unreachable_after_opening",
    "all_of_them_in_a_row",
]


@pytest.mark.parametrize("branch", BRANCHES)
async def test_whatever_happens_at_the_deadline_the_account_is_off_and_has_no_keys(
    branch: str, monkeypatch, _fake_storage
) -> None:
    """Window open, then Home Assistant restarted, the integration disabled,
    the cloud unreachable - in every branch, at the deadline the maintenance
    account is off and has zero refresh tokens. This is the promise of D27 and
    D37, written as a test."""
    import custom_components.haim_agent.remote_access as remote_access

    hass = FakeHass()
    api = FakeApi(down=branch == "cloud_unreachable_at_delivery")
    remote, _, _ = manager(hass=hass, api=api)

    try:
        await remote.async_open(window(minutes=60))
    except HaimApiError:
        assert branch == "cloud_unreachable_at_delivery"

    deadline = datetime.now(timezone.utc) + timedelta(minutes=60)

    def at_the_deadline() -> None:
        monkeypatch.setattr(remote_access, "_now", lambda: deadline + timedelta(seconds=1))

    if branch == "home_assistant_restarted":
        remote, _, _ = manager(hass=hass, api=api)
        _fake_storage.clear()  # the old process and its timers are gone
        await remote.async_restore()

    elif branch == "home_assistant_restarted_after_the_deadline":
        _fake_storage.clear()
        at_the_deadline()
        remote, _, _ = manager(hass=hass, api=api)
        await remote.async_restore()

    elif branch == "integration_disabled":
        await remote.async_stop()

    elif branch == "cloud_unreachable_after_opening":
        api.down = True

    elif branch == "all_of_them_in_a_row":
        # Restarted...
        remote, _, _ = manager(hass=hass, api=api)
        _fake_storage.clear()
        await remote.async_restore()
        # ...disabled...
        await remote.async_stop()
        # ...enabled again with the cloud unreachable.
        api.down = True
        remote, _, _ = manager(hass=hass, api=api)
        await remote.async_restore()

    # The deadline: every timer still armed fires.
    at_the_deadline()

    for action, _when in list(_fake_storage):
        await action(None)

    assert hass.auth.awake() == [], f"{branch}: an account is awake after the deadline"
    assert hass.auth.keys() == [], f"{branch}: a key outlived the window"
    assert remote.redeem("qualunque") is None


async def test_home_assistant_is_told_to_close_the_window_on_unload() -> None:
    """The wiring, which no other test sees: what Home Assistant calls when the
    integration is disabled or reloaded is the method that parks the account."""
    import inspect

    import custom_components.haim_agent as component

    source = inspect.getsource(component.async_setup_entry)

    assert "entry.async_on_unload(remote.async_stop)" in source
    assert not hasattr(RemoteAccessManager, "stop"), "the old stop() only disarmed the timer"


async def test_deleting_the_last_entry_removes_the_account_through_home_assistant() -> None:
    import custom_components.haim_agent as component

    hass = FakeHass()
    entry = SimpleNamespace(entry_id="only", data={"site_uuid": "lab"})
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])

    remote, _, _ = manager(hass=hass)
    await remote.async_open(window(minutes=60))

    await component.async_remove_entry(hass, entry)

    assert hass.auth.users == {}


# -------------------------------- close_session names its window (prompt 05, 6-7)

WINDOW_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WINDOW_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class OutcomeChannel:
    def __init__(self) -> None:
        self.reports: list[tuple[str, str, Any, Any]] = []

    async def async_receive(self):
        return None

    async def async_report(self, command_uuid, status, error=None, data=None) -> None:
        self.reports.append((command_uuid, status, error, data))


def _dispatcher(remote, hass):
    from custom_components.haim_agent.command_channel import CommandDispatcher
    from custom_components.haim_agent.commands import register_supported_commands

    channel = OutcomeChannel()
    dispatcher = CommandDispatcher(channel)
    register_supported_commands(dispatcher, hass, SimpleNamespace(), None, remote)

    return dispatcher, channel


def _close(command_uuid: str, **data: Any) -> dict[str, Any]:
    return {
        "action": "close_session",
        "command_uuid": command_uuid,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
        "data": data,
    }


async def test_a_late_close_for_the_previous_window_does_not_end_the_current_one() -> None:
    """The panel no longer folds close commands together. A close for window A
    that reaches the house after window B opened used to end B."""
    remote, hass, api = manager()
    dispatcher, channel = _dispatcher(remote, hass)

    await remote.async_open(window(session_uuid=WINDOW_B))

    await dispatcher.async_dispatch(_close("c-a", session_uuid=WINDOW_A, close_reason="revoked_by_owner"))

    assert remote.is_open is True
    assert hass.auth.awake() != []
    assert channel.reports[-1][:2] == ("c-a", "succeeded")
    assert channel.reports[-1][3] == {"closed": False, "ignored": "not_the_open_session", "session_uuid": WINDOW_A}

    await dispatcher.async_dispatch(_close("c-b", session_uuid=WINDOW_B, close_reason="revoked_by_owner"))

    assert remote.is_open is False
    assert hass.auth.awake() == []
    assert hass.auth.keys() == []
    assert channel.reports[-1][3] == {"closed": True, "reason": "revoked_by_owner"}


async def test_a_close_with_nothing_open_still_parks_the_account() -> None:
    remote, hass, _ = manager()
    dispatcher, channel = _dispatcher(remote, hass)
    hass.auth.user.is_active = True

    await dispatcher.async_dispatch(_close("c-1", session_uuid=WINDOW_A, close_reason="expired"))

    assert hass.auth.awake() == []
    assert channel.reports[-1][3]["closed"] is True


async def test_a_close_from_a_panel_that_names_no_window_closes_what_is_open() -> None:
    remote, hass, _ = manager()
    dispatcher, _channel = _dispatcher(remote, hass)

    await remote.async_open(window(session_uuid=WINDOW_B))
    await dispatcher.async_dispatch(_close("c-1", close_reason="closed_by_user"))

    assert remote.is_open is False
    assert hass.auth.awake() == []


async def test_a_colleague_removed_from_the_organisation_closes_the_window() -> None:
    """`user_removed`, new in the panel. The reason is shown nowhere in the
    house - only the local log and the result carry it - so there is no text to
    translate: it must simply close, and say why."""
    remote, hass, _ = manager()
    dispatcher, channel = _dispatcher(remote, hass)

    await remote.async_open(window(session_uuid=WINDOW_B))
    await dispatcher.async_dispatch(_close("c-1", session_uuid=WINDOW_B, close_reason="user_removed"))

    assert remote.is_open is False
    assert hass.auth.keys() == []
    assert channel.reports[-1][3] == {"closed": True, "reason": "user_removed"}


async def test_a_close_reason_is_a_word_not_whatever_the_panel_sends() -> None:
    remote, hass, _ = manager()
    dispatcher, channel = _dispatcher(remote, hass)

    await remote.async_open(window(session_uuid=WINDOW_B))
    await dispatcher.async_dispatch(_close("c-1", session_uuid=WINDOW_B, close_reason="<b>Giulia</b>\nrevoked"))

    assert remote.is_open is False
    assert channel.reports[-1][3]["reason"] == "closed_by_user"


def test_no_close_reason_is_shown_in_the_house() -> None:
    """If a reason ever gets a text in the house, `user_removed` needs one in
    every language: this is where that would show."""
    import json
    from pathlib import Path

    here = Path(__file__).resolve().parents[1] / "custom_components" / "haim_agent"

    for name in ("strings.json", "translations/en.json", "translations/it.json"):
        text = json.dumps(json.loads((here / name).read_text()))

        for reason in ("revoked_by_owner", "closed_by_user", "user_removed", "agent_lost"):
            assert reason not in text, f"{name} shows {reason}: translate user_removed too"
