# Security policy

## Supported versions

The HAIM agent follows the version of the agent API contract it speaks. The
server accepts agents up to two minor versions behind the current one, so the
two most recent minor releases are supported at any time.

| Version | Supported |
|---|---|
| 1.0.x | ✅ |

## Reporting a vulnerability

Write to **security@usehaim.com**.

Please report privately and give us a chance to fix it before it is public. Do
not open a GitHub issue for a security problem.

What helps most, in order: what you can do with it, how to reproduce it, the
version you tested, and whether it needs the pairing code or an issued key.

What we commit to:

| Stage | Target |
|---|---|
| We acknowledge your report | 3 working days |
| We tell you whether it is confirmed and how serious we think it is | 10 working days |
| Fix released, or a date for one | 90 days |

We will credit you in the changelog unless you ask us not to.

## What this component can reach

Being clear about the blast radius is part of the report being useful:

- It holds one credential, the agent key, which identifies this instance to the
  HAIM panel that monitors it. It is stored in the Home Assistant config entry.
- It sends counts, versions, system metrics, integration domains and add-on
  slugs. It does not send entity ids, device or area names, states, history,
  energy data, images, or anything about the people in the house.
- Log messages, when the household has chosen to send them, pass through a
  redaction filter first: tokens, email addresses, IP addresses, network names
  and URLs containing credentials are removed before anything leaves.
- It never disables TLS certificate verification. There is no flag, no
  environment variable and no debug branch that does.

## Security fixes in the changelog

Every release note marks security fixes explicitly, with a severity and a CVE
where one exists. A fix that is only described as "various improvements" is a
fix nobody can decide to prioritise.
