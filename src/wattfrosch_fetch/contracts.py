"""Wiring to the wattfrosch-contract: single import point for all contract symbols.

Fetch is strict, contracts is lenient. Everything fetch needs from the
contract is imported here once and re-exported; client modules must not
import from ``wattfrosch_contracts`` directly.

The contract version is checked at import time (see
:mod:`wattfrosch_fetch.version`).
"""

from __future__ import annotations

from wattfrosch_contracts import (
    CONTRACT_VERSION,
    METRICS,
    REGIONS,
    SOURCE_METRIC_MAP,
    SOURCE_REGION_MAP,
    SOURCES,
    MeasurementRecord,
)

from wattfrosch_fetch.version import ContractVersionError, assert_contract_version

__all__ = [
    "CONTRACT_VERSION",
    "METRICS",
    "REGIONS",
    "SOURCES",
    "SOURCE_METRIC_MAP",
    "SOURCE_REGION_MAP",
    "ContractVersionError",
    "MeasurementRecord",
    "assert_contract_version",
]
