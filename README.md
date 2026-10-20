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
Home Assistant, then add the integration. You will need the twelve character
pairing code your installer gives you (shown in three groups of four; spaces,
dashes and lower case are fine), and their HAIM address — the component has no
address built into it, which is why it always asks.

## Answering a request to come in

When your installer asks to connect, the question appears in **Settings →
Repairs** (and, if the companion app is installed, as a notification on your
phone). Open it and choose yes or no. Nothing happens until you answer, and a
yes lasts only for the time you allowed, counted from your yes.

While it lasts, **Maintenance access active** is on and **Maintenance access
ends** shows when it closes. Your installer can leave and come back as often as
they need in that time; at the end the maintenance account is switched off and
every key it had is revoked, even if Home Assistant was off at that moment.

## Security

See [SECURITY.md](SECURITY.md) for how to report a vulnerability and what this
component can reach.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
