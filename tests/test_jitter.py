"""The sending offset."""

from __future__ import annotations

from custom_components.haim_agent.jitter import offset_seconds


def test_the_same_instance_always_gets_the_same_slot() -> None:
    site = "1f9d4f8c-8a2b-4c1e-9a3b-2f6d5c7e1a90"

    # The whole point. The day HACS updates this component across three hundred
    # houses they all restart within minutes of one another - and with an offset
    # chosen at startup they would each pick a new one and still arrive
    # together.
    assert offset_seconds(site, 300) == offset_seconds(site, 300)


def test_different_instances_get_different_slots() -> None:
    offsets = {
        offset_seconds(f"1f9d4f8c-8a2b-4c1e-9a3b-2f6d5c7e1a{index:02d}", 300)
        for index in range(50)
    }

    # Not a uniformity proof, just evidence that the fleet is spread rather than
    # arriving in a heap.
    assert len(offsets) > 40


def test_the_offset_always_fits_inside_the_interval() -> None:
    for interval in (60, 300, 900):
        for index in range(100):
            offset = offset_seconds(f"site-{index}", interval)

            assert 0 <= offset < interval


def test_a_nonsense_interval_does_not_blow_up() -> None:
    assert offset_seconds("site", 0) == 0
