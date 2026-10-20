"""The component's own version, in one place.

Read from manifest.json rather than repeated here, so that a release cannot
ship a manifest saying one thing and a heartbeat saying another - which is
exactly the kind of mismatch nobody notices until they are trying to work out
which version broke a fleet.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path


@cache
def _manifest() -> dict[str, str]:
    path = Path(__file__).parent / "manifest.json"

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


AGENT_VERSION: str = _manifest().get("version", "0.0.0")
