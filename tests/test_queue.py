"""The offline queue.

The two behaviours that matter are both about not losing things and not
drowning the far end: the cap discards the OLDEST, and nothing leaves the queue
until the server has confirmed it.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.haim_agent.const import EVENT_QUEUE_MAX
from custom_components.haim_agent.queue import EventQueue


class FakeStore:
    """Stands in for Home Assistant's Store, and remembers like it does."""

    def __init__(self) -> None:
        self.saved: dict[str, Any] | None = None

    async def async_load(self) -> dict[str, Any] | None:
        return self.saved

    async def async_save(self, data: dict[str, Any]) -> None:
        self.saved = data


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> EventQueue:
    import custom_components.haim_agent.queue as queue_module

    store = FakeStore()
    monkeypatch.setattr(queue_module, "Store", lambda *args, **kwargs: store)

    return EventQueue(hass=None, site_uuid="site-1")


async def test_it_stamps_every_event_with_an_id_the_server_can_dedupe_on(queue: EventQueue) -> None:
    event = await queue.async_add("backup_failed", "critical", {"reason": "network"})

    assert event["event_uid"]
    assert event["type"] == "backup_failed"
    assert event["severity"] == "critical"
    assert event["occurred_at"].endswith("Z")


async def test_an_unknown_severity_becomes_info(queue: EventQueue) -> None:
    event = await queue.async_add("something_new", "apocalyptic")

    # A newer version of ourselves inventing a severity must not have its events
    # rejected by an older server.
    assert event["severity"] == "info"


async def test_it_discards_the_oldest_when_it_is_full(queue: EventQueue) -> None:
    for index in range(EVENT_QUEUE_MAX + 25):
        await queue.async_add("tick", "info", {"index": index})

    assert await queue.async_size() == EVENT_QUEUE_MAX

    remaining = await queue.async_peek(EVENT_QUEUE_MAX)

    # What happened five minutes ago is more use than what happened three days
    # ago, and an instance back from three days offline must not be able to push
    # tens of thousands of rows at once.
    assert remaining[0]["payload"]["index"] == 25
    assert remaining[-1]["payload"]["index"] == EVENT_QUEUE_MAX + 24


async def test_nothing_leaves_the_queue_until_the_server_confirms(queue: EventQueue) -> None:
    first = await queue.async_add("a", "info")
    await queue.async_add("b", "info")

    peeked = await queue.async_peek(10)

    assert len(peeked) == 2
    # A queue that empties on send loses everything to one timeout.
    assert await queue.async_size() == 2

    await queue.async_acknowledge([first["event_uid"]])

    assert await queue.async_size() == 1


async def test_a_duplicate_counts_as_delivered(queue: EventQueue) -> None:
    event = await queue.async_add("a", "info")

    # "Duplicate" means an earlier attempt got through and only the answer was
    # lost - the commonest way this goes wrong on a bad connection. Keeping it
    # queued would mean sending it forever.
    await queue.async_acknowledge([event["event_uid"]])

    assert await queue.async_size() == 0


async def test_it_survives_a_restart(queue: EventQueue, monkeypatch: pytest.MonkeyPatch) -> None:
    await queue.async_add("backup_failed", "critical")

    import custom_components.haim_agent.queue as queue_module

    # Same store, new queue object: exactly what a Home Assistant restart looks
    # like - and a restart is both something worth reporting and something that
    # would lose the report.
    revived = EventQueue(hass=None, site_uuid="site-1")

    assert await revived.async_size() == 1


async def test_a_corrupted_store_does_not_stop_the_component(monkeypatch: pytest.MonkeyPatch) -> None:
    import custom_components.haim_agent.queue as queue_module

    class BrokenStore(FakeStore):
        async def async_load(self):
            return {"events": "this is not a list"}

    monkeypatch.setattr(queue_module, "Store", lambda *args, **kwargs: BrokenStore())

    queue = EventQueue(hass=None, site_uuid="site-1")

    # A store written by a different version, or half-written by a power cut,
    # must cost the queue and not the integration.
    assert await queue.async_size() == 0

    await queue.async_add("a", "info")

    assert await queue.async_size() == 1


# ------------------------------------------ payloads under 4 KB (prompt 05, 4)

PANEL_LIMIT = 4096


def php_json_size(value: Any) -> int:
    """What the panel's strlen(json_encode(...)) says, written independently:
    \\uXXXX for anything outside ASCII (two of them past the BMP), \\/ for a
    slash, no spaces."""
    import json

    text = json.dumps(value, ensure_ascii=True, separators=(",", ":"))

    return len(text.replace("/", "\\/"))


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "x" * 10_000},
        {"path": "/" * 3_000},
        {"message": "è" * 2_000},
        {"emoji": "\U0001f600" * 1_000},
        {f"key_{index}": index for index in range(2_000)},
        {"items": [{"name": "n" * 40, "value": index} for index in range(500)]},
        {"nested": {"deeper": {"deepest": ["y" * 300] * 50}}},
        {"k" * 5_000: "short"},
    ],
    ids=["long-string", "slashes", "accents", "astral", "many-keys", "long-list", "nested", "long-key"],
)
async def test_a_payload_the_panel_would_blank_is_cut_here_first(queue: EventQueue, payload) -> None:
    """Over 4 KB the panel keeps the event and replaces the whole payload with
    {"truncated": true}. Cut here, the part that fits survives."""
    event = await queue.async_add("backup_failed", "warning", payload)

    assert php_json_size(event["payload"]) <= PANEL_LIMIT
    assert event["payload"]["truncated"] is True
    assert len(event["payload"]) > 1, "something of the original must survive"


async def test_the_part_that_survives_is_the_beginning(queue: EventQueue) -> None:
    event = await queue.async_add("backup_failed", "warning", {"reason": "disk_full", "detail": "z" * 9_000})

    assert event["payload"]["reason"] == "disk_full"
    assert event["payload"]["detail"].startswith("zzz")


async def test_a_payload_that_fits_is_left_exactly_as_it_was(queue: EventQueue) -> None:
    payload = {"reason": "network", "path": "/config/backups", "count": 3, "ok": False, "none": None}

    event = await queue.async_add("backup_failed", "warning", payload)

    assert event["payload"] == payload
    assert "truncated" not in event["payload"]


async def test_a_payload_queued_by_an_older_version_is_cut_on_load(monkeypatch) -> None:
    import custom_components.haim_agent.queue as queue_module

    store = FakeStore()
    store.saved = {
        "events": [
            {
                "event_uid": "4f1c0b9e-0000-4000-8000-000000000001",
                "type": "backup_failed",
                "severity": "warning",
                "payload": {"error": "x" * 10_000},
                "occurred_at": "2026-10-14T10:00:00Z",
            }
        ]
    }
    monkeypatch.setattr(queue_module, "Store", lambda *args, **kwargs: store)
    queue = EventQueue(hass=None, site_uuid="site-1")

    [event] = await queue.async_peek(10)

    assert php_json_size(event["payload"]) <= PANEL_LIMIT
    assert event["payload"]["truncated"] is True


# --------------------------------------- the server's own event types (point 5)

SERVER_TYPES = [
    "went_offline",
    "came_online",
    "went_frozen",
    "degraded",
    "site_unstable",
    "monitoring_disabled",
    "never_seen",
    "alert_fired_backup_failed",
    "alert_cleared_offline",
]


@pytest.mark.parametrize("event_type", SERVER_TYPES)
async def test_the_server_s_event_types_are_not_the_agent_s_to_send(queue: EventQueue, event_type: str) -> None:
    """The panel now records who wrote an event (`source`). An agent posting
    `went_offline` reads as the agent claiming to be the server."""
    with pytest.raises(ValueError):
        await queue.async_add(event_type, "critical")

    assert await queue.async_size() == 0


@pytest.mark.parametrize("event_type", ["backup_failed", "integration_error", "update_available", "alert"])
async def test_the_agent_s_own_types_are_queued(queue: EventQueue, event_type: str) -> None:
    await queue.async_add(event_type, "warning")

    assert await queue.async_size() == 1


async def test_a_server_type_left_on_disk_by_an_older_version_is_not_sent(monkeypatch) -> None:
    import custom_components.haim_agent.queue as queue_module

    store = FakeStore()
    store.saved = {
        "events": [
            {"event_uid": "4f1c0b9e-0000-4000-8000-000000000001", "type": "went_offline", "severity": "critical",
             "payload": {}, "occurred_at": "2026-10-14T10:00:00Z"},
            {"event_uid": "4f1c0b9e-0000-4000-8000-000000000002", "type": "backup_failed", "severity": "warning",
             "payload": {}, "occurred_at": "2026-10-14T10:00:01Z"},
        ]
    }
    monkeypatch.setattr(queue_module, "Store", lambda *args, **kwargs: store)

    events = await EventQueue(hass=None, site_uuid="site-1").async_peek(10)

    assert [event["type"] for event in events] == ["backup_failed"]


def test_no_code_in_the_component_queues_a_server_type() -> None:
    """Every literal handed to the queue, read from the source."""
    import ast
    from pathlib import Path

    from custom_components.haim_agent.queue import is_server_event_type

    here = Path(__file__).resolve().parents[1] / "custom_components" / "haim_agent"

    for path in here.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("async_queue_event", "async_add"):
                if node.args and isinstance(node.args[0], ast.Constant):
                    assert not is_server_event_type(node.args[0].value), f"{path.name}: {node.args[0].value}"
