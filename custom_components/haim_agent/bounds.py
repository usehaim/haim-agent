"""Lengths the far side can hold, applied before anything leaves.

The panel keeps `ha_version` and `agent_version` in varchar(20) columns. A
development build of Home Assistant reports itself as
`2026.11.0.dev202610140301` - twenty-five characters - and the insert fails.
Not once: on every heartbeat, forever, because the version does not change
until somebody updates. The instance looks offline and nothing in the house
says why.

The cause is the column, and the panel has to widen it (docs/DOMANDE-APERTE.md).
This file is the half that lives here, because an agent installed in a house is
the part that cannot wait for the server to be fixed: it must never send
anything a server two minor versions old would choke on.

Two rules, and the second is the one that matters:

1. Every free-form string has a ceiling.
2. A version is SHORTENED, never cut. `2026.11.0.dev202610140301` becomes
   `2026.11.0.dev`: still a development build of 2026.11.0, which is the truth.
   Cutting it at twenty characters would give `2026.11.0.dev2026101`, which
   reads like a real build number and is not one - and a version that lies is
   worse than a version that is missing, because an advisory gate reasons on it.
"""

from __future__ import annotations

import re
from typing import Any, Final

# The panel's columns, today. ha_version and agent_version: varchar(20).
VERSION_MAX: Final = 20

# update_commands.from_version / to_version: varchar(40).
UPDATE_VERSION_MAX: Final = 40

# Everything else is stored inside jsonb, which has no column limit - but a
# string with no ceiling is a string somebody's integration will one day fill
# with a stack trace.
SHORT_TEXT_MAX: Final = 100
LONG_TEXT_MAX: Final = 255
URL_MAX: Final = 500

# The parts of a version that are a label plus a long number: `dev202610140301`,
# `b3`, `rc12`, `post1`. When there is not room for the number, the label is
# kept - it is the label that says "this is not a release".
# Home Assistant writes its betas glued to the patch number: `2026.10.0b3`.
_LABELLED: Final = re.compile(r"^(\d*(?:dev|a|alpha|b|beta|rc|c|pre|preview|post))(\d+)$", re.IGNORECASE)

_SEPARATOR: Final = re.compile(r"([.+\-_ ])")


def text(value: Any, limit: int = LONG_TEXT_MAX) -> str | None:
    """A free-form string, capped. None stays None."""
    if value is None:
        return None

    return str(value)[:limit]


def version(value: Any, limit: int = VERSION_MAX) -> str | None:
    """A version string that fits, without inventing a version that does not exist.

    Whole parts only. If the full string does not fit, the long number after a
    pre-release label is dropped and the label kept; if that still does not
    fit, trailing parts are dropped whole. A number is never cut in half.
    """
    if value is None:
        return None

    raw = str(value).strip()

    if len(raw) <= limit:
        return raw

    parts = _SEPARATOR.split(raw)

    # First pass: shorten the labelled numbers, keep everything else.
    shortened = []
    labels: list[int] = []

    for index, part in enumerate(parts):
        labelled = _LABELLED.match(part)

        if labelled:
            labels.append(index)

        shortened.append(labelled.group(1) if labelled else part)

    candidate = "".join(shortened)

    if len(candidate) <= limit:
        return candidate

    # Second pass: whole parts from the left while they fit. Separators only
    # ever travel with the part after them, so nothing ends in a dangling dot.
    kept = ""
    reached = -1

    for index in range(0, len(shortened), 2):
        piece = (shortened[index - 1] if index else "") + shortened[index]

        if len(kept) + len(piece) > limit:
            break

        kept += piece
        reached = index

    if any(label > reached for label in labels):
        # Dropping `dev` would turn a development build into what reads as a
        # release. Missing is honest; that would not be.
        return None

    # A first part longer than the limit - not a version anyone has shipped -
    # is better reported as nothing than as a fragment.
    return kept or None
