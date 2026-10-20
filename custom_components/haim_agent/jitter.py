"""When, inside its interval, this instance sends.

The offset is derived from the site's own identifier and is therefore the same
after every restart:

    offset = crc32(site_uuid) % interval

Never random. The day HACS pushes an update of this component to three hundred
houses, every one of them restarts within a few minutes of the others - and with
a random offset chosen at startup they would all pick a new one and still land
in the same few minutes. A deterministic offset means each house keeps the slot
it has always had, and the load stays spread whatever happens to the fleet.
"""

from __future__ import annotations

from zlib import crc32


def offset_seconds(site_uuid: str, interval_seconds: int) -> int:
    """A stable position inside the interval, in [0, interval)."""
    if interval_seconds <= 0:
        return 0

    return crc32(site_uuid.encode("utf-8")) % interval_seconds
