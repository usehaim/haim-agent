"""CPU, memory and disk, with an ordered fallback.

Three sources, tried in order, because no single one exists everywhere:

    1. Supervisor  - on OS and Supervised installs, the most accurate figures
                     and the only ones that include the host rather than just
                     the container.
    2. psutil      - Home Assistant ships it, so it is always importable, but
                     inside a container it reports the container's view.
    3. /proc       - the floor. Works on anything Linux, wrong on macOS, and
                     that is fine: nobody runs a customer's instance on macOS.

CPU sampling has a window of its own and runs OFF the event loop. The naive
call - psutil.cpu_percent() with no interval - returns the average since the
process started, which after a month of uptime is a number that never moves.
The correct call blocks for the length of its window, and blocking the event
loop for a second every five minutes would make the whole instance stutter.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Long enough to mean something, short enough that the executor thread is not
# tied up for any noticeable time.
CPU_SAMPLE_SECONDS = 1.0


async def async_collect_metrics(hass: HomeAssistant) -> dict[str, Any]:
    """The volatile resources block. Every field may be None."""
    metrics: dict[str, Any] = {
        "cpu": None,
        "ram": None,
        "disk": None,
        "disk_total_gb": None,
        "disk_used_gb": None,
        "db_size_mb": None,
    }

    supervisor = await _async_from_supervisor(hass)

    if supervisor:
        metrics.update({key: value for key, value in supervisor.items() if value is not None})

    if any(metrics[key] is None for key in ("cpu", "ram", "disk")):
        local = await hass.async_add_executor_job(_from_psutil, hass.config.path())

        for key, value in local.items():
            if metrics.get(key) is None and value is not None:
                metrics[key] = value

    metrics["db_size_mb"] = await hass.async_add_executor_job(_database_size_mb, hass.config.path())

    return metrics


async def async_uptime_seconds(hass: HomeAssistant) -> int | None:
    """How long the Home Assistant process has been running.

    Home Assistant's uptime and not the host's, deliberately: the question an
    installer is asking is "did Home Assistant restart". On a Container install
    the host may have been up for a year while Core restarted nine times last
    night, and it is the nine that matter.
    """
    try:
        return int(await hass.async_add_executor_job(_process_uptime))
    except Exception:  # noqa: BLE001 - one missing field, never a failed heartbeat
        _LOGGER.debug("Could not determine uptime", exc_info=True)

        return None


async def _async_from_supervisor(hass: HomeAssistant) -> dict[str, Any] | None:
    """Ask the Supervisor, where there is one."""
    try:
        from homeassistant.components.hassio import get_host_info

        from .system import supervisor_present

        if not supervisor_present(hass):
            return None

        host = get_host_info(hass) or {}

        total = _number(host.get("disk_total"))
        used = _number(host.get("disk_used"))

        return {
            "disk": round(used / total * 100, 1) if total and used is not None else None,
            "disk_total_gb": total,
            "disk_used_gb": used,
        }
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Supervisor metrics unavailable", exc_info=True)

        return None


def _from_psutil(config_path: str) -> dict[str, Any]:
    """Runs in an executor thread: the CPU sample blocks for a full second."""
    metrics: dict[str, Any] = {}

    try:
        import psutil

        # With an interval, so it measures the last second rather than the
        # average since boot - which stops moving after a week of uptime.
        metrics["cpu"] = round(psutil.cpu_percent(interval=CPU_SAMPLE_SECONDS), 1)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not sample CPU", exc_info=True)

    try:
        import psutil

        metrics["ram"] = round(psutil.virtual_memory().percent, 1)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not read memory", exc_info=True)

    try:
        import psutil

        usage = psutil.disk_usage(config_path)
        metrics["disk"] = round(usage.percent, 1)
        metrics["disk_total_gb"] = round(usage.total / 1_000_000_000, 1)
        metrics["disk_used_gb"] = round(usage.used / 1_000_000_000, 1)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not read disk usage", exc_info=True)

    if "cpu" not in metrics:
        metrics["cpu"] = _cpu_from_proc()

    return metrics


def _cpu_from_proc() -> float | None:
    """The floor: load average over the first minute, as a rough percentage."""
    try:
        import os

        load, _, _ = os.getloadavg()
        cores = os.cpu_count() or 1

        return round(min(100.0, load / cores * 100), 1)
    except Exception:  # noqa: BLE001
        return None


def _process_uptime() -> float:
    import os
    import time

    import psutil

    return time.time() - psutil.Process(os.getpid()).create_time()


def _database_size_mb(config_path: str) -> float | None:
    """The recorder database, if it is the default SQLite one.

    Worth reporting because it is the file that quietly fills a disk: a busy
    instance with the default retention reaches a gigabyte inside a year, and
    nobody notices until backups start failing.
    """
    try:
        from pathlib import Path

        database = Path(config_path) / "home-assistant_v2.db"

        if not database.is_file():
            return None

        return round(database.stat().st_size / 1_000_000, 1)
    except Exception:  # noqa: BLE001
        return None


def _number(value: Any) -> float | None:
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None
