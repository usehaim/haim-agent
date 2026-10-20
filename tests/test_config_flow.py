"""How the pairing code typed in the config flow reaches the panel (D47)."""

from __future__ import annotations

import pytest

from custom_components.haim_agent.config_flow import normalise_pairing_code


@pytest.mark.parametrize(
    "typed",
    [
        "K3M9P2XA7H4Q",
        "K3M9 P2XA 7H4Q",
        "k3m9-p2xa-7h4q",
        "  K3M9 - P2XA - 7H4Q  ",
        "K3M9\tP2XA 7H4Q",
    ],
)
def test_the_code_reaches_the_panel_as_it_was_minted(typed: str) -> None:
    assert normalise_pairing_code(typed) == "K3M9P2XA7H4Q"
