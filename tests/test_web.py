"""The door: the page the installer's browser knocks on.

It is open to the internet, on the instance's own address, so what is tested is
mostly what it REFUSES.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.haim_agent.web import HaimOpenView, ticket_hash


class FakeRequest:
    def __init__(self, form: dict[str, Any], scheme: str = "https", host: str = "haim.example") -> None:
        self._form = form
        self.scheme = scheme
        self.host = host

    async def post(self):
        return self._form


class FakeRemote:
    """Only the half the view uses: spend a ticket, get the key."""

    def __init__(self, ticket: str | None = None, minutes: int = 60) -> None:
        self._hash = ticket_hash(ticket) if ticket else None
        self._minutes = minutes

    async def async_redeem(self, ticket: str):
        if self._hash is None:
            return None

        from custom_components.haim_agent.web import ticket_matches

        if not ticket_matches(ticket, self._hash):
            return None

        expires = datetime.now(timezone.utc) + timedelta(minutes=self._minutes)

        return {"token": "eyJ-la-chiave-della-finestra", "expires_at_ms": int(expires.timestamp() * 1000)}


async def test_a_valid_ticket_hands_over_the_key() -> None:
    view = HaimOpenView(FakeRemote("un-ticket-vero"))

    response = await view.post(FakeRequest({"ticket": "un-ticket-vero"}))

    assert response.status == 200
    assert "eyJ-la-chiave-della-finestra" in response.text
    assert 'location.replace("/")' in response.text


async def test_the_token_reaches_the_page_as_something_javascript_can_use() -> None:
    """The bug this exists for cost an evening on a real instance.

    The payload was encoded twice and parsed twice. One JSON.parse gives the
    object; the second one throws, because an object is not a string. The
    try/catch around it swallowed the throw, the page redirected anyway, and
    Home Assistant found an empty localStorage and showed a login form - with
    nothing anywhere saying what had gone wrong.

    Asserting on the source text of the page cannot catch that, and did not.
    What catches it is decoding what the page actually embeds: exactly one level
    of encoding, and what comes out is the object, not a string of it.
    """
    remote = FakeRemote("un-ticket-vero")
    view = HaimOpenView(remote)

    response = await view.post(FakeRequest({"ticket": "un-ticket-vero"}))
    blob = re.search(r"var tokens = (.+);", response.text).group(1)

    decoded = json.loads(blob)

    assert isinstance(decoded, dict), "encoded twice: the page would throw and say nothing"
    assert decoded["access_token"] == "eyJ-la-chiave-della-finestra"
    assert response.text.count("JSON.parse") == 0


async def test_it_does_not_send_the_browser_on_if_the_session_was_not_stored() -> None:
    """A browser that refuses localStorage - private window, site data blocked -
    must be told so, not sent to a login page that explains nothing."""
    remote = FakeRemote("un-ticket-vero")
    view = HaimOpenView(remote)

    page = (await view.post(FakeRequest({"ticket": "un-ticket-vero"}))).text

    # The redirect happens only after reading back what was written.
    assert 'ok = !!localStorage.getItem("hassTokens")' in page
    assert page.index("ok = !!localStorage") < page.index('location.replace("/")')
    assert "if (ok) {" in page


async def test_the_origin_is_taken_from_the_browser_and_never_from_the_server() -> None:
    """The bug this exists to prevent, found on a real instance behind Cloudflare.

    The frontend refuses a stored session whose `hassUrl` does not match where it
    is served from. Behind a reverse proxy Home Assistant sees the request as
    plain http, so guessing on the server writes `http://...` while the browser
    is on `https://...` - and the only symptom is a login screen with no
    explanation.
    """
    view = HaimOpenView(FakeRemote("un-ticket-vero"))

    # A request that looks like plain http, which is what a proxied one looks
    # like from inside Home Assistant.
    response = await view.post(FakeRequest({"ticket": "un-ticket-vero"}, scheme="http", host="haim.example"))

    # Nothing about the origin was written server side...
    assert "http://haim.example" not in response.text

    # ...and the page takes it from where it is actually being read.
    assert "location.origin" in response.text
    assert 'tokens.hassUrl = location.origin' in response.text


async def test_a_ticket_works_for_as_long_as_the_window_does() -> None:
    """Not once: for the window.

    One-time was protecting against the leak of something that never leaves the
    installer's browser, and that dies with the window anyway. What it cost was
    real - reopening a tab closed by accident meant asking the household again.
    """
    remote = FakeRemote("un-ticket-vero")
    view = HaimOpenView(remote)

    assert (await view.post(FakeRequest({"ticket": "un-ticket-vero"}))).status == 200
    assert (await view.post(FakeRequest({"ticket": "un-ticket-vero"}))).status == 200


async def test_every_failure_looks_the_same() -> None:
    """No window, no ticket, wrong ticket - one answer, and it is the answer of
    a path that is not there. Telling them apart is how somebody works out which
    of the three they are hitting."""
    closed = HaimOpenView(FakeRemote(None))
    open_window = HaimOpenView(FakeRemote("un-ticket-vero"))

    assert (await closed.post(FakeRequest({"ticket": "qualunque"}))).status == 404
    assert (await open_window.post(FakeRequest({"ticket": "sbagliato"}))).status == 404
    assert (await open_window.post(FakeRequest({}))).status == 404
    assert (await open_window.post(FakeRequest({"ticket": ""}))).status == 404


async def test_it_does_not_leak_the_address_of_somebody_home() -> None:
    view = HaimOpenView(FakeRemote("un-ticket-vero"))

    response = await view.post(FakeRequest({"ticket": "un-ticket-vero"}))

    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "noindex" in response.headers["X-Robots-Tag"]
    assert response.headers["Cache-Control"] == "no-store"


async def test_the_view_needs_no_home_assistant_token() -> None:
    # The ticket IS the authentication. Requiring a Home Assistant token would
    # defeat the point: the person arriving does not have one, which is exactly
    # why they are being handed one.
    assert HaimOpenView.requires_auth is False
    assert HaimOpenView.url == "/api/haim/open"


# ----------------------------------------------- D37, point 7: unrecognisable


def _home_assistant_404():
    """What Home Assistant's router answers for a path it does not have."""
    from aiohttp import web

    missing = web.HTTPNotFound()

    return missing.status, missing.text, missing.content_type


async def test_a_refusal_is_byte_for_byte_the_404_of_a_path_that_does_not_exist() -> None:
    """An empty body was enough to tell this door from a missing path - and so
    that this house runs HAIM, with an admin account that wakes up."""
    view = HaimOpenView(FakeRemote("un-ticket-vero"))

    for form in ({"ticket": "sbagliato"}, {}, {"ticket": ""}):
        response = await view.post(FakeRequest(form))

        assert (response.status, response.text, response.content_type) == _home_assistant_404()


@pytest.mark.parametrize("method", ["get", "head", "put", "patch", "delete"])
async def test_every_other_method_is_a_404_too_not_a_405(method: str) -> None:
    # Answering only POST made aiohttp send 405 on GET: "something lives here".
    view = HaimOpenView(FakeRemote("un-ticket-vero"))

    response = await getattr(view, method)(FakeRequest({}))

    assert (response.status, response.text, response.content_type) == _home_assistant_404()


async def test_knocking_is_counted_at_debug_and_never_warned_about(caplog) -> None:
    """Anybody on the internet could fill the household's log with warnings."""
    import logging

    caplog.set_level(logging.DEBUG)
    view = HaimOpenView(FakeRemote("un-ticket-vero"))

    await view.post(FakeRequest({"ticket": "sbagliato"}))
    await view.post(FakeRequest({}))
    await view.get(FakeRequest({}))

    assert view.refused == 3
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
    assert "3 refused since start" in caplog.records[-1].getMessage()


async def test_the_house_side_of_a_refusal_is_not_a_warning_either(caplog) -> None:
    import logging

    from custom_components.haim_agent.remote_access import RemoteAccessManager

    caplog.set_level(logging.DEBUG)
    remote = RemoteAccessManager.__new__(RemoteAccessManager)
    remote._session = None
    remote._token = None
    remote._ticket_hash = None

    assert await remote.async_redeem("qualunque") is None
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
