"""Tests for the Open-Meteo client (respx only — no real API calls)."""

import json
from datetime import UTC, datetime

import pytest
import respx

from wattfrosch_fetch.clients import open_meteo as om
from wattfrosch_fetch.clients.open_meteo import OpenMeteoClient
from wattfrosch_fetch.contracts import METRICS
from wattfrosch_fetch.http import FetchConfigError, FetchSettings, FetchValidationError

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


class FakeClock:
    """Injectable clock/sleep, mirroring test_http.FakeClock."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# Fixed now: boundary (today - archive_lag_days=7) = 2026-10-13 00:00 UTC;
# archive overlap end = 2026-10-14 00:00 UTC; horizon end = 2026-11-05 12:00.
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

TEMPERATURE = "weather.temperature_2m"
WIND = "weather.wind_speed_10m"


def _client(**settings_kwargs: object) -> OpenMeteoClient:
    settings = FetchSettings(http_backoff_base_seconds=0.0, **settings_kwargs)  # type: ignore[arg-type]
    return OpenMeteoClient(settings=settings, now_fn=lambda: NOW)


def _hourly(
    times: list[str], values: list[object], variable: str = "temperature_2m"
) -> dict:
    return {"hourly": {"time": times, variable: values}}


def _hours(day: str, count: int = 24) -> list[str]:
    return [f"{day}T{hour:02d}:00" for hour in range(count)]


def _dt(day: str, hour: int = 0) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:00").replace(tzinfo=UTC)


@respx.mock
def test_archive_only_window_makes_single_archive_call() -> None:
    archive = respx.get(ARCHIVE_URL).respond(
        json=_hourly(_hours("2026-10-01"), [10.0] * 24)
    )
    forecast = respx.get(FORECAST_URL).respond(json={})
    client = _client()

    records = list(
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-10-01"), _dt("2026-10-02")
        )
    )

    assert forecast.call_count == 0
    assert archive.call_count == 1
    params = archive.calls.last.request.url.params
    assert params["latitude"] == "52.52"
    assert params["longitude"] == "13.405"
    assert params["hourly"] == "temperature_2m"
    assert params["timezone"] == "UTC"
    assert params["models"] == "era5"
    assert params["start_date"] == "2026-10-01"
    assert params["end_date"] == "2026-10-01"
    assert len(records) == 24
    assert records[0].ts_utc == _dt("2026-10-01")
    assert records[0].value == pytest.approx(283.15)  # 10 °C -> K
    assert records[0].source == "open-meteo"
    assert records[0].resolution == "PT1H"


@respx.mock
def test_forecast_only_window_makes_single_forecast_call() -> None:
    archive = respx.get(ARCHIVE_URL).respond(json={})
    forecast = respx.get(FORECAST_URL).respond(
        json=_hourly(_hours("2026-10-15"), [5.0] * 24, variable="wind_speed_10m")
    )
    client = _client()

    records = list(
        client.fetch_series(WIND, "DE-KOELN", _dt("2026-10-15"), _dt("2026-10-16"))
    )

    assert archive.call_count == 0
    assert forecast.call_count == 1
    params = forecast.calls.last.request.url.params
    assert params["latitude"] == "50.937"
    assert params["longitude"] == "6.96"
    assert params["hourly"] == "wind_speed_10m"
    assert params["start_date"] == "2026-10-15"
    assert params["end_date"] == "2026-10-15"
    assert len(records) == 24
    assert records[0].value == pytest.approx(5.0 / 3.6)  # km/h -> m/s


@respx.mock
def test_spanning_window_calls_archive_and_forecast() -> None:
    archive = respx.get(ARCHIVE_URL).respond(json=_hourly([], []))
    forecast = respx.get(FORECAST_URL).respond(json=_hourly([], []))
    client = _client()

    list(
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-10-10"), _dt("2026-10-20")
        )
    )

    assert archive.call_count == 1
    assert forecast.call_count == 1
    # archive window: [start, boundary + overlap) = [10-10, 10-14)
    archive_params = archive.calls.last.request.url.params
    assert archive_params["start_date"] == "2026-10-10"
    assert archive_params["end_date"] == "2026-10-13"
    # forecast window: [boundary, end) = [10-13, 10-20)
    forecast_params = forecast.calls.last.request.url.params
    assert forecast_params["start_date"] == "2026-10-13"
    assert forecast_params["end_date"] == "2026-10-19"


@respx.mock
def test_overlap_archive_wins_on_duplicate_timestamps() -> None:
    # Request [10-12, 10-15): archive covers [10-12, 10-14), forecast
    # [10-13, 10-15). Overlap = all of 2026-10-13.
    respx.get(ARCHIVE_URL).respond(
        json=_hourly(
            _hours("2026-10-12") + _hours("2026-10-13"),
            [1.0] * 48,
        )
    )
    respx.get(FORECAST_URL).respond(
        json=_hourly(
            _hours("2026-10-13") + _hours("2026-10-14"),
            [99.0] * 48,
        )
    )
    client = _client()

    records = list(
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-10-12"), _dt("2026-10-15")
        )
    )

    assert len(records) == 72  # no duplicates
    by_ts = {record.ts_utc: record for record in records}
    assert by_ts[_dt("2026-10-12", 12)].value == pytest.approx(274.15)  # archive
    assert by_ts[_dt("2026-10-13", 12)].value == pytest.approx(274.15)  # archive wins
    assert by_ts[_dt("2026-10-14", 12)].value == pytest.approx(372.15)  # forecast


@respx.mock
def test_forecast_window_capped_at_horizon() -> None:
    archive = respx.get(ARCHIVE_URL).respond(json={})
    forecast = respx.get(FORECAST_URL).respond(json=_hourly([], []))
    client = _client()

    list(
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-10-15"), _dt("2026-12-01")
        )
    )

    # now + forecast_horizon_days = 2026-11-05 12:00; last hour 11:00.
    params = forecast.calls.last.request.url.params
    assert params["end_date"] == "2026-11-05"
    assert archive.call_count == 0


@respx.mock
def test_window_entirely_beyond_horizon_returns_empty() -> None:
    archive = respx.get(ARCHIVE_URL).respond(json={})
    forecast = respx.get(FORECAST_URL).respond(json={})
    client = _client()

    records = list(
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-11-10"), _dt("2026-11-12")
        )
    )

    assert records == []
    assert archive.call_count == 0
    assert forecast.call_count == 0


@pytest.mark.parametrize(
    ("metric", "variable"),
    [
        (TEMPERATURE, "temperature_2m"),
        (WIND, "wind_speed_10m"),
        ("weather.precipitation", "precipitation"),
        ("weather.cloud_cover", "cloud_cover"),
    ],
)
@respx.mock
def test_metric_resolved_via_source_metric_map(metric: str, variable: str) -> None:
    respx.get(ARCHIVE_URL).respond(json=_hourly([], [], variable=variable))
    client = _client()

    list(client.fetch_series(metric, "DE-BERLIN", _dt("2026-10-01"), _dt("2026-10-02")))

    request = respx.get(ARCHIVE_URL).calls.last.request  # type: ignore[union-attr]
    assert request.url.params["hourly"] == variable


def test_uncovered_metric_raises_config_error() -> None:
    client = _client()

    with pytest.raises(FetchConfigError, match="not covered"):
        client.fetch_series(
            "price.day_ahead", "DE-BERLIN", _dt("2026-10-01"), _dt("2026-10-02")
        )


def test_uncovered_region_raises_config_error() -> None:
    client = _client()

    with pytest.raises(FetchConfigError, match="not covered"):
        client.fetch_series(TEMPERATURE, "DE-LU", _dt("2026-10-01"), _dt("2026-10-02"))


def test_region_at_alone_raises_config_error() -> None:
    client = _client()

    with pytest.raises(FetchConfigError, match="not covered"):
        client.fetch_series(TEMPERATURE, "AT", _dt("2026-10-01"), _dt("2026-10-02"))


def test_derived_metric_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    derived = METRICS[TEMPERATURE].model_copy(update={"derived": True})
    monkeypatch.setattr(om, "METRICS", {**METRICS, TEMPERATURE: derived})
    client = _client()

    with pytest.raises(FetchConfigError, match="derived"):
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-10-01"), _dt("2026-10-02")
        )


def test_naive_start_raises_config_error() -> None:
    client = _client()

    with pytest.raises(FetchConfigError, match="timezone-aware"):
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", datetime(2026, 10, 1), _dt("2026-10-02")
        )


def test_start_not_before_end_raises_config_error() -> None:
    client = _client()

    with pytest.raises(FetchConfigError, match="before end"):
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-10-02"), _dt("2026-10-01")
        )


@respx.mock
def test_half_open_end_and_start_filters_applied() -> None:
    # API returns the full day; request [10-10 12:00, 10-11 00:00) keeps
    # exactly the hours 12:00..23:00 (half-open end excluded).
    respx.get(ARCHIVE_URL).respond(json=_hourly(_hours("2026-10-10"), [1.0] * 24))
    client = _client()

    records = list(
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-10-10", 12), _dt("2026-10-11")
        )
    )

    assert len(records) == 12
    assert records[0].ts_utc == _dt("2026-10-10", 12)
    assert records[-1].ts_utc == _dt("2026-10-10", 23)


@respx.mock
def test_temperature_converted_celsius_to_kelvin() -> None:
    respx.get(ARCHIVE_URL).respond(json=_hourly(_hours("2026-10-01"), [20.0] * 24))
    client = _client()

    records = list(
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-10-01"), _dt("2026-10-02")
        )
    )

    assert records[0].value == pytest.approx(293.15)


@respx.mock
def test_null_values_become_missing_rows() -> None:
    times = _hours("2026-10-01")
    values: list[object] = [10.0] * 24
    values[3] = None
    respx.get(ARCHIVE_URL).respond(json=_hourly(times, values))
    client = _client()

    records = list(
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-10-01"), _dt("2026-10-02")
        )
    )

    assert len(records) == 23
    assert _dt("2026-10-01", 3) not in {record.ts_utc for record in records}


@respx.mock
def test_non_finite_value_raises_validation_error() -> None:
    payload = _hourly(_hours("2026-10-01"), [float("nan")] + [10.0] * 23)
    respx.get(ARCHIVE_URL).respond(
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    client = _client()

    with pytest.raises(FetchValidationError, match="non-finite"):
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-10-01"), _dt("2026-10-02")
        )


@respx.mock
def test_non_numeric_value_raises_validation_error() -> None:
    respx.get(ARCHIVE_URL).respond(json=_hourly(_hours("2026-10-01"), ["10.0"] * 24))
    client = _client()

    with pytest.raises(FetchValidationError, match="non-numeric"):
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-10-01"), _dt("2026-10-02")
        )


@respx.mock
def test_api_error_body_raises_validation_error() -> None:
    respx.get(ARCHIVE_URL).respond(json={"error": True, "reason": "Invalid request"})
    client = _client()

    with pytest.raises(FetchValidationError, match="Invalid request"):
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-10-01"), _dt("2026-10-02")
        )


@respx.mock
def test_missing_variable_array_raises_validation_error() -> None:
    respx.get(ARCHIVE_URL).respond(json={"hourly": {"time": _hours("2026-10-01")}})
    client = _client()

    with pytest.raises(FetchValidationError, match="temperature_2m"):
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-10-01"), _dt("2026-10-02")
        )


@respx.mock
def test_length_mismatch_raises_validation_error() -> None:
    respx.get(ARCHIVE_URL).respond(json=_hourly(_hours("2026-10-01"), [10.0] * 23))
    client = _client()

    with pytest.raises(FetchValidationError, match="values"):
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-10-01"), _dt("2026-10-02")
        )


@respx.mock
@pytest.mark.parametrize(
    "payload",
    [[], "text"],
    ids=["array", "string"],
)
def test_non_object_response_raises_validation_error(payload: object) -> None:
    respx.get(ARCHIVE_URL).respond(json=payload)
    client = _client()

    with pytest.raises(FetchValidationError, match="not a JSON object"):
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-10-01"), _dt("2026-10-02")
        )


@respx.mock
def test_missing_hourly_object_raises_validation_error() -> None:
    respx.get(ARCHIVE_URL).respond(json={"latitude": 52.52})
    client = _client()

    with pytest.raises(FetchValidationError, match="missing 'hourly'"):
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-10-01"), _dt("2026-10-02")
        )


@respx.mock
def test_missing_time_array_raises_validation_error() -> None:
    respx.get(ARCHIVE_URL).respond(json={"hourly": {"temperature_2m": [10.0] * 24}})
    client = _client()

    with pytest.raises(FetchValidationError, match="missing 'hourly.time'"):
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-10-01"), _dt("2026-10-02")
        )


def test_unknown_metric_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(om, "METRICS", {})
    client = _client()

    with pytest.raises(FetchConfigError, match="unknown metric"):
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-10-01"), _dt("2026-10-02")
        )


def test_region_without_weather_point_entry_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Region is mapped for the source but has no REGIONS entry -> not a
    # usable weather point.
    monkeypatch.setattr(om, "REGIONS", {})
    client = _client()

    with pytest.raises(FetchConfigError, match="weather point"):
        client.fetch_series(
            TEMPERATURE, "DE-BERLIN", _dt("2026-10-01"), _dt("2026-10-02")
        )


def test_clock_and_sleep_kwargs_pass_through() -> None:
    clock = FakeClock()

    client = OpenMeteoClient(
        settings=FetchSettings(http_backoff_base_seconds=0.0),
        clock=clock,
        sleep=clock.sleep,
        now_fn=lambda: NOW,
    )

    assert client is not None
