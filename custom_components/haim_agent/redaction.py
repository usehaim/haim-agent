"""Strips secrets out of log messages before they leave the house.

This is not hygiene theatre. Home Assistant stack traces routinely contain
access tokens, URLs with credentials in them, email addresses used as account
names, and the name of the household's wireless network. The competing product
forwards them whole, up to fifty errors and fifty warnings a minute. For us this
is at once a reduction in GDPR exposure and something we can point at in a sales
conversation - and it only counts if it is applied on THIS side, before the
message is put on the wire.

Three levels, chosen by the household:

    counts      how many errors and warnings. No messages at all.
    categories  the counts, plus which integration each came from, with the
                message reduced to its first line and redacted.
    full        the counts, plus the messages, redacted.

Redaction is deliberately over-eager. Removing one word too many from a log line
costs somebody thirty seconds of puzzlement; leaving one token in costs a house.
"""

from __future__ import annotations

import functools
import html
import re
import unicodedata
from typing import Final
from urllib.parse import unquote

from .const import LOG_LEVEL_CATEGORIES, LOG_LEVEL_COUNTS, LOG_LEVEL_FULL

PLACEHOLDER: Final = "[redacted]"

# Words that announce a secret, however a library spells them. Matched as a
# PART of an identifier on purpose: `access_token`, `client_secret`,
# `long_lived_access_token`, `mqtt_password`. A `\btoken\b` never sees the
# token in `access_token`, because the underscore is a word character - that
# was the commonest way a credential walked straight through this filter.
_SECRET_KEY: Final = (
    r"[A-Za-z0-9_-]*"
    r"(?:token|secret|password|passwd|passphrase|pwd|psk|api[ _-]?key|apikey"
    r"|x-ha-access|authorization|cookie|credentials?|private[_-]?key)"
    r"[A-Za-z0-9_-]*"
)

# Hostnames that point at ONE house from the outside. A dynamic DNS name is as
# good as a street address: `casa-rossi.duckdns.org` is who and where.
_DYNAMIC_DNS: Final = (
    r"duckdns\.org|nabu\.casa|dynu\.(?:net|com)|no-ip\.(?:org|biz|com)|ddns\.net"
    r"|hopto\.org|zapto\.org|sytes\.net|myfritz\.net|synology\.me|myds\.me"
    r"|freeboxos\.fr|tplinkdns\.com|dyndns\.org|dynv6\.net|ts\.net|ngrok(?:-free)?\.(?:io|app)"
    r"|trycloudflare\.com|myqnapcloud\.com|asuscomm\.com|dnsalias\.\w+|ddnsfree\.com"
)

# Names on the house network. mDNS and router-assigned names are what people
# call their own machines - `giulia-macbook.local` - and a device's name is on
# the list of things that never leave (rule 4).
_LOCAL_SUFFIXES: Final = r"local|lan|home|internal|localdomain|home\.arpa|fritz\.box|station"

# Home Assistant entity domains. An entity id is `<domain>.<object_id>`, and
# the object id is where the household lives: `light.camera_di_giulia`. The
# domain is kept, because "a light failed" is what an installer needs; the
# rest goes.
_ENTITY_DOMAINS: Final = (
    "air_quality|alarm_control_panel|assist_satellite|automation|binary_sensor|button"
    "|calendar|camera|climate|conversation|counter|cover|date|datetime|device_tracker"
    "|event|fan|group|humidifier|image|image_processing|input_boolean|input_button"
    "|input_datetime|input_number|input_select|input_text|lawn_mower|light|lock"
    "|media_player|notify|number|person|remote|scene|schedule|script|select|sensor"
    "|siren|stt|switch|text|time|timer|todo|tts|update|vacuum|valve|wake_word"
    "|water_heater|weather|zone"
)


# Python modules that live inside a component package and are named like an
# object id: `homeassistant.components.automation.config` is code, while
# `homeassistant.components.automation.sveglia_giulia` is the logger of one
# automation. Anything after `<domain>.` that is not on this list goes - a new
# module name costs a word, an unlisted automation name would cost a person.
_COMPONENT_MODULES: Final = (
    "__init__|__main__|const|config|config_flow|helpers|trace|logbook|recorder|reproduce_state"
    "|significant_change|device_action|device_condition|device_trigger|intent|services|util"
    "|entity|models|schema|schema_basic|schema_json|schema_template|condition|trigger"
    "|group|diagnostics|repairs|websocket_api|http|storage|manager|blueprint|system_health"
)

# Hosts a URL may keep, because they name public software and not a house:
# where Home Assistant, its integrations and their releases come from. An
# installer reading "cannot reach github.com" learns something; reading
# "cannot reach villa-bianchi.it" learns whose house it is. Kept short on
# purpose - every entry is a host whose subdomains are run by the same
# project, which is why `github.io` (anybody's pages) is not here.
_PUBLIC_HOSTS: Final = (
    "github.com",
    "githubusercontent.com",
    "home-assistant.io",
    "pypi.org",
    "pythonhosted.org",
    "esphome.io",
    "hacs.xyz",
)

# Single-label names that are the same in every installation and so say
# nothing about this one: the Supervisor API and the machine itself.
_GENERIC_HOSTS: Final = ("localhost", "supervisor")


def _public_host(host: str) -> bool:
    host = host.lower().rstrip(".")

    if host in _GENERIC_HOSTS:
        return True

    return any(host == public or host.endswith("." + public) for public in _PUBLIC_HOSTS)


def _url_host(match: re.Match[str]) -> str:
    """The host of a URL goes, user part and all, unless it is public software."""
    userinfo, host = match.group(2), match.group(3)

    if not userinfo and _public_host(host):
        return match.group(0)

    return match.group(1) + PLACEHOLDER


def _code(match: re.Match[str]) -> str:
    """`code=4821` is an alarm code; `status code: 500` is HTTP."""
    before = match.string[max(0, match.start() - 12):match.start()]
    key = match.group(1).lower()

    if key.endswith("code") and (
        re.search(r"(?i)(?:status|error|http|response|exit|return|result|reason|err|country|lang(?:uage)?)[ _-]?$", before)
        or re.fullmatch(r"(?:status|error|http|response|exit|return|result|reason|err|country|lang(?:uage)?)[_-]?code", key)
    ):
        return match.group(0)

    return f"{match.group(1)}{match.group(2)}{PLACEHOLDER}"


def _looks_random(match: re.Match[str]) -> str:
    """A value after an unknown key that reads like a key, not like a word.

    Measured against ordinary Home Assistant lines (see the tests): model
    names - `TS0601_thermostat`, `ShellyPlus1PM` - carry a run of lowercase
    letters, a word; a generated key does not. So: ten characters or more,
    letters AND digits, and either mixed case with no run of four lowercase
    letters, or nothing but hex with at least one letter in it.
    """
    value = match.group(2)

    letters = sum(char.isalpha() for char in value)
    digits = sum(char.isdigit() for char in value)

    if len(value) < 10 or letters < 2 or digits < 2:
        return match.group(0)

    is_hex = re.fullmatch(r"[0-9a-fA-F]+", value) is not None
    mixed = any(char.islower() for char in value) and any(char.isupper() for char in value)

    if is_hex or (mixed and not re.search(r"[a-z]{4}", value)):
        return match.group(1) + PLACEHOLDER

    return match.group(0)


def _phone(match: re.Match[str]) -> str:
    """Eight to fifteen digits in the shape of a telephone number (E.164)."""
    digits = sum(char.isdigit() for char in match.group(0))

    return PLACEHOLDER if 8 <= digits <= 15 else match.group(0)


def _ipv6(match: re.Match[str]) -> str:
    """Only what really parses as an IPv6 address.

    A loose pattern also matches a time of day - `10:00:00` - and the redaction
    would eat every timestamp in a log line. Validating the candidate costs a
    function call and keeps the filter honest in both directions.
    """
    import ipaddress

    candidate = match.group(0)

    try:
        ipaddress.IPv6Address(candidate.split("%", 1)[0])
    except ValueError:
        return candidate

    return PLACEHOLDER


# Every separator a key and its value have been seen with: `=`, `:`, and the
# arrows and walrus of reprs and config dumps (`password => x`, `token -> x`),
# a pipe, and a dash with space around it (`password - x`; without the spaces
# it is the inside of `password-protected`). Unicode arrows, colons and dashes
# are turned into these by `_plain` before any pattern runs.
_SEPARATOR: Final = r"(?:\s*(?:=>|->|:=|[:=|])\s*|\s+-{1,2}\s+)"

# Words that announce a secret only when a separator follows: as bare words
# they are everywhere (`pin 4`, `key error`, `session started`). Matched as the
# LAST part of an identifier, so `local_key`, `user_code` and `session_id` are
# in and `keyboard` is not.
_SEPARATED_KEY: Final = (
    r"(?:[A-Za-z0-9_-]*[_-])?"
    r"(?:key|pin|code|session|sid|otp|nonce|salt|seed|sig|signature)"
    r"(?:[_-]?(?:id|key|code))?"
)

# Ordered on purpose: the credentials-in-a-URL pattern has to run before the
# bare-host and bare-token ones, or it only ever sees a URL that has already
# had its password turned into a placeholder.
_RAW_PATTERNS: Final[tuple[tuple[re.Pattern[str], object], ...]] = (
    # https://user:secret@host - the single most common way a credential ends
    # up in a Home Assistant traceback.
    # The password may contain a slash - `admin:pa/ss@nas` used to leave
    # `admin:pa/` behind - so it runs to the last @ before a space.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s@]*@"), r"\1" + PLACEHOLDER + "@"),

    # The host of every URL, whatever its shape: `smb://nas-rossi/`, a
    # family's own `villa-bianchi.it`, an IPv6 literal. Recognising houses by
    # their suffix (`.local`, `.duckdns.org`) missed every name without one.
    # The user part goes with it, so `https://github.com@villa-bianchi.it`
    # is not kept for starting with a public name.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^\s/?#@'\"<>]*@)?((?:\[[^\]\s]*\]|[^\s/?#:@'\"<>\[\]\\,;)])+)"), _url_host),

    # UNC paths, `\\NAS-ROSSI\backup`, also with the backslashes doubled by a
    # repr. Not after a letter or a colon: `C:\\Users` and `a\\nb` are paths
    # and escapes, not servers.
    (re.compile(r"(?<![\\\w:])(\\{2,})((?:\[[^\]\s]*\]|[^\s\\/'\"<>])+)"), r"\1" + PLACEHOLDER),

    # user:password@host with no scheme in front: `admin:Giulia2010@nas`.
    # The host goes with it - if somebody logs in to it, it is theirs.
    (re.compile(r"(?<![\w/:.@%+-])[A-Za-z0-9._~%+-]+:(?!//)[^\s'\"<>]+@(?:\[[^\]\s]*\]|[A-Za-z0-9.-])+"), PLACEHOLDER),

    # Authorization headers, whatever the scheme: `Token x`, `Digest x`,
    # `Bot x`. The key rule below stops at the space and took the scheme
    # word for the value, leaving the credential after it.
    (re.compile(
        r"(?i)(?<![A-Za-z0-9])((?:proxy-)?authorization|x-ha-access)(['\"]?\s*[:=]\s*)"
        r"(?!\[redacted\])(?:\"[^\"]*\"|'[^']*'|(?:[A-Za-z][A-Za-z0-9-]*\s+)?[^\s,;'\"}\]]+)"
    ), lambda m: f"{m.group(1)}{m.group(2)}{PLACEHOLDER}"),

    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), PLACEHOLDER),

    # Every value in a query string. Over-eager by design: `?api_key=`,
    # `&lat=45.40&lon=11.87`, `?token=`, `?sig=` - the parameter names stay so
    # the line still reads, the values go.
    (re.compile(r"([?&;][A-Za-z0-9_.\[\]-]+=)(?!\[redacted\])[^&\s#'\"<>]+"), r"\1" + PLACEHOLDER),

    # A webhook id IS the credential: whoever knows it can trigger the
    # automation behind it.
    (re.compile(r"(?i)(/api/webhook/)[^\s/?#'\"]+"), r"\1" + PLACEHOLDER),

    # Authorization headers.
    # `%` included: an encoded space inside the value must not end it.
    (re.compile(r"(?i)\b(bearer|basic)\s+(?!\[redacted\])[A-Za-z0-9._~+/=%-]+"), r"\1 " + PLACEHOLDER),

    # key = value, key: "value", "key": "value", with any separator and any
    # length of value. With an explicit separator even a three-letter value
    # goes: `password=pw` is still a password.
    (re.compile(
        r"(?i)(?<![A-Za-z0-9])(" + _SECRET_KEY + r")(['\"]?" + _SEPARATOR + r")"
        r"(?!\[redacted\])(?:\"[^\"]*\"|'[^']*'|[^\s,;&'\"}\]]+)"
    ), lambda m: f"{m.group(1)}{m.group(2)}{PLACEHOLDER}"),

    # `pass=` and `pw=`, only with an explicit separator: as bare words they
    # are in "test passed" and would eat the word after.
    (re.compile(
        r"(?i)(?<![A-Za-z0-9])((?:[A-Za-z0-9_-]*[_-])?(?:pass|pw))(['\"]?" + _SEPARATOR + r")"
        r"(?!\[redacted\])(?:\"[^\"]*\"|'[^']*'|[^\s,;&'\"}\]]+)"
    ), lambda m: f"{m.group(1)}{m.group(2)}{PLACEHOLDER}"),

    # Alarm codes, PINs, Tuya local keys, session ids - only with a separator.
    (re.compile(
        r"(?i)(?<![A-Za-z0-9])(" + _SEPARATED_KEY + r")(['\"]?" + _SEPARATOR + r")"
        r"(?!\[redacted\])(?:\"[^\"]*\"|'[^']*'|[^\s,;&'\"}\]]+)"
    ), _code),

    # Any other key whose value reads like a generated one (`_looks_random`).
    (re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9_-]*['\"]?\s*[:=]\s*['\"]?)([A-Za-z0-9+/_~-]{10,})(?![A-Za-z0-9+/_~.-])"),
     _looks_random),

    # XML and SOAP bodies, which some cameras and inverters still log whole.
    (re.compile(r"(?i)<(" + _SECRET_KEY + r"|pass|pw)>[^<]*</"), lambda m: f"<{m.group(1)}>{PLACEHOLDER}</"),

    # ...and with nothing but a space: "with password 'Giulia2010!'". Quoted
    # values always; bare ones from six characters, so that "token expired"
    # costs a word and "password for anna" keeps its sense.
    (re.compile(
        r"(?i)(?<![A-Za-z0-9])(" + _SECRET_KEY + r")\s+"
        r"(?!\[redacted\])(?:\"[^\"]*\"|'[^']*'|[A-Za-z0-9._~+/=!@#$%^*-]{6,})"
    ), lambda m: f"{m.group(1)} {PLACEHOLDER}"),

    # JSON web tokens, which are recognisable on sight and never harmless.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"), PLACEHOLDER),

    # Credentials with a shape the whole industry recognises, whatever their
    # length. The generic rules below need a long run before they fire, and an
    # AWS key is twenty characters - so without these it would sail through.
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), PLACEHOLDER),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), PLACEHOLDER),
    (re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{20,}\b"), PLACEHOLDER),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), PLACEHOLDER),
    (re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"), PLACEHOLDER),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), PLACEHOLDER),
    # Telegram bot tokens: `123456789:AAH...`.
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), PLACEHOLDER),

    # Home Assistant long-lived tokens and anything else that is a long
    # unbroken run of token-ish characters.
    (re.compile(r"\b[A-Za-z0-9_-]{40,}\b"), PLACEHOLDER),
    # Shorter, when it mixes letters and digits the way keys do and words do not.
    (re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{24,}\b"), PLACEHOLDER),
    # Hex of twenty and more: API keys, webhook ids, device serials.
    (re.compile(r"(?i)\b[0-9a-f]{20,}\b"), PLACEHOLDER),

    # Email addresses, which in this context are account names.
# Any characters a mail server accepts, accented ones included: `giulià@`
    # used to lose only the part after the accent.
    (re.compile(r"[^\s@<>()\[\]\"',;:/]+@[^\s@<>()\[\]\"',;:/]+\.[^\W\d_]{2,}\b", re.UNICODE), PLACEHOLDER),

    # Hostnames that name one house, from outside or from inside.
    (re.compile(r"(?i)\b(?:[a-z0-9-]+\.)+(?:" + _DYNAMIC_DNS + r")\b"), PLACEHOLDER),
    # Not followed by a dash either: `www.home-assistant.io` is not a host
    # called `www` on a network called `home`.
    (re.compile(r"(?i)\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:" + _LOCAL_SUFFIXES + r")(?![\w.-])"), PLACEHOLDER),

    # IPv4 addresses. All of them, including private ones: a subnet layout is
    # not ours to collect, and `ip_local` in the volatile block already carries
    # the one address that is legitimately useful.
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), PLACEHOLDER),

    # Colon or dash hex longer than a MAC: an EUI-64, a sixteen-byte network
    # key. The MAC rule took the first six groups and left the rest behind.
    (re.compile(r"\b[A-Fa-f0-9]{2}(?:[:-][A-Fa-f0-9]{2}){5,}\b"), PLACEHOLDER),

    # Zigbee IEEE addresses and other EUI-64s written as one number:
    # `0x00124b001ca1b2c3`, also glued to a suffix in a unique id. From eight
    # hex digits: a four-digit network address (`0x1a2b`) changes on every
    # rejoin and names nothing.
    (re.compile(r"(?i)(?<![0-9a-z])0x[0-9a-f]{8,}(?![0-9a-z])"), PLACEHOLDER),
    (re.compile(r"(?i)(?<![0-9a-z])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{16}(?![0-9a-z])"), PLACEHOLDER),

    # IPv6, compressed forms included (`2001:db8::1`), checked by parsing.
    (re.compile(r"(?<![A-Za-z0-9:])(?:[A-Fa-f0-9]{0,4}:){2,7}[A-Fa-f0-9]{0,4}(?:%[A-Za-z0-9]+)?(?![A-Za-z0-9:])"), _ipv6),

    # MAC addresses, in the three ways vendors write them.
    (re.compile(r"\b(?:[A-Fa-f0-9]{2}[:-]){5}[A-Fa-f0-9]{2}\b"), PLACEHOLDER),
    (re.compile(r"\b[A-Fa-f0-9]{4}\.[A-Fa-f0-9]{4}\.[A-Fa-f0-9]{4}\b"), PLACEHOLDER),
    (re.compile(r"\b[A-Fa-f0-9]{12}\b"), PLACEHOLDER),

    # Telephone numbers, which SMS and call notifiers log. International
    # with `+` or `00`; Italian mobiles (3xx) and landlines (0x) without.
    # Never after a dot, a colon or a dash, and never before one followed by
    # a digit: that is how versions, times, dates and ports are written.
    (re.compile(r"(?<![\w+.:/-])(?:\+|\b00)\d{1,3}(?:[ .\-/]?\(?\d{1,4}\)?){2,6}(?!\w)"), _phone),
    (re.compile(r"(?<![\w+.:/-])3\d{2}[ .-]?\d{3}[ .-]?\d{3,4}(?!\w|[.:-]\d)"), PLACEHOLDER),
    (re.compile(r"(?<![\w+.:/-])0\d{1,3}[ .-]?\d{5,8}(?!\w|[.:-]\d)"), _phone),

    # Coordinates: where the house is. Labelled, or as a pair.
    # A labelled pair goes whole, separator encoded or not: `lat=45.40%2C11.87`.
    (re.compile(r"(?i)\b(lat|lng|lon|latitude|longitude|latlng|location)(['\"]?\s*[:=]\s*)-?\d{1,3}\.\d+(?:\s*(?:,|%2C)\s*-?\d{1,3}\.\d+)?"),
     lambda m: f"{m.group(1)}{m.group(2)}{PLACEHOLDER}"),
    (re.compile(r"-?\b\d{1,3}\.\d{4,}\s*[,;/ ]\s*-?\d{1,3}\.\d{4,}\b"), PLACEHOLDER),

    # Wireless network names, when the message says that is what they are -
    # quoted names whole, spaces and all: "Casa Rossi" is two words and one
    # family.
    (re.compile(r"(?i)\b(ssid|essid|bssid|wifi|wi-fi|wlan|network name)\b(\s*(?:['\"]\s*[:=]\s*|[:=]\s*)?)(?!\[redacted\])(?:\"[^\"]*\"|'[^']*'|[^\s'\",;]+)"),
     lambda m: f"{m.group(1)} {PLACEHOLDER}"),
    (re.compile(r"(?i)\b(network|access point|hotspot)\b(\s*[:=]?\s*)(?:\"[^\"]*\"|'[^']*')"),
     lambda m: f"{m.group(1)} {PLACEHOLDER}"),

    # Entity ids: the domain stays, the household's name for the thing goes.
    # Also behind a dot - `states.sensor.camera_giulia`, the logger
    # `homeassistant.components.automation.sveglia_giulia` - except where
    # what follows is one of the component's own modules.
    (re.compile(
        r"(?<![A-Za-z0-9_])(" + _ENTITY_DOMAINS + r")\.(?!(?:py|" + _COMPONENT_MODULES + r")\b)"
        r"(?!\[redacted\])[a-z0-9_]+\b"
    ),
     lambda m: f"{m.group(1)}.{PLACEHOLDER}"),
)


def _ascii(pattern: re.Pattern[str]) -> re.Pattern[str]:
    """Word boundaries in ASCII, not Unicode.

    In Unicode mode an accented letter is a word character, so a token glued
    to Italian text - `tokenè` followed by forty characters - had no `\b` in
    front of it and the long-token rule never fired. Every secret we look for
    is ASCII; the boundaries around it have to be too. The email rule is the
    one exception, because an address can carry an accent.
    """
    if pattern.flags & re.UNICODE and "[^\\W" in pattern.pattern:
        return pattern

    return re.compile(pattern.pattern, (pattern.flags & ~re.UNICODE) | re.ASCII)


_PATTERNS: Final = tuple((_ascii(pattern), replacement) for pattern, replacement in _RAW_PATTERNS)

_PERCENT: Final = re.compile(r"%[0-9A-Fa-f]{2}")

_ENTITY: Final = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")

# Arrows (Arrows, Supplemental Arrows A/B, Dingbat arrows, Misc Symbols and
# Arrows), colon look-alikes, equals look-alikes, dashes and minus signs.
_ARROWS: Final = re.compile("[←-⇿⟰-⟿⤀-⥿➔-➿⬀-⬯⮕]")
_COLONS: Final = re.compile("[∶꞉˸ː׃։፡᠄⁚⸬]")
_EQUALS: Final = re.compile("[≐≑≔≕≙≜≝≡≣⩴⩵⩶]")
_DASHES: Final = re.compile("[‐-―−⸺⸻﹘﹣〜]")

MAX_MESSAGE_LENGTH: Final = 500

# What redact() reads at most. Every caller keeps at most half of this, and some
# patterns are quadratic on long runs of separators: a 10,000-character line
# took seconds, in the event loop, once per heartbeat. The tail is dropped,
# never sent.
MAX_INPUT_LENGTH: Final = 1000

MAX_MESSAGES: Final = 20


def _plain(message: str) -> str:
    r"""The same characters a reader will see, in the forms the patterns know.

    The patterns are ASCII (see `_ascii`), so everything that READS as ASCII
    to a person has to BE ASCII before they run - otherwise switching to ASCII
    boundaries, which closed one bypass, opened three (regression found by the
    second security review of 2f03c20):

    - compatibility forms: fullwidth `１９２.１６８.１.４０`, `paſſword` (long s),
      the Kelvin sign. NFKC turns them into what they look like.
    - digits from other scripts, `١٩٢.١٦٨.١.٤٠`: a reader still reads an
      address. Turned into ASCII digits.
    - Unicode spaces, `password\u2003hunter22`: an em space is a space to the
      reader and was not `\s` to an ASCII pattern. Turned into plain spaces.
    - escaped quotes: a JSON document logged inside a string is
      `{\"password\": \"x\"}`, and the backslash stopped the key pattern.
    """
    # Fourth review: characters a reader never sees. Terminal colour codes -
    # Home Assistant's own log is coloured - and zero-width or soft-hyphen
    # characters (Unicode category Cf). `pass​word=x` reads as
    # `password=x` and matched nothing; `\x1b[31mtoken\x1b[0m=x` the same.
    message = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", message)
    # Review after D40: HTML entities, `{&quot;password&quot;: ...}`, from a
    # body logged as it came off a web page. Only the terminated form: the
    # legacy `&para` without a semicolon would turn `?a=1&param=x` into
    # `?a=1¶m=x`, and the query-string rule would lose the parameter. Twice,
    # for `&amp;quot;`.
    for _ in range(2):
        message = _ENTITY.sub(lambda m: html.unescape(m.group(0)), message)
    message = "".join(char for char in message if unicodedata.category(char) != "Cf")
    message = unicodedata.normalize("NFKC", message)
    message = "".join(
        str(unicodedata.decimal(char)) if not char.isascii() and unicodedata.decimal(char, None) is not None else char
        for char in message
    )
    # ...and separators a reader reads as `->`, `:`, `=` or `-`, which NFKC
    # leaves alone: `password → x`, `password ∶ x`, `password — x`.
    message = _ARROWS.sub("->", message)
    message = _COLONS.sub(":", message)
    message = _EQUALS.sub("=", message)
    message = _DASHES.sub("-", message)
    message = re.sub(r"[^\S\n]", " ", message)

    return message.replace("\\\"", '"').replace("\\'", "'")


def _known_pattern(name: str) -> str | None:
    """A known name, matched however it is spelled in the log.

    Matching the literal string missed every other spelling of the same name:
    "Casa Rossi" as `casa_rossi`, `casa-rossi`, `CasaRossi`, `Casa  Rossi`, or
    with a decomposed accent - the reader sees one name, the filter saw none.
    So the name is cut into its words, normalised like the message, and the
    words may be joined by any run of separators, or by none.
    """
    words = re.findall(r"[^\W_]+", _plain(name))

    if sum(len(word) for word in words) < 3:
        return None

    return r"[\W_]*".join(re.escape(word) for word in words)


@functools.lru_cache(maxsize=8)
def _known_regexes(known: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """Every known name as its own pattern, compiled once per set of names.

    Compiled once rather than on every call: a house knows a few hundred names
    (D41), and this runs for every field of every heartbeat, in Home
    Assistant's event loop. Not one alternation, though: an alternation takes
    one match per position, so with "Giulia Rossi" and "Camera di Giulia"
    known, "Camera di Giulia Rossi" came out as "[redacted] Rossi".
    """
    return tuple(
        re.compile(pattern, re.IGNORECASE)
        for name in sorted({name for name in known if name})
        if (pattern := _known_pattern(name)) is not None
    )


def _apply(message: str, known: tuple[str, ...]) -> str:
    # Every match of every name, overlapping ones merged, then replaced once.
    spans = sorted(
        match.span() for regex in _known_regexes(tuple(known)) for match in regex.finditer(message)
    )

    if spans:
        merged: list[list[int]] = []

        for begin, finish in spans:
            if merged and begin <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], finish)
            else:
                merged.append([begin, finish])

        for begin, finish in reversed(merged):
            message = message[:begin] + PLACEHOLDER + message[finish:]

    for pattern, replacement in _PATTERNS:
        message = pattern.sub(replacement, message)

    return message


def redact(message: str, known: tuple[str, ...] = ()) -> str:
    """Run one message through every pattern.

    `known` are the strings this instance already knows identify it - the
    name the household gave it, the hostnames of its own addresses. No pattern
    can tell that "Casa Rossi" is a family name; the instance can, so it says.

    Percent-encoding is handled in TWO passes, and the order is the point. The
    first pass reads the text as it is, so `token=abc%20def` is one value and
    goes whole. Only then is it decoded and read again, which catches what
    encoding hid - `token%3Dabc`. Decoding first, as 2f03c20 did, turned
    `%20` into a space INSIDE a value and the value ended there: `def` came
    out the other side. That was a regression on 334f710, and it is the
    reason for this order.

    Our own placeholder sent IN is defused before anything runs: the patterns
    skip a value that already reads `[redacted]`, so as not to redact twice,
    and a value that merely began with that text was skipped whole.
    """
    if not message:
        return ""

    # Cut before AND after normalising: NFKC can turn one character into
    # eighteen (U+FDFA), so a cut made only before is not a cap.
    message = _plain(message[:MAX_INPUT_LENGTH])[:MAX_INPUT_LENGTH].replace(PLACEHOLDER, "(redacted)")
    message = _apply(message, known)

    for _ in range(2):
        if not _PERCENT.search(message):
            break

        message = _apply(_plain(unquote(message))[:MAX_INPUT_LENGTH], known)

    return message.strip()


def summarise(
    entries: list[dict[str, object]],
    level: str,
    errors: int,
    warnings: int,
    known: tuple[str, ...] = (),
) -> dict[str, object]:
    """Build the `logs` block at the level the household chose.

    `entries` are already-shaped records: level, source, message, at, count.
    """
    summary: dict[str, object] = {
        "level": level if level in (LOG_LEVEL_COUNTS, LOG_LEVEL_CATEGORIES, LOG_LEVEL_FULL) else LOG_LEVEL_COUNTS,
        "errors": max(0, int(errors)),
        "warnings": max(0, int(warnings)),
    }

    if summary["level"] == LOG_LEVEL_COUNTS:
        # `recent` is absent, not empty. The schema allows it to be missing and
        # the difference says something: nobody chose to send messages.
        return summary

    recent: list[dict[str, object]] = []

    for entry in entries[:MAX_MESSAGES]:
        message = str(entry.get("message", ""))

        if summary["level"] == LOG_LEVEL_CATEGORIES:
            # First line only. A stack trace's first line says what broke; the
            # forty after it say where our own code was standing at the time,
            # and are where the secrets live.
            message = message.splitlines()[0] if message else ""

        redacted = redact(message, known)[:MAX_MESSAGE_LENGTH]

        record: dict[str, object] = {
            "level": str(entry.get("level", "error")),
            # A source is an integration domain: `zha`, `mqtt`. It says which
            # protocol broke and nothing about the household - usually. It is
            # also a logger name or a file name, which somebody's own
            # python_script can make `giulia_alarm.py`, so it goes through the
            # same filter as the message.
            "source": redact(str(entry.get("source", "unknown")), known)[:120] or "unknown",
            "message": redacted,
            "at": str(entry.get("at", "")),
        }

        count = entry.get("count")

        if isinstance(count, int) and count > 1:
            record["count"] = count

        recent.append(record)

    summary["recent"] = recent

    return summary
