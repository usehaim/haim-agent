# Changelog

Every release says what changed and, separately, whether anything in it was a
**security fix**. A fix buried in "various improvements" is a fix nobody
downstream can decide to prioritise, and under the Cyber Resilience Act that is
not a style preference.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- First version of the component: pairing against a HAIM panel, the telemetry it
  reports, the offline queue, and the command channel.
- Released under Apache-2.0.

### Changed (aligned with the panel of 14/10/2026)
- Pairing codes are twelve characters, shown in three groups of four. Spaces,
  dashes and lower case are accepted and removed before the code is sent.
- A rotated agent key (`X-HAIM-Token-Rotate`) is written to the config entry
  first and used only on a later answer. Kept in memory only, it was lost at the
  next restart, and the house was locked out when the old key's overlap ended.
- A `429` is waited out per kind of call, for `Retry-After` plus jitter. The
  event queue loses nothing and counts no strike while it waits; a limit on
  events does not silence the command poll.
- An event stamped more than five minutes ahead of the panel's time is
  re-stamped with the panel's time before it is sent: the panel drops anything
  more than ten minutes in its future.
- Event payloads are cut to under 4 KB (as the panel measures them) in the
  house, keeping what fits and marking it `"truncated": true`.
- Event types: the agent never sends the panel's own vocabulary
  (`went_offline`, `went_frozen`, `degraded`, `came_online`, `site_unstable`,
  `monitoring_disabled`, `never_seen`, `alert_fired_*`, `alert_cleared_*`). The
  queue refuses them. The agent's types name what only the house can see, such
  as `backup_failed`, `integration_error`, `update_available`.
- `close_session` closes only the window its `session_uuid` names (or, with
  nothing open or no `session_uuid`, parks the account as before). A close for
  another window is ignored and reported as `{"closed": false, "ignored":
  "not_the_open_session"}`. `close_reason` is kept only if it is a plain word
  (`user_removed` included); it is shown nowhere in the house.

### Security
- No security fixes in this release (first release).
