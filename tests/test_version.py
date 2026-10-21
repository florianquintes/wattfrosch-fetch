"""Tests for the contract version check (wattfrosch_fetch.version)."""

import importlib

import pytest
from wattfrosch_contracts import CONTRACT_VERSION

from wattfrosch_fetch import version
from wattfrosch_fetch.version import (
    CONTRACT_VERSION_SUPPORTED,
    ContractVersionError,
    assert_contract_version,
)


def test_contract_version_matches_supported() -> None:
    assert CONTRACT_VERSION.startswith(CONTRACT_VERSION_SUPPORTED + ".")
    assert_contract_version(CONTRACT_VERSION)


@pytest.mark.parametrize(
    ("ver", "ok"),
    [
        ("0.1.0", True),
        ("0.1.7", True),
        ("0.2.0", False),
        ("1.0.0", False),
        ("0.1", False),
        ("0.1.0rc1", False),
        ("garbage", False),
        ("", False),
    ],
)
def test_assert_contract_version(ver: str, ok: bool) -> None:
    if ok:
        assert_contract_version(ver)
    else:
        with pytest.raises(ContractVersionError):
            assert_contract_version(ver)


def test_import_raises_on_version_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wattfrosch_contracts

    real = wattfrosch_contracts.CONTRACT_VERSION
    monkeypatch.setattr(wattfrosch_contracts, "CONTRACT_VERSION", "9.9.9")
    # reload re-executes the module, so its ContractVersionError is a new
    # class object -> catch broadly and check against the reloaded class.
    with pytest.raises(Exception) as excinfo:
        importlib.reload(version)
    assert isinstance(excinfo.value, version.ContractVersionError)
    monkeypatch.setattr(wattfrosch_contracts, "CONTRACT_VERSION", real)
    importlib.reload(version)
    assert_contract_version(version.CONTRACT_VERSION)
