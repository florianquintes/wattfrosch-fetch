"""Fetch external weather/energy data and normalize it to MeasurementRecord."""

from wattfrosch_fetch.contracts import (
    CONTRACT_VERSION,
    METRICS,
    REGIONS,
    SOURCE_METRIC_MAP,
    SOURCE_REGION_MAP,
    SOURCES,
    MeasurementRecord,
)

__all__ = [
    "CONTRACT_VERSION",
    "METRICS",
    "REGIONS",
    "SOURCES",
    "SOURCE_METRIC_MAP",
    "SOURCE_REGION_MAP",
    "MeasurementRecord",
]
