"""The HTTP client the component uses to talk to HAIM.

Everything about this file is shaped by one fact: it runs in somebody's house
and we cannot reach it. So it is conservative about what it treats as fatal,
generous about what it ignores, and it never, ever weakens transport security.

There is no option to skip certificate verification. Not a flag, not an
environment variable, not a debug branch. The token that identifies a customer's
instance travels on this channel, and a switch that has to be remembered before
going to production is a switch that eventually is not. In development the
answer is to make the local certificate authority trusted in the environment,
which is a change to the machine and not to this code.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

from .const import (
    API_PREFIX,
    HEADER_CONFIG_VERSION,
    HEADER_SERVER_TIME,
    HEADER_STATE_HASH,
    HEADER_TOKEN_ROTATE,
)

_LOGGER = logging.getLogger(__name__)


class HaimApiError(Exception):
    """Something went wrong that the caller has to decide about."""


class PairingRejected(HaimApiError):
    """The pairing code was expired, already used, or never existed."""


class TokenRevoked(HaimApiError):
    """Our key is no longer accepted. Stop trying and tell the household."""


class SiteInactive(HaimApiError):
    """The instance is archived or the plan lapsed. Pause and come back later."""


class CertificateRejected(HaimApiError):
    """The server's certificate did not verify.

    Its own type because it needs its own answer on screen. "Cannot connect" is
    what somebody sees when they have mistyped an address; this is what they see
    when the address is right and the certificate is not, and the two lead to
    completely different next steps. What it never leads to is turning
    verification off.
    """


class RateLimited(HaimApiError):
    """The panel said 429: this kind of call waits until `retry_after` has passed.

    A HaimApiError like a network failure, on purpose: every caller already
    treats that as "not now, nothing lost" - the event queue keeps its events
    and counts no strike, the poll tries again later.
    """

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Rate limited by the panel; waiting {retry_after:.0f}s.")
        self.retry_after = retry_after


# When the panel says 429 without a usable Retry-After.
RATE_LIMIT_DEFAULT_SECONDS = 60

# The longest a single 429 can keep one kind of call quiet. The panel's limits
# are per minute (and ten minutes for an address that failed authentication);
# a Retry-After of a day is a mistake somewhere, and a poll that obeyed it would
# leave a consent request unanswered until tomorrow.
RATE_LIMIT_MAX_SECONDS = 900


def _retry_after(value: str | None) -> float:
    """Seconds to wait, from a Retry-After header: seconds or an HTTP date."""
    if not value:
        return RATE_LIMIT_DEFAULT_SECONDS

    value = value.strip()

    try:
        seconds = float(int(value))
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return RATE_LIMIT_DEFAULT_SECONDS

        if moment is None:
            return RATE_LIMIT_DEFAULT_SECONDS

        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)

        seconds = (moment - datetime.now(timezone.utc)).total_seconds()

    return min(max(seconds, 1.0), RATE_LIMIT_MAX_SECONDS)


def _limit_group(path: str) -> str:
    """Which limit a call counts against. The panel keeps one per endpoint, so a
    429 on /events must not silence /poll, which carries consent requests."""
    return path.strip("/").split("/", 1)[0]


@dataclass(slots=True)
class ApiResponse:
    """One answer, plus the headers that arrive on every single one."""

    status: int
    data: dict[str, Any] | None
    server_time: str | None = None
    config_version: str | None = None
    rotated_key: str | None = None


class HaimApi:
    """A thin, deliberately dull client. No retries, no state, no cleverness.

    Retrying is the caller's decision, because only the caller knows whether the
    thing being retried is a heartbeat that can simply be skipped or an event
    queue that must not be lost.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        agent_key: str | None = None,
    ) -> None:
        self._session = session
        self._base = base_url.rstrip("/") + API_PREFIX
        self._agent_key = agent_key
        # Per kind of call: the monotonic moment before which it is not sent.
        self._quiet_until: dict[str, float] = {}

    @property
    def agent_key(self) -> str | None:
        return self._agent_key

    def with_key(self, agent_key: str) -> None:
        """Adopt a rotated key. Called when the cloud hands us a new one."""
        self._agent_key = agent_key

    async def enroll(self, payload: dict[str, Any]) -> ApiResponse:
        """Trade a pairing code for a key. The only unauthenticated call."""
        response = await self._request("POST", "/enroll", json=payload, authenticated=False)

        if response.status == 410:
            raise PairingRejected("The pairing code is expired, already used, or wrong.")

        if response.status != 201:
            raise HaimApiError(f"Enrolment refused with status {response.status}.")

        return response

    async def send_state(self, payload: dict[str, Any], state_hash: str) -> ApiResponse:
        """Send telemetry. 204 is the normal answer and means nothing changed."""
        return await self._request(
            "POST", "/state", json=payload, headers={HEADER_STATE_HASH: state_hash}
        )

    async def send_events(self, events: list[dict[str, Any]]) -> ApiResponse:
        """Hand over a batch of transitions. Idempotent on the far side."""
        return await self._request("POST", "/events", json={"events": events})

    async def poll(self) -> ApiResponse:
        """Ask for a command. 204 means there is nothing to do."""
        return await self._request("GET", "/poll")

    async def report_command(self, command_uuid: str, payload: dict[str, Any]) -> ApiResponse:
        """Say what became of a command. Silence is treated as failure up there."""
        return await self._request("POST", f"/commands/{command_uuid}/result", json=payload)

    async def deliver_credentials(self, payload: dict[str, Any]) -> ApiResponse:
        """The token for THIS window, minted a moment ago (contract 1.1.0).

        Not a registration: a delivery. It is minted per session and dies with
        it, so there is nothing to renew and nothing kept anywhere (D27).
        """
        return await self._request("POST", "/credentials", json=payload)

    async def decide_access_request(self, request_uuid: str, payload: dict[str, Any]) -> ApiResponse:
        """The household's answer to a request to come in.

        The primary channel for consent: decided here, on their own screens,
        and carried back on the connection this instance already trusts.
        """
        return await self._request("POST", f"/access-requests/{request_uuid}/decision", json=payload)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
    ) -> ApiResponse:
        request_headers = {"Accept": "application/json"}

        if headers:
            request_headers.update(headers)

        if authenticated:
            if not self._agent_key:
                raise HaimApiError("No agent key: this instance has not been paired.")

            # A standard Authorization header and not a custom one: logging and
            # APM tooling redacts this one by default and knows nothing about an
            # invented header, which would end up written out in full.
            request_headers["Authorization"] = f"Bearer {self._agent_key}"

        group = _limit_group(path)
        wait = self._quiet_until.get(group, 0.0) - time.monotonic()

        if wait > 0:
            # Still inside a Retry-After the panel gave us. Not even sent:
            # another request now would only be another 429.
            raise RateLimited(wait)

        try:
            # Redirects are never followed. aiohttp follows them by default,
            # and a 307 or 308 repeats the request WITH its body to wherever
            # it points - `http://` included. The body of /credentials is a
            # token that opens the house, and the body of /state is the
            # house's inventory. The panel has no reason to redirect an agent
            # call; one that does is answered as an error (verifica 03).
            response_context = self._session.request(
                method,
                f"{self._base}{path}",
                json=json,
                headers=request_headers,
                allow_redirects=False,
            )
        except aiohttp.ClientError as error:
            raise HaimApiError("Could not reach the panel.") from error

        try:
            async with response_context as response:
                rotated = response.headers.get(HEADER_TOKEN_ROTATE)
                body: dict[str, Any] | None = None

                if response.status != 204:
                    try:
                        parsed = await response.json(content_type=None)
                        body = parsed if isinstance(parsed, dict) else None
                    except (aiohttp.ContentTypeError, ValueError):
                        # A proxy error page instead of JSON is not a reason to
                        # crash the integration; it is a reason to have no body.
                        body = None

                retry_after = response.headers.get("Retry-After")

                result = ApiResponse(
                    status=response.status,
                    data=body,
                    server_time=response.headers.get(HEADER_SERVER_TIME),
                    config_version=response.headers.get(HEADER_CONFIG_VERSION),
                    rotated_key=rotated,
                )
        except aiohttp.ClientConnectorCertificateError as error:
            raise CertificateRejected("The panel's certificate did not verify.") from error
        except (aiohttp.ClientError, OSError) as error:
            raise HaimApiError("Could not reach the panel.") from error

        if result.status == 429:
            # Respect Retry-After, plus a little jitter: every house that hit
            # the same limit at the same moment must not come back together.
            delay = _retry_after(retry_after)
            delay = min(delay + random.uniform(0, max(1.0, delay * 0.1)), RATE_LIMIT_MAX_SECONDS + 60)
            self._quiet_until[group] = time.monotonic() + delay
            _LOGGER.debug("The panel rate limited /%s; quiet for %.0fs", group, delay)

            raise RateLimited(delay)

        if 300 <= result.status < 400:
            raise HaimApiError(f"The panel answered with a redirect ({result.status}); not followed.")

        if authenticated and result.status == 401:
            error = (result.data or {}).get("error")

            # Two different 401s, and the difference matters: a revoked key means
            # stop and raise a repair issue, anything else means keep trying
            # because somebody may be fixing a typo in a config entry.
            if error == "token_revoked":
                raise TokenRevoked("The cloud has revoked this instance's key.")

            raise HaimApiError("The cloud did not accept this instance's key.")

        if authenticated and result.status == 410:
            raise SiteInactive("The instance is archived or the plan has lapsed.")

        return result
