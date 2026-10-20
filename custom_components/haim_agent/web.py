"""The page that hands the key to the browser, served by the house itself.

Decision D29. There is no tunnel in this path and no relay: the installer's
browser goes straight to the instance's own public address, and the page that
puts the credential into it is served by THIS component, on Home Assistant's own
origin.

WHY IT HAS TO BE SERVED HERE. The Home Assistant frontend keeps its session in
`localStorage`, and `localStorage` belongs to an origin. A page on our panel
cannot write into the instance's storage - browsers exist to stop exactly that.
A page served by the component is same-origin with the frontend by definition,
whatever way the household exposed it: a port forward, a dynamic DNS name, Nabu
Casa, a reverse proxy. We do not have to know or care.

WHAT PROTECTS IT, because this endpoint is open to the internet:

* It answers to POST only, and only with a ticket that matches the one the cloud
  sent over the authenticated channel when the window opened. We hold the
  sha256, never the ticket.
* The ticket is worth exactly as long as the window and not a second more
  (D30). It is not spent on first use: the installer who closes the tab, or
  comes back an hour into a two-hour window, walks in again with the same
  ticket. What ends it is the window ending.
* It answers nothing at all unless a window is open. Outside one there is no
  credential to hand over - the maintenance account is parked and every token it
  had has ceased to exist (D27).
* Every failure looks the same: 404. No ticket, wrong ticket, closed window, no
  session - one answer, because telling them apart is how somebody works out
  which of the four they are hitting. Which one it was is in the log of this
  house, where the household and its installer can read it and nobody else can.
* And that 404 is the one Home Assistant gives for a path that does not exist,
  byte for byte, on every method (D37). An empty body, or a 405 on GET, was
  enough for a scanner to tell that this house runs HAIM - and so that it has
  an administrator account that wakes up.
* Knocking is not an event. Every refusal used to be a WARNING, so anybody on
  the internet could fill the household's log - and teach them to ignore it.
  Refusals are DEBUG, and counted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

_LOGGER = logging.getLogger(__name__)

PATH = "/api/haim/open"


class HaimOpenView(HomeAssistantView):
    """POST here with a valid ticket and you are inside, for the window."""

    url = PATH
    name = "api:haim:open"

    # The ticket IS the authentication. Requiring a Home Assistant token here
    # would defeat the whole point: the person arriving does not have one, which
    # is precisely why they are being handed one.
    requires_auth = False

    def __init__(self, remote) -> None:
        self._remote = remote
        # How many knocks were refused since Home Assistant started. In the
        # debug line of each refusal, so that whoever turns debug on to find
        # out why an installer could not get in also sees whether the door is
        # being tried by somebody else.
        self.refused = 0

    async def post(self, request: web.Request) -> web.Response:
        try:
            data = await request.post()
        except Exception:  # noqa: BLE001
            return self._refuse("the body could not be read")

        ticket = data.get("ticket")

        if not isinstance(ticket, str) or not ticket:
            return self._refuse("no ticket in the request")

        session = await self._remote.async_redeem(ticket)

        if session is None:
            # The reason is in the log of THIS house and nowhere else, at debug
            # level. The answer on the wire stays one indistinguishable 404 -
            # that part is for whoever is knocking. This part is for whoever
            # has to work out, a week from now, why an installer could not get
            # in.
            return self._refuse(f"ticket {fingerprint(ticket)} - see the line above for which check failed")

        _LOGGER.info(
            "HAIM door: opened with ticket %s, window ends at %s",
            fingerprint(ticket),
            _readable(session["expires_at_ms"]),
        )

        return web.Response(
            text=_page(session["token"], session["expires_at_ms"]),
            content_type="text/html",
            headers={
                # The address of somebody's home. It does not go into a search
                # index, a cache, or the Referer of every request the frontend
                # then makes.
                "Referrer-Policy": "no-referrer",
                "X-Robots-Tag": "noindex, nofollow",
                "Cache-Control": "no-store",
            },
        )


    # Every other method answers what Home Assistant answers for a path it
    # does not have. A 405 here would say "something lives at this address".
    # Not OPTIONS: Home Assistant's CORS layer already answers it on every
    # route, and a view that declares its own makes register_view fail, which
    # takes the whole integration down with it.
    async def get(self, request: web.Request) -> web.Response:
        return self._refuse("GET")

    async def head(self, request: web.Request) -> web.Response:
        return self._refuse("HEAD")

    async def put(self, request: web.Request) -> web.Response:
        return self._refuse("PUT")

    async def patch(self, request: web.Request) -> web.Response:
        return self._refuse("PATCH")

    async def delete(self, request: web.Request) -> web.Response:
        return self._refuse("DELETE")

    def _refuse(self, why: str) -> web.Response:
        self.refused += 1

        _LOGGER.debug("HAIM door: refused (%s), %d refused since start", why, self.refused)

        return _nothing()


def _readable(expires_at_ms: int) -> str:
    """A time somebody can compare to their watch.

    Epoch milliseconds in a log line are a small tax on every person who ever
    has to read one, paid at the worst moment.
    """
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(expires_at_ms / 1000))


def _nothing() -> web.Response:
    """One answer for every failure, and it is the answer of a path that is not there.

    Exactly what Home Assistant's router sends for an unknown path: aiohttp's
    own HTTPNotFound, `404: Not Found` in text/plain. Built from the exception
    rather than copied from it, so that an aiohttp release that changes the
    wording changes it here too.
    """
    missing = web.HTTPNotFound()

    return web.Response(status=missing.status, text=missing.text, content_type=missing.content_type)


def _page(token: str, expires_at_ms: int) -> str:
    """The shape the frontend itself writes, verified against a real instance.

    `expires` is what decides whether the frontend tries to refresh, and a
    refresh would fail: a long-lived token has no refresh token behind it. It is
    set to the end of the window, so the attempt never happens - and when the
    window does end, the account is parked and the token stops existing anyway,
    which is what actually closes the session.
    """
    remaining = max(60, (expires_at_ms - int(time.time() * 1000)) // 1000)

    tokens = {
        "access_token": token,
        "token_type": "Bearer",
        # The frontend only wants this field to exist. It never reaches for it.
        "refresh_token": token,
        "expires_in": remaining,
        "expires": expires_at_ms,
        # hassUrl and clientId are deliberately absent: the browser fills them
        # in from `location.origin`. See the note in the page.
        "ha_auth_provider": "homeassistant",
    }

    # ONE level of encoding, and the page uses it as a literal. It used to be
    # dumped twice and parsed twice, which is a SyntaxError on the second parse:
    # the object is not a string. The try/except around it turned that into a
    # login page with no explanation. Fewer moving parts is the fix; the check
    # in the page is the seatbelt.
    #
    # `<` is escaped because the payload sits inside a <script>: a token can be
    # any bytes, and "</script>" appearing in one would end the block early.
    blob = json.dumps(tokens).replace("<", "\\u003c")

    return _HTML.replace("__TOKENS__", blob)


_HTML = """<!doctype html>
<meta charset="utf-8">
<meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex, nofollow">
<title>HAIM</title>
<body>
<script>
// The origin is taken HERE and not on the server, and it is the difference
// between working and landing on a login page.
//
// The frontend refuses a stored session whose `hassUrl` does not match where it
// is served from. Home Assistant behind a reverse proxy - Cloudflare, nginx,
// Nabu Casa - sees the request arrive as plain http, so a server-side guess
// writes `http://...` while the browser is on `https://...`. Found on a real
// instance behind Cloudflare, and the only symptom is a login screen.
//
// `location.origin` cannot be wrong: it IS the origin this page is being read
// from, which is by definition the one the frontend will run on.
//
// It reads back what it wrote before going anywhere. Silently failing to store
// the session and redirecting anyway is a login page with no explanation, which
// is exactly what the first version of this did.
var ok = false;

try {
  var tokens = __TOKENS__;
  tokens.hassUrl = location.origin;
  tokens.clientId = location.origin + "/";
  localStorage.setItem("hassTokens", JSON.stringify(tokens));
  ok = !!localStorage.getItem("hassTokens");
} catch (e) {}

if (ok) {
  location.replace("/");
} else {
  document.getElementById("m").hidden = false;
}
</script>
<p id="m" hidden>Questo browser non ha potuto memorizzare la sessione. Succede in
navigazione privata o con i dati dei siti bloccati: riprova in una finestra
normale.</p>
<noscript><a href="/">Continua</a></noscript>
</body>
"""


def fingerprint(secret: str) -> str:
    """Enough to match two log lines, useless to anybody who reads one.

    The first eight characters of the sha256. The panel logs the same eight for
    the same ticket, which is what makes "the house refused the ticket the panel
    issued" a fact instead of a guess - without either side writing a live
    credential into a log file.
    """
    return ticket_hash(secret)[:8]


def ticket_hash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode()).hexdigest()


def ticket_matches(ticket: str, expected_hash: str) -> bool:
    """Constant time, because the alternative leaks how much of it matched."""
    return hmac.compare_digest(ticket_hash(ticket), expected_hash)
