"""The canonical form of the state block, and its hash.

One rule, and the whole contract rests on it:

    state_hash = sha256 of the canonical JSON of the `state` block, and nothing
    else.

Canonical means keys sorted at every level, no whitespace, non-ASCII escaped -
that is, exactly what `json.dumps(state, sort_keys=True, separators=(",", ":"))`
produces with its defaults. The server recomputes the same thing in PHP and the
two have to agree byte for byte; there is a test on each side that pins the
result against a real installation's payload and the hash published in the contract
README, because if they ever drift the only symptom is that a fleet quietly
starts costing ten times as much to run.

The volatile block is not in here and must never be. If a field that changes on
every send - CPU, memory, uptime, an address - ends up inside `state`, the hash
differs every time, the server's "nothing changed" branch never fires, and
ninety per cent of the write saving is gone.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(state: dict[str, Any]) -> str:
    """The exact bytes both sides hash."""
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def state_hash(state: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(state).encode("utf-8")).hexdigest()
