"""Shared fixtures.

Most of this component is deliberately testable without Home Assistant running:
the hash, the redaction filter, the offline queue and the command dispatch are
plain Python that happens to live in a custom component. Making them depend on a
full Home Assistant test harness would mean they got tested less, and those four
are exactly the parts that must not be wrong.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The component is imported as a package so relative imports inside it work.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeStore:
    """Home Assistant's Store, without a Home Assistant and without a disk.

    Keyed like the real one, so a test can check that two instances of the same
    component do not write over each other - which is the whole reason the real
    keys carry the site uuid.
    """

    files: dict[str, dict] = {}

    def __init__(self, hass, version, key) -> None:
        self._key = key

    async def async_load(self) -> dict | None:
        return FakeStore.files.get(self._key)

    async def async_save(self, data: dict) -> None:
        FakeStore.files[self._key] = data

    async def async_remove(self) -> None:
        FakeStore.files.pop(self._key, None)


@pytest.fixture(scope="session")
def contract_dir() -> Path:
    """The contract, which lives beside this component in the monorepo only.

    The published repository is `agent/` alone: no contract, and no fixture
    taken from a real house. There the tests that need it are skipped. In the
    monorepo - recognised by the panel sitting next to it - a missing contract
    is a failure, never a skip: that is where the check has to bite.
    """
    contract = REPO_ROOT / "contract"

    if contract.is_dir():
        return contract

    if (REPO_ROOT / "panel").is_dir():
        pytest.fail("contract/ is missing from the monorepo")

    pytest.skip("contract/ is not part of the published repository")


@pytest.fixture(scope="session")
def state_schema(contract_dir: Path) -> dict:
    return json.loads((contract_dir / "schemas" / "state.schema.json").read_text())


@pytest.fixture(scope="session")
def real_fixture(contract_dir: Path) -> dict:
    payload = json.loads((contract_dir / "fixtures" / "reference.state.json").read_text())

    # A note to humans, not part of the contract payload.
    payload.pop("_fixture_meta", None)

    return payload
