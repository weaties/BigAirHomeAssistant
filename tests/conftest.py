"""Shared fixtures. The synthetic drive is the fuel-model ground truth."""

from __future__ import annotations

import pytest

from tests.support import Drive, load_drive


@pytest.fixture(scope="session")
def drive() -> Drive:
    return load_drive()
