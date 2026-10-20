# HAIM agent

The Home Assistant component of **HAIM — instance manager for Home Assistant**.

It tells the installer who maintains this instance how it is doing. This
repository is public so that what it sends can be read rather than believed.

## What it sends

Counts, versions and system metrics. Specifically: Home Assistant and add-on
versions, the installation type, how many entities, devices, automations and
dashboards exist, how many entities are unavailable, how many integrations
failed to start, how many repair issues are open, whether the last backup
worked, which updates are pending, and CPU, memory and disk usage.

Integration **domains** (`zha`, `mqtt`) and add-on **slugs** travel too. A
domain says which protocol is in use; it says nothing about the household.

## What it never sends

Entity ids. Device names. Room and area names. States. History. Energy figures.
Images. Anything about the people who live here.

Error log messages are sent only if the household chooses to, and pass through a
redaction filter first: tokens, email addresses, IP addresses, network names and
URLs containing credentials are removed before anything leaves the house. The
choice is between counts only, counts and categories, or full messages.

## Installing

Add this repository to HACS as a custom repository, install **HAIM**, restart
Home Assistant, then add the integration. You will need the eight character
pairing code your installer gives you, and their HAIM address — the component
has no address built into it, which is why it always asks.

## Security

See [SECURITY.md](SECURITY.md) for how to report a vulnerability and what this
component can reach.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
