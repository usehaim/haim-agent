"""Constants for the HAIM agent.

HAIM is an instance manager for Home Assistant. This component reports how a
Home Assistant instance is doing to a cloud panel run by the installer who
maintains it.

What it never reports, under any circumstance: entity ids, device names, room
or area names, states, history, energy figures, images, or the people who live
in the house. Only counts, system metrics, integration domains and add-on slugs
travel over this channel. That is not a policy note - it is the reason the
product can say "we do not see what happens in your house, we see whether your
system is healthy" and have it be true.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "haim_agent"

# Config entry keys.
CONF_BASE_URL: Final = "base_url"
CONF_PAIRING_CODE: Final = "pairing_code"
CONF_AGENT_KEY: Final = "agent_key"
CONF_SITE_UUID: Final = "site_uuid"
CONF_LOCALE: Final = "locale"
CONF_LOG_LEVEL: Final = "log_level"

# The base URL is asked for and never hardcoded. That single decision is what
# makes staging, on-premise installs and white-label resellers possible without
# building a different component for each of them.

API_PREFIX: Final = "/api/v1/agent"

# Defaults, all overridable by the `config` block the cloud sends back. The
# point of that block is that intervals can be changed across a whole fleet
# without shipping a release to three hundred houses and waiting for HACS.
DEFAULT_STATE_INTERVAL: Final = 300
DEFAULT_POLL_INTERVAL: Final = 30
DEFAULT_EVENT_BATCH_MAX: Final = 50

# How much of a log message leaves the house. The customer chooses.
LOG_LEVEL_COUNTS: Final = "counts"
LOG_LEVEL_CATEGORIES: Final = "categories"
LOG_LEVEL_FULL: Final = "full"
LOG_LEVELS: Final = (LOG_LEVEL_COUNTS, LOG_LEVEL_CATEGORIES, LOG_LEVEL_FULL)
# Counts only, unless the house chooses more (D41). The first line of an error
# carries automation aliases, entity ids and phone numbers often enough that
# "categories" cannot be what a family gets without being asked.
DEFAULT_LOG_LEVEL: Final = LOG_LEVEL_COUNTS

# Response headers the cloud sets on every answer.
HEADER_SERVER_TIME: Final = "X-HAIM-Server-Time"
HEADER_CONFIG_VERSION: Final = "X-HAIM-Config-Version"
HEADER_TOKEN_ROTATE: Final = "X-HAIM-Token-Rotate"
HEADER_STATE_HASH: Final = "X-State-Hash"

# Capabilities we negotiate. The cloud DEGRADES what it offers based on these -
# it never refuses an instance for lacking one. A Container install has no
# Supervisor and therefore no add-on management, and that is a smaller product,
# not a broken one.
CAPABILITY_SUPERVISOR: Final = "supervisor"
CAPABILITY_HACS: Final = "hacs"
CAPABILITY_BACKUP_AGENTS: Final = "backup_agents"
CAPABILITY_MOBILE_APP: Final = "mobile_app"
CAPABILITY_SYSTEM_LOG: Final = "system_log"

# Offline queue. Capped, oldest discarded first: an instance back from three
# days offline must not be able to push tens of thousands of events at once.
EVENT_QUEUE_MAX: Final = 200

# How many strikes an event gets before it is dropped (queue.py). A strike is
# only ever counted right after a heartbeat got through, so ten is ten answered
# refusals - fifty minutes of a server that is up and says no - and never ten
# minutes of a house without a network.
EVENT_MAX_ATTEMPTS: Final = 10

# The largest event payload the panel stores, measured as PHP's json_encode
# writes it. Past that it keeps the event and replaces the payload with
# {"truncated": true}; cutting it here first keeps what matters (panel-b,
# point 2). A little under the panel's 4096, for what PHP spells differently.
EVENT_PAYLOAD_MAX_BYTES: Final = 4000
STORAGE_VERSION: Final = 1
STORAGE_KEY_QUEUE: Final = f"{DOMAIN}.queue"

# The deadline of an open remote session, on DISK and not in memory (§11.3).
# The competing product keeps its timer in memory, so a Home Assistant restart
# during a session loses it. A restart must never be a way to lengthen - or
# accidentally shorten - a window somebody agreed to.
STORAGE_KEY_SESSION: Final = f"{DOMAIN}.session"

# The one request that may be waiting for an answer, on DISK (D33). It used to
# live only in memory, so a Home Assistant restart while somebody was deciding
# left the card on their screen and nothing behind it: they tapped and got an
# error, and the installer had to ask again.
STORAGE_KEY_CONSENT: Final = f"{DOMAIN}.consent"

# How old a restored request may be before this house stops offering it.
# Generous on purpose: the panel gives a request sixty minutes and the panel is
# the authority on whether it is still answerable - this only stops a question
# living on a family's screen forever if the two ever lose touch.
CONSENT_MAX_AGE_MINUTES: Final = 90

# When the household was last told what leaves their house. The notice used to
# come back at every restart, which is how a text meant to be read becomes a
# text people click away without reading. Once a week is often enough to stay
# honest and rare enough to still be read (D32).
STORAGE_KEY_NOTICE: Final = f"{DOMAIN}.notice"
NOTICE_EVERY_DAYS: Final = 7

# The account the installer works as while a window is open. Parked
# (is_active=False) the rest of the time, which in Home Assistant also destroys
# every token it had - which is why a new one is minted per session (D27).
MAINTENANCE_USERNAME: Final = "haim-maintenance"

MAINTENANCE_NAME: Final = "HAIM Manutenzione"

# The id Home Assistant gave that account, on DISK. A name is something the
# household can change in Settings - and an account looked up by name that has
# been renamed is an account that is never parked again, with a second one
# created next to it. The id cannot be changed by anybody (D37).
STORAGE_KEY_MAINTENANCE: Final = f"{DOMAIN}.maintenance"

# The key of the pseudonyms in `updates` and `addons` (D40). In the config
# entry, never in a payload.
CONF_PSEUDONYM_KEY: Final = "pseudonym_key"

# The longest window this house will open, whatever the cloud says. The panel
# offers six hours at most and the household can only shorten it (D28); this is
# the same ceiling, enforced where the account actually lives, so a cloud that
# is wrong - or compromised - cannot hand out a longer one (D37).
MAX_WINDOW_MINUTES: Final = 360

# While a window is open the house asks the panel whether it still is (D51,
# contract 1.5.0). Not otherwise: a house with nothing open asks nothing.
SESSION_SYNC_SECONDS: Final = 30

# The one shape an agent key ever takes (panel: AgentKey::parse). A rotated key
# in any other shape is not one the panel minted, and persisting it would lock
# this house out the day the old key ends its overlap.
AGENT_KEY_PATTERN: Final = r"haim_[a-z0-9]{8}_[A-Za-z0-9]{32}"

# How long a rotated key sits in the config entry before it is used. Home
# Assistant writes config entries to disk a moment after they change (an
# atomic write, delayed by about a second); the new key is only used once that
# write has had ample time to land. Until then the old key keeps working - the
# panel overlaps the two for seven days and repeats the new one on every answer
# until it is used.
ROTATED_KEY_ADOPT_AFTER_SECONDS: Final = 10

# When the key is refused for good, stop trying and tell the household.
ISSUE_TOKEN_REVOKED: Final = "token_revoked"
ISSUE_SITE_INACTIVE: Final = "site_inactive"

# The house's clock is plainly wrong (clock.py): before this component existed,
# or more than a day away from the panel's.
ISSUE_CLOCK_SKEWED: Final = "clock_skewed"

# How long to wait before trying again after a 410 (archived instance, lapsed
# plan). Six hours, not thirty seconds: nothing that happens in the house will
# change the answer.
PAUSED_RETRY_SECONDS: Final = 6 * 60 * 60
