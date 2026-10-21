"""Tests for unit conversions (pure functions, no HTTP)."""

import pytest

from wattfrosch_fetch.conversions import (
    celsius_to_kelvin,
    kilometers_per_hour_to_meters_per_second,
)


def test_absolute_zero() -> None:
    assert celsius_to_kelvin(-273.15) == pytest.approx(0.0)


def test_freezing_point() -> None:
    assert celsius_to_kelvin(0.0) == pytest.approx(273.15)


def test_boiling_point() -> None:
    assert celsius_to_kelvin(100.0) == pytest.approx(373.15)


def test_negative_temperature() -> None:
    assert celsius_to_kelvin(-10.0) == pytest.approx(263.15)


def test_result_stays_inside_contract_plausibility_range() -> None:
    # Contract plausible range for weather.temperature_2m: (170.0, 345.0) K.
    for celsius in (-100.0, 0.0, 10.0, 40.0):
        value = celsius_to_kelvin(celsius)
        assert 170.0 <= value <= 345.0


def test_calm_wind() -> None:
    assert kilometers_per_hour_to_meters_per_second(0.0) == pytest.approx(0.0)


def test_beaufort_12() -> None:
    # ~118 km/h hurricane force -> ~32.8 m/s.
    assert kilometers_per_hour_to_meters_per_second(118.0) == pytest.approx(118.0 / 3.6)


def test_wind_result_stays_inside_contract_plausibility_range() -> None:
    # Contract plausible range for weather.wind_speed_10m: (0.0, 75.0) m/s.
    for kmh in (0.0, 50.0, 150.0, 250.0):
        value = kilometers_per_hour_to_meters_per_second(kmh)
        assert 0.0 <= value <= 75.0
