"""Shared fixtures. The synthetic drive is the fuel-model ground truth."""

from __future__ import annotations

import pytest

from tests.support import Drive, load_drive


@pytest.fixture(scope="session")
def drive() -> Drive:
    return load_drive()


@pytest.fixture(autouse=True)
def _no_internet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests run without a network (CLAUDE.md): only loopback connections are allowed."""
    import socket

    real = socket.create_connection

    def guarded(address: tuple[str, int], *a: object, **kw: object) -> socket.socket:
        host = address[0]
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise OSError(f"test tried to reach {host}: mock the edge instead")
        return real(address, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "create_connection", guarded)
