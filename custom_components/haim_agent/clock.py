"""The house's clock, and what to do when it is plainly wrong.

Events carry `occurred_at` from the clock of the machine Home Assistant runs
on. A Raspberry Pi has no battery-backed clock: without NTP it boots in 1970,
or at the date `fake-hwclock` saved at the last clean shutdown, weeks ago. The
panel stores events in monthly partitions, and a 1970 event has no partition
to go into - the whole batch fails, and is sent again, and fails, forever.

The panel's own time arrives on every answer as `X-HAIM-Server-Time`. So:

    A local time is IMPOSSIBLE when it is before this component existed, or
    more than a day away from the server's time (estimated from the last
    header plus the monotonic time elapsed since - the monotonic clock does
    not jump when NTP finally corrects the wall clock).

When the local time is impossible and a server reference is known, the
server's time is used instead and the household is told with a repair issue:
a machine with a wrong clock has other problems too - certificates, backups,
automations at the wrong hour - and they deserve to know. When no reference is
known yet, there is nothing better to use; the event is corrected at the next
flush, which always comes right after an answer from the server.

A day either way, and not a minute: this is for clocks that are plainly
wrong, not for a drift of a few seconds, which the panel absorbs.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Callable, Final

# Before this, the component did not exist; a clock that says otherwise is
# wrong. Earlier than any build that carries this file, on purpose.
NOT_BEFORE: Final = datetime(2026, 10, 1, tzinfo=UTC)

MAX_SKEW: Final = timedelta(days=1)

# How far ahead of the panel an event may be stamped. The panel refuses an
# `occurred_at` more than ten minutes in its future, and answers it as a
# duplicate so the queue lets go of it (panel-b, point 2): stamped by a house
# clock half an hour fast, every event would be thrown away without a word.
# Half the panel's margin, so a send that takes a while still lands inside.
FUTURE_TOLERANCE: Final = timedelta(minutes=5)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse(value: object) -> datetime | None:
    """An ISO 8601 instant, or None. Never raises."""
    if not isinstance(value, str) or not value:
        return None

    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


class HouseClock:
    """Local time, unless it is impossible; then the server's."""

    def __init__(
        self,
        wall: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._wall = wall or (lambda: datetime.now(tz=UTC))
        self._monotonic = monotonic or time.monotonic
        self._server: datetime | None = None
        self._server_at: float = 0.0

    def observe(self, header: object) -> None:
        """Remember the server's time from an `X-HAIM-Server-Time` header."""
        moment = parse(header)

        if moment is not None:
            self._server = moment
            self._server_at = self._monotonic()

    def server_now(self) -> datetime | None:
        if self._server is None:
            return None

        return self._server + timedelta(seconds=self._monotonic() - self._server_at)

    def plausible(self, moment: datetime) -> bool:
        if moment < NOT_BEFORE:
            return False

        reference = self.server_now()

        return reference is None or abs(moment - reference) <= MAX_SKEW

    def in_the_future(self, moment: datetime) -> bool:
        """Later than the panel's now, by more than it will take."""
        reference = self.server_now()

        return reference is not None and moment > reference + FUTURE_TOLERANCE

    @property
    def skewed(self) -> bool:
        """Whether the local clock is impossible right now."""
        return not self.plausible(self._wall())

    @property
    def can_correct(self) -> bool:
        return self._server is not None

    def now(self) -> datetime:
        local = self._wall()

        if self.plausible(local):
            return local

        return self.server_now() or local

    def stamp(self) -> tuple[datetime, bool]:
        """The time to put on an event, and whether it is VERIFIED (D44).

        Verified: the panel's time is known and the stamp agrees with it -
        either the local clock is within a few minutes of it, or the local
        clock was impossible and the panel's time was used. Such a stamp is
        kept as it is however late the event is sent: a house offline for two
        days sends events that are two days old, and they are.

        Not verified: nothing to check against yet, or a local clock that is
        plausible but off by more than minutes - `fake-hwclock` restarting
        weeks ago. Those are judged again when they are sent.
        """
        local = self._wall()
        reference = self.server_now()

        if reference is None:
            return local, False

        if not self.plausible(local):
            return reference, True

        return local, abs(local - reference) <= FUTURE_TOLERANCE
