"""Tests for the contracts wiring module (Issue #1)."""

from wattfrosch_fetch import contracts
from wattfrosch_fetch.contracts import (
    CONTRACT_VERSION,
    METRICS,
    REGIONS,
    SOURCE_METRIC_MAP,
    SOURCE_REGION_MAP,
    SOURCES,
    MeasurementRecord,
)

EXPECTED_SOURCES = ("open-meteo", "energy-charts", "smard")


def test_contract_symbols_reexported() -> None:
    assert MeasurementRecord is not None
    assert set(METRICS) > set()
    assert set(REGIONS) > set()
    assert set(SOURCES) == set(EXPECTED_SOURCES)
    assert set(SOURCE_METRIC_MAP) == set(EXPECTED_SOURCES)
    assert set(SOURCE_REGION_MAP) == set(EXPECTED_SOURCES)


def test_contract_version_reexported() -> None:
    from wattfrosch_fetch import version

    assert isinstance(CONTRACT_VERSION, str)
    # contracts re-exports the version symbols
    assert contracts.assert_contract_version is version.assert_contract_version
    assert contracts.ContractVersionError is version.ContractVersionError


def test_reexport_is_same_object_as_contracts() -> None:
    import wattfrosch_contracts

    assert contracts.MeasurementRecord is wattfrosch_contracts.MeasurementRecord
    assert contracts.METRICS is wattfrosch_contracts.METRICS
    assert contracts.SOURCE_METRIC_MAP is wattfrosch_contracts.SOURCE_METRIC_MAP
    assert contracts.CONTRACT_VERSION is wattfrosch_contracts.CONTRACT_VERSION
