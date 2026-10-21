"""Shared test fixtures: the suite must never touch the real network."""

from collections.abc import Generator

import pytest


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Fail loudly if any test attempts a real socket connection.

    respx intercepts httpx transports, but this guard also catches any
    code path that would bypass it — the issue requires zero real API
    calls, structurally.
    """

    def _no_socket(*args: object, **kwargs: object) -> object:
        raise AssertionError("real network access attempted in tests")

    monkeypatch.setattr("socket.socket", _no_socket)
    yield
