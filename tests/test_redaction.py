"""The redaction filter.

Every case here is something a real Home Assistant log has been observed to
contain. The filter is deliberately over-eager: removing a word too many costs
somebody thirty seconds of puzzlement, leaving a token in costs a house.
"""

from __future__ import annotations

import pytest

from custom_components.haim_agent.const import (
    LOG_LEVEL_CATEGORIES,
    LOG_LEVEL_COUNTS,
    LOG_LEVEL_FULL,
)
from custom_components.haim_agent.redaction import PLACEHOLDER, redact, summarise


@pytest.mark.parametrize(
    "secret",
    [
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
        "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH",
    ],
)
def test_it_removes_things_that_look_like_tokens(secret: str) -> None:
    assert secret not in redact(f"Setup failed with token {secret} for the integration")


def test_it_removes_credentials_from_a_url() -> None:
    line = "Cannot connect to https://mario:hunter2@api.example.com/api"

    redacted = redact(line)

    assert "hunter2" not in redacted
    assert "mario:" not in redacted
    # The host goes too: since the review after D40 every URL host does, save
    # a short list of public software hosts, because a family's own domain
    # (`villa-bianchi.it`) is no different from `nas.local`. What stays is
    # the scheme and the path, which say which kind of service would not
    # answer.
    assert "api.example.com" not in redacted
    assert redacted == "Cannot connect to https://[redacted]/api"


@pytest.mark.parametrize(
    "line,secret",
    [
        ("Login failed for anna.rossi@example.com", "anna.rossi@example.com"),
        ("Cannot reach 192.168.1.40 after 3 tries", "192.168.1.40"),
        ("Device aa:bb:cc:dd:ee:ff stopped answering", "aa:bb:cc:dd:ee:ff"),
        ("Connected to SSID CasaRossi_5G", "CasaRossi_5G"),
        ("password: correct-horse-battery", "correct-horse-battery"),
        ("api_key=9f8e7d6c5b4a3f2e1d0c9b8a", "9f8e7d6c5b4a3f2e1d0c9b8a"),
    ],
)
def test_it_removes_the_things_that_identify_a_household(line: str, secret: str) -> None:
    assert secret not in redact(line)
    assert PLACEHOLDER in redact(line)


def test_it_leaves_an_ordinary_message_alone() -> None:
    line = "Error setting up entry for zha: could not open the serial port"

    # Over-eager is not the same as useless. A message with nothing sensitive in
    # it must survive intact, or the whole feature gets turned off.
    assert redact(line) == line


def test_counts_level_sends_no_messages_at_all() -> None:
    summary = summarise(
        [{"level": "error", "source": "zha", "message": "boom", "at": "2026-08-24T10:00:00Z"}],
        LOG_LEVEL_COUNTS,
        errors=5,
        warnings=3,
    )

    # Absent, not empty: the schema allows it to be missing, and the difference
    # says that nobody chose to send messages.
    assert "recent" not in summary
    assert summary["errors"] == 5
    assert summary["warnings"] == 3


def test_categories_level_keeps_the_first_line_only() -> None:
    trace = "Setup failed for mqtt\nTraceback (most recent call last):\n  token=abcdefghijklmnopqrstuvwxyz012345"

    summary = summarise(
        [{"level": "error", "source": "mqtt", "message": trace, "at": "2026-08-24T10:00:00Z"}],
        LOG_LEVEL_CATEGORIES,
        errors=1,
        warnings=0,
    )

    message = summary["recent"][0]["message"]

    # The first line says what broke. The forty after it say where our own code
    # was standing, and are where the secrets live.
    assert message == "Setup failed for mqtt"
    assert "abcdefghijklmnopqrstuvwxyz012345" not in message


def test_full_level_still_redacts() -> None:
    summary = summarise(
        [{
            "level": "error",
            "source": "cloud",
            "message": "Refused for anna@example.com with token eyJhbGciOiJIUzI1NiJ9.eyJhIjoxfQ.sig12345",
            "at": "2026-08-24T10:00:00Z",
        }],
        LOG_LEVEL_FULL,
        errors=1,
        warnings=0,
    )

    message = summary["recent"][0]["message"]

    # "Full" means the whole message, not the whole message plus the secrets.
    assert "anna@example.com" not in message
    assert "eyJhbGciOiJIUzI1NiJ9" not in message


def test_it_never_sends_more_than_the_schema_allows() -> None:
    entries = [
        {"level": "error", "source": "zha", "message": "x" * 900, "at": "2026-08-24T10:00:00Z"}
        for _ in range(50)
    ]

    summary = summarise(entries, LOG_LEVEL_FULL, errors=50, warnings=0)

    assert len(summary["recent"]) == 20
    assert all(len(entry["message"]) <= 500 for entry in summary["recent"])


def test_an_unknown_level_falls_back_to_counts() -> None:
    summary = summarise(
        [{"level": "error", "source": "zha", "message": "boom", "at": "2026-08-24T10:00:00Z"}],
        "everything_please",
        errors=1,
        warnings=0,
    )

    # A newer panel inventing a level must make us send LESS, never more.
    assert summary["level"] == LOG_LEVEL_COUNTS
    assert "recent" not in summary


# --------------------------------------------------------------------------
# Verifica 03: real Home Assistant log lines, and every hole found in them.
#
# Each line below is shaped like something Home Assistant or an integration
# actually logs. Every one of them got through the filter as it was before the
# audit (docs/verifica/03-esito.md): the string in the second column came out
# the other side intact.
# --------------------------------------------------------------------------

LEAKS_FOUND_IN_THE_AUDIT = [
    # `\btoken\b` never matched inside `access_token`: underscore is a word
    # character. The commonest spelling of a credential walked straight through.
    ("Invalid access_token: 'abc123def456'", "abc123def456"),
    ("refresh_token: 8f9a0b1c2d3e4f5a", "8f9a0b1c2d3e4f5a"),
    ("client_secret=Zx9yW8vU7tS6", "Zx9yW8vU7tS6"),
    ("long_lived_access_token=abcDEF123456", "abcDEF123456"),
    ("mqtt_password: hunter22", "hunter22"),
    # JSON and Python reprs put a quote between the key and the separator.
    ('Config was {"password": "hunter22", "username": "mario"}', "hunter22"),
    ("Payload {'password': 'my secret pw'}", "secret pw"),
    # Short values with an explicit separator.
    ("password=pw", "=pw"),
    ("Unexpected error with token=abc", "=abc"),
    ("api key: 'ab12'", "ab12"),
    ("X-Ha-Access: abcdef123456", "abcdef123456"),
    ("psk=SuperSecretWifi", "SuperSecretWifi"),
    ("passphrase: correcthorse", "correcthorse"),
    # Query strings: keys and the house's coordinates.
    ("Request to https://api.example.com/v1/data?appid=abcd1234efgh&lat=45.4064&lon=11.8768 failed", "abcd1234efgh"),
    ("Request to https://api.example.com/v1/data?appid=abcd1234efgh&lat=45.4064&lon=11.8768 failed", "45.4064"),
    ("Home location latitude: 45.406435, longitude: 11.876761", "45.406435"),
    ("zone.home is at 45.4064, 11.8768", "11.8768"),
    # Compressed IPv6, which the old pattern needed all eight groups to see.
    ("Cannot reach 2001:db8::1", "2001:db8::1"),
    ("Cannot reach fe80::1ff:fe23:4567:890a%eth0", "fe80::1ff:fe23:4567:890a"),
    # MAC addresses written the other two ways vendors write them.
    ("Device aabb.ccdd.eeff lost", "aabb.ccdd.eeff"),
    ("Device AABBCCDDEEFF lost", "AABBCCDDEEFF"),
    # Network names: quoted with a space in, or announced differently.
    ('Connected to wifi "Casa Rossi"', "Rossi"),
    ("Network 'FRITZ!Box 7530 GR' not found", "FRITZ!Box 7530 GR"),
    ("essid: TIM-12345678", "TIM-12345678"),
    # Keys with a well-known shape and fewer than forty characters.
    ("Using key AIzaSyA1234567890abcdefghijklmnopqrstu", "AIzaSyA1234567890abcdefghijklmnopqrstu"),
    ("telegram bot 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw failed", "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"),
    ("webhook https://hooks.example.com/api/webhook/a1b2c3d4e5f6 failed", "a1b2c3d4e5f6"),
    # Names that say whose house it is.
    ("Calling https://casa-rossi.duckdns.org:8123/api/states", "casa-rossi"),
    ("Cannot connect to giulia-macbook.local", "giulia-macbook"),
    ("Error in automation automation.sveglia_giulia: light.camera_di_giulia unavailable", "camera_di_giulia"),
    ("Error in automation automation.sveglia_giulia: light.camera_di_giulia unavailable", "sveglia_giulia"),
]


LEAKS_FOUND_IN_THE_AUDIT += [
    # Second pass, after a security review of the first fix: the same secrets,
    # spelled so the patterns did not recognise them (parser differential).
    # Percent-encoded: a server reads `token%3Dx` as `token=x`; we read nothing.
    ("GET /auth?code%3Dabc%26token%3Ds3cr3tvalue", "s3cr3tvalue"),
    ("password%3Dhunter22", "hunter22"),
    ("lat%3D45.4064%26lon%3D11.8768", "45.4064"),
    # JSON logged inside a string: the backslash stopped the key pattern.
    ('{\\"password\\": \\"hunter22\\"}', "hunter22"),
    # Accented text glued to a token: in Unicode mode `è` is a word character,
    # so there was no `\b` and the long-token rules never fired.
    ("Errore: tokenè" + "A" * 45, "A" * 45),
    ("chiaveé" + "Ab1" * 10, "Ab1Ab1Ab1Ab1"),
    # An address with an accent kept everything after it.
    ("giulià@example.it non raggiungibile", "example.it"),
    # A slash in the password left the user name and half of it behind.
    ("https://admin:pa/ss@nas.example.com/", "admin:pa"),
    # Separators the key pattern did not know.
    ("password => hunter22", "hunter22"),
    ("password -> hunter22", "hunter22"),
    ("pass=hunter22", "hunter22"),
    ("pw=hunter22", "hunter22"),
    ("<password>hunter22</password>", "hunter22"),
    # Our own placeholder, sent in: the guard that stops double redaction made
    # the filter skip a value that merely began with it (ineffective control).
    ("password=[redacted]hunter22", "hunter22"),
    ("Authorization: Bearer [redacted]abcdef123456", "abcdef123456"),
]


LEAKS_FOUND_IN_THE_AUDIT += [
    # Third pass: regressions that 2f03c20 introduced on 334f710, found by
    # comparing the two on the same inputs. Each was redacted by 334f710.
    # Decoding BEFORE matching put a space inside the value, and it ended there.
    ("token=abc%20def", "def"),
    ("?api_key=abc%20def&x=1", "def"),
    ("password=hunter%2022", "22"),
    ("token=abc%0Adef", "def"),
    ("ssid=Casa%20Rossi", "Rossi"),
    ("https://admin:se%20cret@host.example.com/", "admin:se"),
    # ASCII patterns no longer saw what a reader still reads as ASCII.
    ("password hunter22xx", "hunter22"),
    ("Cannot reach ١٩٢.١٦٨.١.٤٠", "٤٠"),
    ("Cannot reach １９２.１６８.１.４０", "４０"),
    ("paſſword=hunter22", "hunter22"),
    # Fourth review: characters a reader never sees, between the letters.
    ("pass​word=hunter22", "hunter22"),
    ("pass­word=hunter22", "hunter22"),
    ("\x1b[31mpassword\x1b[0m=hunter22", "hunter22"),
    ("Cannot reach 192.168.​1.77", "1.77"),
    ("anna‍.rossi@example.com failed", "example.com"),
    # Leaked by both, closed with the same pass.
    ("Bearer abc%20def", "def"),
    ("lat=45.4064%2C11.8768", "11.8768"),
]


@pytest.mark.parametrize("line,secret", LEAKS_FOUND_IN_THE_AUDIT)
def test_every_leak_found_in_the_audit_is_closed(line: str, secret: str) -> None:
    assert secret not in redact(line)


@pytest.mark.parametrize("level", [LOG_LEVEL_CATEGORIES, LOG_LEVEL_FULL])
@pytest.mark.parametrize("line,secret", LEAKS_FOUND_IN_THE_AUDIT)
def test_no_level_the_household_can_choose_lets_one_through(level: str, line: str, secret: str) -> None:
    """The filter is applied on the way out at every level that sends text.

    Fed as the first line, because that is the line `categories` keeps.
    """
    summary = summarise(
        [{"level": "error", "source": "zha", "message": line + "\nTraceback...", "at": "2026-08-24T10:00:00Z"}],
        level,
        errors=1,
        warnings=0,
    )

    assert secret not in summary["recent"][0]["message"]


@pytest.mark.parametrize("line,secret", LEAKS_FOUND_IN_THE_AUDIT)
def test_counts_sends_none_of_it(line: str, secret: str) -> None:
    import json

    summary = summarise(
        [{"level": "error", "source": "zha", "message": line, "at": "2026-08-24T10:00:00Z"}],
        LOG_LEVEL_COUNTS,
        errors=1,
        warnings=0,
    )

    assert secret not in json.dumps(summary)


def test_the_full_level_reaches_the_secrets_below_the_first_line() -> None:
    """`full` sends the whole trace, so the whole trace is filtered."""
    trace = (
        "Error setting up entry Casa for shelly\n"
        "Traceback (most recent call last):\n"
        '  File "/usr/src/homeassistant/homeassistant/config_entries.py", line 749, in async_setup\n'
        "    result = await component.async_setup_entry(hass, self)\n"
        "aiohttp.client_exceptions.ClientResponseError: 401, message='Unauthorized', "
        "url='http://admin:S3cr3tPw@192.168.1.77/rpc?auth_key=deadbeef42'"
    )

    summary = summarise(
        [{"level": "error", "source": "shelly", "message": trace, "at": "2026-08-24T10:00:00Z"}],
        LOG_LEVEL_FULL,
        errors=1,
        warnings=0,
    )

    message = summary["recent"][0]["message"]

    for secret in ("S3cr3tPw", "admin:", "192.168.1.77", "deadbeef42"):
        assert secret not in message

    # And it is still a trace an installer can read.
    assert "ClientResponseError: 401" in message


def test_the_source_is_filtered_too() -> None:
    """A source is a logger or a file name, and those can be anybody's."""
    summary = summarise(
        [{"level": "error", "source": "light.camera_di_giulia", "message": "boom", "at": "2026-08-24T10:00:00Z"}],
        LOG_LEVEL_FULL,
        errors=1,
        warnings=0,
    )

    assert "giulia" not in summary["recent"][0]["source"]


@pytest.mark.parametrize(
    "line",
    [
        "Update of sensor is taking over 10 seconds",
        "Timeout fetching data at 2026-08-24 10:00:00",
        "Platform shelly not ready yet: Timeout; Retrying in background in 30 seconds",
        'File "/usr/src/homeassistant/homeassistant/components/light/__init__.py", line 45, in async_turn_on',
        "Error requesting data: Cannot connect to host api.openweathermap.org:443",
        "Version 2026.8.2 is available",
    ],
)
def test_the_wider_filter_still_leaves_ordinary_lines_alone(line: str) -> None:
    """Times of day are not IPv6 addresses, versions are not coordinates."""
    assert redact(line) == line


@pytest.mark.parametrize(
    "line",
    [
        "Automation casa_rossi_notte failed",
        "Host casa-rossi unreachable",
        "Welcome to CasaRossi",
        "Instance 'Casa  Rossi' restarted",
        "Instance 'CASA ROSSI' restarted",
    ],
)
def test_a_known_name_goes_however_it_is_spelled(line: str) -> None:
    """Third review: the instance's own name was matched only literally.

    "Casa Rossi" written `casa_rossi`, `casa-rossi`, `CasaRossi` - the same
    name to a reader, nothing to the filter.
    """
    assert "rossi" not in redact(line, ("Casa Rossi",)).lower()


def test_a_known_name_with_an_accent_matches_in_either_unicode_form() -> None:
    # The name as Home Assistant stored it (decomposed), the log as written
    # (composed): one name to a reader.
    decomposed = "Villa Nicolò"
    composed = "Riavvio di Villa Nicolò"

    assert "Nicol" not in redact(composed, (decomposed,))


# --------------------------------------------------------------------------
# Independent review after D40: ten classes of input that came out the other
# side. Each entry is (line, what must not survive). Every one failed before
# the fix that closes it.
# --------------------------------------------------------------------------

def _summary_message(line: str, level: str) -> str:
    summary = summarise(
        [{"level": "error", "source": "zha", "message": line + "\nTraceback...", "at": "2026-08-24T10:00:00Z"}],
        level,
        errors=1,
        warnings=0,
    )

    return str(summary["recent"][0]["message"])


REVIEW_LEAKS: dict[str, list[tuple[str, str]]] = {
    # 1. The host of any URL or UNC path, not only the ones with a suffix we
    #    happen to know: a single-label name or the family's own domain.
    "hosts": [
        ("Backup to smb://nas-rossi/backup failed", "nas-rossi"),
        ("Cannot connect to http://nas-rossi:5000/webapi", "nas-rossi"),
        ("Cannot open \\\\NAS-ROSSI\\backup", "NAS-ROSSI"),
        ("Cannot open \\\\\\\\NAS-ROSSI\\\\backup", "NAS-ROSSI"),
        ("Fetching https://villa-bianchi.it failed", "villa-bianchi"),
        ("Fetching https://www.villa-bianchi.it/cam.jpg failed", "villa-bianchi"),
        ("Stream rtsp://camera-giardino:554/live lost", "camera-giardino"),
        ("Cannot reach http://[fe80::1ff:fe23:4567:890a]:8123/", "fe80"),
        ("ftp://user@nas-rossi/share refused", "nas-rossi"),
        # A public host as the user part does not make the real host public.
        ("Fetching https://github.com@villa-bianchi.it/x", "villa-bianchi"),
        ("Fetching https://github.com.villa-bianchi.it/x", "villa-bianchi"),
    ],
    # 2. user:password@host with no scheme in front.
    "schemeless_credentials": [
        ("Login admin:Giulia2010@192.168.1.20 refused", "Giulia2010"),
        ("Login admin:Giulia2010@nas-rossi refused", "Giulia2010"),
        ("Login admin:Giulia2010@nas-rossi refused", "nas-rossi"),
        ("mqtt admin:pa/ss@broker failed", "pa/ss"),
    ],
    # 3. Keys that announce a secret and were not on the list.
    "keys": [
        ("Tuya device local_key=a1B2c3D4e5F6g7H8", "a1B2c3D4e5F6g7H8"),
        ("Alarm disarm with code=4821", "4821"),
        ("Keypad pin=4821 rejected", "4821"),
        ("Keypad pin: 4821 rejected", "4821"),
        ("Lock user_code: 4821", "4821"),
        ("Cookie session=Ab3dE6gH9jK2mN5pQ8sT1 expired", "Ab3dE6gH9jK2mN5pQ8sT1"),
        ("session_id: Ab3dE6gH9jK2mN5pQ8sT1", "Ab3dE6gH9jK2mN5pQ8sT1"),
        ("Header Authorization: Token 5f3a9b2c7d rejected", "5f3a9b2c7d"),
        ("Authorization: Digest username=x, response=6629fae49393", "6629fae49393"),
        # The generic rule: a value that looks random after an unknown key.
        ("Device aux=Zx9yW8vU7tS6", "Zx9yW8vU7tS6"),
        ("Device payload devkey: k8Jd2LmQ9xRt", "k8Jd2LmQ9xRt"),
        ("Device payload blob=5f3a9b2c7d", "5f3a9b2c7d"),
    ],
    # 4. Colon hex longer than a MAC: a sixteen-byte network key.
    "long_colon_hex": [
        ("network_key 01:03:05:07:09:0B:0D:0F:00:02:04:06:08:0A:0C:0D", "08:0A:0C:0D"),
        ("key 01-03-05-07-09-0B-0D-0F-00-02-04-06-08-0A-0C-0D", "0C-0D"),
        ("pan 01:03:05:07:09:0b:0d", "0b:0d"),
    ],
    # 5. Zigbee IEEE / EUI-64 in every common spelling.
    "eui64": [
        ("Device 0x00124b001ca1b2c3 left the network", "00124b001ca1b2c3"),
        ("Device 0x00124B001CA1B2C3 left the network", "00124B001CA1B2C3"),
        ("Device 00:12:4b:00:1c:a1:b2:c3 left", "1c:a1:b2:c3"),
        ("Device 00-12-4B-00-1C-A1-B2-C3 left", "1C-A1-B2-C3"),
        ("Device 00124b001ca1b2c3 left", "00124b001ca1b2c3"),
        ("Device [0x00124b001ca1b2c3]", "001ca1b2c3"),
    ],
    # 6. JSON quoted with HTML entities.
    "html_entities": [
        ("{&quot;password&quot;: &quot;hunter22&quot;}", "hunter22"),
        ("{&#34;token&#34;: &#34;abc123&#34;}", "abc123"),
        ("{&#x27;password&#x27;: &#x27;hunter22&#x27;}", "hunter22"),
        ("&lt;password&gt;hunter22&lt;/password&gt;", "hunter22"),
    ],
    # 7. Unicode separators between a key and its value.
    "unicode_separators": [
        ("password → hunter22x", "hunter22x"),
        ("password ⇒ hunter22x", "hunter22x"),
        ("password ➡ hunter22x", "hunter22x"),
        ("password ⟶ hunter22x", "hunter22x"),
        ("password：hunter22x", "hunter22x"),
        ("password ∶ hunter22x", "hunter22x"),
        ("password ≔ hunter22x", "hunter22x"),
        ("password ꞉ hunter22x", "hunter22x"),
        ("password — hunter22x", "hunter22x"),
        ("password | hunter22x", "hunter22x"),
        ("token → abc", "abc"),
    ],
    # 8. Telephone numbers: SMS and call notifiers log them.
    "phones": [
        ("SMS to +39 347 123 4567 failed", "123 4567"),
        ("SMS to +393471234567 failed", "3471234567"),
        ("SMS to 0039 347 1234567 failed", "1234567"),
        ("SMS to 3471234567 failed", "3471234567"),
        ("SMS to 347 123 4567 failed", "123 4567"),
        ("SMS to 347-123-4567 failed", "123-4567"),
        ("Call to 049 8765432 failed", "8765432"),
        ("Call to 06 12345678 failed", "12345678"),
        ("Call to +44 20 7946 0958 failed", "7946 0958"),
        ("Call to +1 (415) 555-0132 failed", "555-0132"),
        ("Call to +49-30-1234567 failed", "1234567"),
    ],
    # 9. Entity ids behind a dot: templates and per-automation loggers.
    "dotted_entities": [
        ("Template error: states.sensor.temperatura_camera_giulia.state", "temperatura_camera_giulia"),
        ("Logger homeassistant.components.automation.sveglia_giulia: error", "sveglia_giulia"),
        ("Error in homeassistant.components.script.apri_cancello_rossi", "apri_cancello_rossi"),
        ("{{ states.light.camera_di_giulia }}", "camera_di_giulia"),
    ],
    # 10. `.home` as a suffix: the neighbour of `www.home-assistant.io`, which
    #     must still go. The domain itself is in the ordinary-lines test.
    "home_suffix": [
        ("Cannot connect to giulia-pc.home", "giulia-pc"),
        ("Cannot connect to giulia-pc.home:8123", "giulia-pc"),
    ],
}

_REVIEW_PARAMS = [
    pytest.param(line, secret, id=f"{kind}-{index}")
    for kind, cases in REVIEW_LEAKS.items()
    for index, (line, secret) in enumerate(cases)
]


@pytest.mark.parametrize("line,secret", _REVIEW_PARAMS)
def test_every_leak_found_by_the_independent_review_is_closed(line: str, secret: str) -> None:
    assert secret not in redact(line)


@pytest.mark.parametrize("level", [LOG_LEVEL_CATEGORIES, LOG_LEVEL_FULL])
@pytest.mark.parametrize("line,secret", _REVIEW_PARAMS)
def test_no_level_lets_a_review_leak_through(level: str, line: str, secret: str) -> None:
    assert secret not in _summary_message(line, level)


@pytest.mark.parametrize("line,secret", _REVIEW_PARAMS)
def test_counts_sends_none_of_the_review_leaks(line: str, secret: str) -> None:
    import json

    summary = summarise(
        [{"level": "error", "source": "zha", "message": line, "at": "2026-08-24T10:00:00Z"}],
        LOG_LEVEL_COUNTS,
        errors=1,
        warnings=0,
    )

    assert secret not in json.dumps(summary)


@pytest.mark.parametrize(
    "source,secret",
    [
        ("homeassistant.components.automation.sveglia_giulia", "sveglia_giulia"),
        ("smb://nas-rossi/backup", "nas-rossi"),
    ],
)
def test_the_source_loses_the_review_leaks_too(source: str, secret: str) -> None:
    summary = summarise(
        [{"level": "error", "source": source, "message": "boom", "at": "2026-08-24T10:00:00Z"}],
        LOG_LEVEL_FULL,
        errors=1,
        warnings=0,
    )

    assert secret not in summary["recent"][0]["source"]


@pytest.mark.parametrize(
    "line",
    [
        # Public software hosts an installer needs, kept by name (classes 1, 10).
        "See https://www.home-assistant.io/integrations/zha for details",
        "Release notes at https://github.com/home-assistant/core/releases/tag/2026.8.2",
        "Download from https://raw.githubusercontent.com/hacs/default/master/integration",
        "Requirements from https://pypi.org/simple/zha-quirks/ failed",
        "Requirements from https://files.pythonhosted.org/packages/ failed",
        "Visit www.home-assistant.io or community.home-assistant.io",
        "Cannot connect to http://supervisor/core/info",
        "Cannot connect to http://localhost:8123/api/",
        # Python modules of Home Assistant are packages, not entities (class 9).
        "Logger homeassistant.components.light is noisy",
        "Error in homeassistant.components.automation.config: invalid trigger",
        "Error in homeassistant.components.sensor.recorder while compiling statistics",
        "homeassistant.components.mqtt.light.schema_json: bad payload",
        "Setup of homeassistant.components.zha.light took 1.2 seconds",
        'File "/usr/src/homeassistant/homeassistant/components/light/__init__.py", line 45',
        # Numbers that are not telephone numbers (class 8).
        "Fetched 1271 entities in 0.35 seconds",
        "Listening on port 8123",
        "2026-08-24 10:00:00.123456 ERROR (MainThread) [homeassistant.core] Error",
        "Timestamp 2026-08-24T10:00:00+00:00 is in the past",
        "Version 2026.8.2 is available, you have 2024.6.0",
        "Took 3.4712345 seconds",
        "Recorder purged 120000 rows",
        # Status codes, which are not PIN codes (class 3).
        "Unexpected status code: 500",
        "Request failed with status_code=503",
        "Error code: 401 Unauthorized",
        "HTTP error code=404",
        # Ordinary values after a colon that the entropy rule must leave alone.
        "Platform zha: model TS0601_thermostat not supported",
        "manufacturer: LUMI, model: lumi.sensor_magnet.aq2",
        "Device model: SNZB-02P, firmware: 1.0.3",
        "Error: ClientConnectorError: Cannot connect",
        "Setup of integration esphome failed: Timeout",
        "Device ShellyPlus1PM: firmware=20240819-074343/1.4.2-gc2639da",
        # Zigbee network addresses: four hex digits, reassigned on every rejoin.
        "Device 0x1a2b did not respond",
    ],
)
def test_the_review_fixes_leave_ordinary_lines_alone(line: str) -> None:
    assert redact(line) == line


@pytest.mark.parametrize("line", ["1-" * 5000, "light." * 3000, "a=" * 5000, "\ufdfa" * 1000])
def test_a_pathological_line_costs_milliseconds_not_seconds(line) -> None:
    """It runs in Home Assistant's event loop, once per heartbeat."""
    import time

    started = time.perf_counter()
    redact(line)

    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    ("line", "known"),
    [
        ("Lampada Camera di Giulia offline", ("Lampada Camera", "Camera di Giulia")),
        ("Camera di Giulia Rossi", ("Giulia Rossi", "Camera di Giulia")),
    ],
)
def test_two_known_names_that_overlap_go_whole(line, known) -> None:
    """One alternation took one match per position and left the rest of the
    other name: "[redacted] Rossi" (ninth review)."""
    redacted = redact(line, known)

    for word in ("Giulia", "Rossi", "Lampada"):
        assert word not in redacted
