"""Security properties of the component, enforced rather than assumed.

This file exists because two of the rules this project runs on are the kind that
survive by everybody remembering them - and everybody remembering something is
not a control. A grep in the test suite is.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "haim_agent"
SOURCES = sorted(COMPONENT.rglob("*.py"))


def code_only(source: Path) -> str:
    """The file with its comments and string literals removed.

    Grepping a source file for a forbidden word finds it in the comment that
    explains why the word is forbidden. What we want to check is the code, so
    the prose comes out first.
    """
    import io
    import tokenize

    kept: list[str] = []

    with io.StringIO(source.read_text()) as handle:
        for token in tokenize.generate_tokens(handle.readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue

            kept.append(f"{token.start[0]}:{token.string}")

    return "\n".join(kept)


# Every way anyone has ever turned off certificate verification in Python.
TLS_WEAKENERS = [
    r"verify\s*=\s*False",
    r"ssl\s*=\s*False",
    r"ssl_context\s*=\s*False",
    r"_create_unverified_context",
    r"CERT_NONE",
    r"check_hostname\s*=\s*False",
    r"InsecureRequestWarning",
    r"verify_ssl\s*=\s*False",
]


@pytest.mark.parametrize("pattern", TLS_WEAKENERS)
def test_nothing_anywhere_turns_off_certificate_verification(pattern: str) -> None:
    """Rule 13, made mechanical.

    Not behind a flag, not behind an environment variable, not inside an
    `if debug`. The token that identifies a customer's home travels on this
    channel, and a switch somebody has to remember to turn off before going to
    production is a switch that eventually is not.
    """
    offenders = [
        f"{source.name}:{index}"
        for source in SOURCES
        for index, line in enumerate(source.read_text().splitlines(), start=1)
        if re.search(pattern, line)
    ]

    assert offenders == [], f"{pattern} appears at {offenders}"


def test_no_plain_http_url_is_accepted_anywhere() -> None:
    """The contract says no version of this client has an option to allow HTTP.

    A loopback exception was written once and taken out again: it is exactly the
    local convenience that stops being local. This is what stops it coming back.
    """
    config_flow = (COMPONENT / "config_flow.py").read_text()

    assert 'startswith("https://")' in config_flow
    assert "_is_loopback" not in config_flow
    assert "127.0.0.1" not in config_flow


def test_the_agent_key_is_never_logged() -> None:
    """No log statement anywhere interpolates the key or the pairing code."""
    suspicious = re.compile(
        r"_LOGGER\.[a-z]+\([^)]*(agent_key|_agent_key|pairing_code|CONF_AGENT_KEY|token)",
        re.IGNORECASE,
    )

    offenders = [
        f"{source.name}: {match.group(0)[:70]}"
        for source in SOURCES
        for match in suspicious.finditer(source.read_text())
    ]

    assert offenders == [], offenders


def test_the_component_ships_no_dependencies_of_its_own() -> None:
    """Every dependency installed into a customer's house is attack surface.

    It is also a line in the SBOM somebody has to keep watching, and a reason
    for the component to break when Home Assistant pins a different version.
    """
    manifest = json.loads((COMPONENT / "manifest.json").read_text())

    assert manifest["requirements"] == []


def test_nothing_collects_anything_about_the_household() -> None:
    """The boundary that is the product, checked in the source.

    Counts, versions, system metrics, integration domains, add-on slugs. Never a
    state, a history, an area name or a person. These Home Assistant APIs are
    the ones that would hand us the wrong things.
    """
    forbidden = [
        # The whole of somebody's history and current state.
        r"async_get_recorder",
        r"recorder\b",
        r"\bstates\.async_all\(\)\s*\[",
        # Names, as opposed to counts, of what is in the house.
        r"friendly_name",
        r"\.name for .* in .*areas",
        # The people.
        r"auth\.async_get_users",
        r"async_get_owner",
    ]

    for pattern in forbidden:
        offenders = [
            f"{source.name}:{line}"
            for source in SOURCES
            for line in code_only(source).splitlines()
            if re.search(pattern, line)
        ]

        assert offenders == [], f"{pattern} appears at {offenders}"


async def test_nothing_about_a_household_reaches_the_payload() -> None:
    """The boundary that is the product, measured rather than asserted.

    A fake instance furnished the way a real one is - a child's bedroom light, a
    camera in the hall, an area called after somebody - and then the whole
    serialised payload is searched for any of it. Counting occurrences of the
    string "entity_id" in the source would have been a proxy; this is the thing
    itself.
    """
    import json
    from types import SimpleNamespace

    from custom_components.haim_agent.collector import StateCollector

    from test_collector import FakeHass, state

    household = {
        "light.camera_di_matteo": state("light.camera_di_matteo", "on", friendly_name="Camera di Matteo"),
        "camera.ingresso_casa_rossi": state("camera.ingresso_casa_rossi", "idle", friendly_name="Ingresso"),
        "sensor.consumo_totale": state("sensor.consumo_totale", "4212.7", friendly_name="Consumo totale"),
        "device_tracker.telefono_di_anna": state("device_tracker.telefono_di_anna", "home"),
        "automation.sveglia_di_anna": state("automation.sveglia_di_anna", "on"),
        # The one entity id that is allowed out, because it names a Home
        # Assistant update entity and nothing anybody owns.
        "update.home_assistant_core_update": state(
            "update.home_assistant_core_update", "on",
            installed_version="2026.8.2", latest_version="2026.8.4", supported_features=3,
        ),
    }

    hass = FakeHass(states=household, components={"system_log"})

    payload = json.dumps(await StateCollector(hass).async_build_state())

    forbidden = [
        "camera_di_matteo", "Camera di Matteo",
        "ingresso_casa_rossi", "Ingresso",
        "consumo_totale", "4212.7",
        "telefono_di_anna",
        "sveglia_di_anna",
    ]

    for leak in forbidden:
        assert leak not in payload, f"the payload carries {leak!r}"

    # And the counts still tell the installer what they need: one automation,
    # one update waiting.
    rebuilt = json.loads(payload)

    assert rebuilt["counts"]["automations"] == 1
    assert rebuilt["updates"][0]["entity_id"] == "update.home_assistant_core_update"


@pytest.mark.parametrize(
    "secret",
    [
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.aVeryLongSignatureValueHere12345",
        "ghp_16C7e42F292c6912E7710c838347Ae178B4a",
        "AKIAIOSFODNN7EXAMPLE",
    ],
)
def test_the_redaction_filter_holds_against_real_secret_shapes(secret: str) -> None:
    from custom_components.haim_agent.redaction import redact

    line = f"Integration setup failed, credential was {secret} at line 40"

    assert secret not in redact(line)


def test_redaction_survives_a_message_that_is_all_secret() -> None:
    from custom_components.haim_agent.redaction import redact

    # A stack trace that is one enormous token. It must not crash the filter and
    # it must not come out the other side.
    blob = "A" * 5000

    assert blob not in redact(blob)


# --------------------------------------------------------------------------
# Verifica 03, point 6: the grep above covers keywords. These cover the two
# ways round it that a grep does not see.
# --------------------------------------------------------------------------


def _calls(name: str) -> list[tuple[str, object]]:
    import ast

    found = []

    for source in SOURCES:
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Call):
                callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)

                if callee == name:
                    found.append((f"{source.name}:{node.lineno}", node))

    return found


def test_the_shared_session_is_always_the_verifying_one() -> None:
    """`async_get_clientsession(hass, False)` turns verification off with a
    POSITIONAL argument - no `verify_ssl=` for a grep to find. Home Assistant's
    shared session is the only one, called with `hass` and nothing else."""
    calls = _calls("async_get_clientsession") + _calls("async_create_clientsession")

    assert calls, "the component no longer gets its session from Home Assistant?"

    for where, node in calls:
        assert len(node.args) == 1 and not node.keywords, f"{where} passes more than hass"


@pytest.mark.parametrize("constructor", ["ClientSession", "TCPConnector", "SSLContext", "create_default_context"])
def test_the_component_builds_no_transport_of_its_own(constructor: str) -> None:
    """A session, connector or SSL context of our own is where verification
    gets configured - and so where it gets turned off."""
    assert _calls(constructor) == []


def test_no_request_follows_a_redirect() -> None:
    """aiohttp follows redirects by default, and a 307 or 308 repeats the
    request WITH its body - to `http://` too. The body of /credentials is a
    token that opens the house."""
    for where, node in _calls("request"):
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}

        assert "allow_redirects" in keywords, f"{where} follows redirects"
        assert getattr(keywords["allow_redirects"], "value", None) is False, where


async def test_a_redirect_is_an_error_not_a_destination() -> None:
    from custom_components.haim_agent.api import HaimApi, HaimApiError

    seen: list[dict] = []

    class Response:
        status = 307
        headers = {"Location": "http://haim.example/api/v1/agent/credentials"}

        async def json(self, content_type=None):
            return {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Session:
        def request(self, method, url, **kwargs):
            seen.append(kwargs)

            return Response()

    api = HaimApi(Session(), "https://haim.example", agent_key="haim_abcdef12_" + "x" * 32)

    with pytest.raises(HaimApiError):
        await api.deliver_credentials({"session_uuid": "s", "token": "secret"})

    assert seen[0]["allow_redirects"] is False
