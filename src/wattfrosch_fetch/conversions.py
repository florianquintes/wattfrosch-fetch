"""Unit conversions for source data that does not ship in contract units.

Contract rule ("Responsibility boundaries"): export happens in metric units
only (e.g. kelvin for temperature); the conversion happens in fetch, before
the ``MeasurementRecord`` is built. External functions here keep the clients
free of conversion logic and make future unit changes in the APIs a
one-file fix.
"""

from __future__ import annotations

_CELSIUS_KELVIN_OFFSET: float = 273.15


_KMH_MS_FACTOR: float = 3.6


def celsius_to_kelvin(value: float) -> float:
    """Convert a temperature from degrees Celsius to kelvin."""
    return value + _CELSIUS_KELVIN_OFFSET


def kilometers_per_hour_to_meters_per_second(value: float) -> float:
    """Convert a wind speed from km/h to m/s."""
    return value / _KMH_MS_FACTOR


__all__ = [
    "celsius_to_kelvin",
    "kilometers_per_hour_to_meters_per_second",
]
