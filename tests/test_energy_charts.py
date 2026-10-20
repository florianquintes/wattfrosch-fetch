"""Tests for the Energy-Charts client (respx only — no real API calls)."""

import json
from datetime import UTC, datetime

import pytest
import respx

from wattfrosch_fetch.clients import energy_charts
from wattfrosch_fetch.clients.energy_charts import EnergyChartsClient
from wattfrosch_fetch.contracts import METRICS
from wattfrosch_fetch.http import FetchConfigError, FetchSettings, FetchValidationError

POWER_URL = "https://api.energy-charts.info/public_power"
PRICE_URL = "https://api.energy-charts.info/price"


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


SOLAR = "generation.solar"
HYDRO = "generation.hydro"
LOAD = "consumption.load"
OTHER = "generation.other"
PRICE = "price.day_ahead"

DE_LU = "DE-LU"
DE_AT_LU = "DE-AT-LU"

# Bidding-zone change: DE-AT-LU ends 2018-10-01, DE-LU starts 2018-10-01.
BOUNDARY_DAY = "2018-10-01"


def _client(**settings_kwargs: object) -> EnergyChartsClient:
    settings = FetchSettings(http_backoff_base_seconds=0.0, **settings_kwargs)  # type: ignore[arg-type]
    return EnergyChartsClient(settings=settings)


def _dt(day: str, hour: int = 0, minute: int = 0) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}").replace(tzinfo=UTC)


def _unix_hours(day: str, count: int = 24) -> list[int]:
    base = int(_dt(day).timestamp())
    return [base + hour * 3600 for hour in range(count)]


def _unix_quarters(day: str, count: int = 96) -> list[int]:
    base = int(_dt(day).timestamp())
    return [base + quarter * 900 for quarter in range(count)]


def _power(unix_seconds: list[int], series: dict[str, list[object]]) -> dict:
    return {
        "unix_seconds": unix_seconds,
        "production_types": [
            {"name": name, "data": data} for name, data in series.items()
        ],
    }


def _price_response(
    unix_seconds: list[int], prices: list[object], unit: str = "EUR / MWh"
) -> dict:
    return {"unix_seconds": unix_seconds, "price": prices, "unit": unit}


@respx.mock
def test_quarter_hourly_steps_averaged_to_hourly() -> None:
    # Two hours of 15-min solar data -> hourly means 2.5 and 6.5.
    route = respx.get(POWER_URL).respond(
        json=_power(
            _unix_quarters("2026-10-01"),
            {"Solar": [1.0, 2.0, 3.0, 4.0] + [5.0, 6.0, 7.0, 8.0] + [0.0] * 88},
        )
    )
    client = _client()

    records = list(
        client.fetch_series(SOLAR, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))
    )

    assert route.call_count == 1
    params = route.calls.last.request.url.params
    assert params["zone"] == "de-lu"  # lowercase id from SOURCE_REGION_MAP
    assert params["start"] == str(int(_dt("2026-10-01").timestamp()))
    assert len(records) == 24
    assert records[0].ts_utc == _dt("2026-10-01")
    assert records[0].value == pytest.approx(2.5)
    assert records[1].value == pytest.approx(6.5)
    assert records[0].source == "energy-charts"
    assert records[0].resolution == "PT1H"


@respx.mock
def test_quarter_hourly_mean_is_mean_not_sum() -> None:
    # Load quarters 100, 200, 300, 400 -> hourly mean 250 (MW average).
    respx.get(POWER_URL).respond(
        json=_power(
            _unix_quarters("2026-10-01"),
            {"Load": [100.0, 200.0, 300.0, 400.0] + [0.0] * 92},
        )
    )
    client = _client()

    records = list(
        client.fetch_series(LOAD, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))
    )

    assert records[0].value == pytest.approx(250.0)


@respx.mock
def test_hourly_price_steps_pass_through() -> None:
    # Historical /price windows ship hourly steps; values stay unchanged.
    respx.get(PRICE_URL).respond(
        json=_price_response(_unix_hours("2017-10-01"), [19.05] * 24)
    )
    client = _client()

    records = list(
        client.fetch_series(PRICE, DE_AT_LU, _dt("2017-10-01"), _dt("2017-10-02"))
    )

    assert len(records) == 24
    assert records[0].ts_utc == _dt("2017-10-01")
    assert records[0].value == pytest.approx(19.05)
    assert records[23].ts_utc == _dt("2017-10-01", 23)


@respx.mock
def test_price_quarter_hourly_steps_averaged_to_hourly() -> None:
    # Current /price windows ship 15-min steps -> hourly mean.
    respx.get(PRICE_URL).respond(
        json=_price_response(
            _unix_quarters("2026-10-01"), [10.0, 20.0, 30.0, 40.0] * 24
        )
    )
    client = _client()

    records = list(
        client.fetch_series(PRICE, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))
    )

    assert len(records) == 24
    assert records[0].value == pytest.approx(25.0)


@respx.mock
def test_price_uses_uppercase_bzn_param() -> None:
    route = respx.get(PRICE_URL).respond(
        json=_price_response(_unix_hours("2026-10-01"), [50.0] * 24)
    )
    client = _client()

    list(client.fetch_series(PRICE, DE_LU, _dt("2026-10-01"), _dt("2026-10-02")))

    params = route.calls.last.request.url.params
    assert params["bzn"] == "DE-LU"  # API rejects lowercase bzn with 400


@respx.mock
def test_hydro_sums_run_of_river_and_reservoir() -> None:
    # Both hydro series map to generation.hydro: quarters are summed per
    # step (across series), then averaged over the hour.
    respx.get(POWER_URL).respond(
        json=_power(
            _unix_quarters("2026-10-01"),
            {
                "Hydro Run-of-River": [1.0, 2.0, 3.0, 4.0] + [0.0] * 92,
                "Hydro water reservoir": [10.0, 20.0, 30.0, 40.0] + [0.0] * 92,
            },
        )
    )
    client = _client()

    records = list(
        client.fetch_series(HYDRO, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))
    )

    assert len(records) == 24
    # per-step sums (1+10, 2+20, 3+30, 4+40) averaged over the hour
    assert records[0].value == pytest.approx((11 + 22 + 33 + 44) / 4)


@respx.mock
def test_hydro_missing_component_series_raises_validation_error() -> None:
    respx.get(POWER_URL).respond(
        json=_power(
            _unix_quarters("2026-10-01"),
            {"Hydro Run-of-River": [1.0] * 96},
        )
    )
    client = _client()

    with pytest.raises(FetchValidationError, match="Hydro water reservoir"):
        client.fetch_series(HYDRO, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))


@respx.mock
def test_null_quarter_skips_whole_hour() -> None:
    # A missing quarter means the hourly mean would be partial -> the hour
    # becomes a missing row (no invented averages).
    values: list[object] = [1.0, 2.0, 3.0, 4.0] * 2 + [5.0] * 88
    values[1] = None  # quarter 2 of hour 0
    respx.get(POWER_URL).respond(
        json=_power(_unix_quarters("2026-10-01"), {"Solar": values})
    )
    client = _client()

    records = list(
        client.fetch_series(SOLAR, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))
    )

    assert len(records) == 23
    assert _dt("2026-10-01", 0) not in {record.ts_utc for record in records}
    assert records[0].ts_utc == _dt("2026-10-01", 1)


@respx.mock
def test_null_in_one_hydro_series_skips_hour() -> None:
    respx.get(POWER_URL).respond(
        json=_power(
            _unix_quarters("2026-10-01"),
            {
                "Hydro Run-of-River": [1.0, None, 3.0, 4.0] + [1.0] * 92,
                "Hydro water reservoir": [10.0] * 96,
            },
        )
    )
    client = _client()

    records = list(
        client.fetch_series(HYDRO, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))
    )

    assert len(records) == 23
    assert _dt("2026-10-01", 0) not in {record.ts_utc for record in records}


@respx.mock
def test_gap_in_quarter_steps_skips_hour() -> None:
    # The API itself skips a timestamp (data gap): hour 0 only has 3 of 4
    # quarters -> skipped even though no value is null.
    unix = _unix_quarters("2026-10-01")
    del unix[1]
    respx.get(POWER_URL).respond(
        json=_power(unix, {"Solar": [1.0, 2.0, 3.0] + [4.0] * 92})
    )
    client = _client()

    records = list(
        client.fetch_series(SOLAR, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))
    )

    assert _dt("2026-10-01", 0) not in {record.ts_utc for record in records}
    assert len(records) == 23


@respx.mock
def test_partial_first_hour_skipped() -> None:
    # Window starts mid-hour: hour 0 has only 2 quarters -> skipped.
    respx.get(POWER_URL).respond(
        json=_power(
            _unix_quarters("2026-10-01"),
            {"Solar": [1.0] * 96},
        )
    )
    client = _client()

    records = list(
        client.fetch_series(SOLAR, DE_LU, _dt("2026-10-01", 0, 30), _dt("2026-10-02"))
    )

    assert len(records) == 23
    assert records[0].ts_utc == _dt("2026-10-01", 1)


@respx.mock
def test_api_inclusive_end_timestamp_dropped() -> None:
    # The API includes the end timestamp; half-open [start, end) drops it.
    unix = _unix_hours("2026-10-01") + [int(_dt("2026-10-02").timestamp())]
    respx.get(PRICE_URL).respond(json=_price_response(unix, [10.0] * 24 + [99.0]))
    client = _client()

    records = list(
        client.fetch_series(PRICE, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))
    )

    assert len(records) == 24
    assert records[-1].ts_utc == _dt("2026-10-01", 23)


@respx.mock
def test_de_at_lu_records_clamped_before_2018_boundary() -> None:
    # The API serves current data under de-at-lu too; the client only emits
    # ts_utc where DE-AT-LU is valid (< 2018-10-01).
    respx.get(POWER_URL).respond(
        json=_power(
            _unix_hours("2018-09-30") + _unix_hours(BOUNDARY_DAY),
            {"Solar": [1.0] * 48},
        )
    )
    client = _client()

    records = list(
        client.fetch_series(SOLAR, DE_AT_LU, _dt("2018-09-30"), _dt(BOUNDARY_DAY, 12))
    )

    assert len(records) == 24
    assert records[0].ts_utc == _dt("2018-09-30")
    assert records[-1].ts_utc == _dt("2018-09-30", 23)
    params = respx.get(POWER_URL).calls.last.request.url.params  # type: ignore[union-attr]
    assert params["zone"] == "de-at-lu"


@respx.mock
def test_de_lu_records_clamped_from_2018_boundary() -> None:
    # DE-LU is only valid from 2018-10-01; earlier ts_utc are not emitted
    # even though the API serves them.
    respx.get(POWER_URL).respond(
        json=_power(
            _unix_hours("2018-09-30") + _unix_hours(BOUNDARY_DAY),
            {"Solar": [1.0] * 48},
        )
    )
    client = _client()

    records = list(
        client.fetch_series(SOLAR, DE_LU, _dt("2018-09-30"), _dt(BOUNDARY_DAY, 12))
    )

    assert len(records) == 12
    assert records[0].ts_utc == _dt(BOUNDARY_DAY)
    assert records[-1].ts_utc == _dt(BOUNDARY_DAY, 11)


@respx.mock
def test_window_fully_outside_validity_returns_empty_without_request() -> None:
    # DE-AT-LU ended 2018-10-01: a 2026 window is entirely invalid -> no
    # request at all, empty result (exit 0 semantics).
    route = respx.get(POWER_URL).respond(json=_power([], {}))
    client = _client()

    records = list(
        client.fetch_series(SOLAR, DE_AT_LU, _dt("2026-10-01"), _dt("2026-10-02"))
    )

    assert records == []
    assert route.call_count == 0


@respx.mock
def test_metric_without_api_series_returns_empty() -> None:
    # generation.other_renewable is covered by the source but has no API
    # series -> empty result, not an error.
    respx.get(POWER_URL).respond(
        json=_power(_unix_hours("2026-10-01"), {"Solar": [1.0] * 24})
    )
    client = _client()

    records = list(
        client.fetch_series(
            "generation.other_renewable", DE_LU, _dt("2026-10-01"), _dt("2026-10-02")
        )
    )

    assert records == []


@respx.mock
def test_others_and_waste_both_map_to_generation_other() -> None:
    respx.get(POWER_URL).respond(
        json=_power(
            _unix_hours("2026-10-01"),
            {"Others": [100.0] * 24, "Waste": [50.0] * 24},
        )
    )
    client = _client()

    records = list(
        client.fetch_series(OTHER, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))
    )

    assert len(records) == 24
    assert records[0].value == pytest.approx(150.0)


@respx.mock
def test_404_returns_empty_result() -> None:
    # 404 = "no data for that zone/range" -> empty result, not an error.
    respx.get(POWER_URL).respond(404)
    client = _client()

    records = list(
        client.fetch_series(SOLAR, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))
    )

    assert records == []


@respx.mock
def test_memo_reuses_response_for_second_metric() -> None:
    # One request per (region, window): the second fetch_series call for a
    # different metric on the same window reuses the in-run memo.
    route = respx.get(POWER_URL).respond(
        json=_power(
            _unix_hours("2026-10-01"),
            {"Solar": [10.0] * 24, "Load": [20.0] * 24},
        )
    )
    client = _client()

    solar = list(
        client.fetch_series(SOLAR, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))
    )
    load = list(client.fetch_series(LOAD, DE_LU, _dt("2026-10-01"), _dt("2026-10-02")))

    assert route.call_count == 1
    assert len(solar) == 24
    assert len(load) == 24
    assert solar[0].value == pytest.approx(10.0)
    assert load[0].value == pytest.approx(20.0)


@respx.mock
def test_memo_not_hit_for_different_window() -> None:
    route = respx.get(POWER_URL).respond(
        json=_power(_unix_hours("2026-10-01"), {"Solar": [10.0] * 24})
    )
    client = _client()

    list(client.fetch_series(SOLAR, DE_LU, _dt("2026-10-01"), _dt("2026-10-02")))
    list(client.fetch_series(SOLAR, DE_LU, _dt("2026-10-02"), _dt("2026-10-03")))

    assert route.call_count == 2


@respx.mock
def test_non_numeric_value_raises_validation_error() -> None:
    respx.get(POWER_URL).respond(
        json=_power(_unix_hours("2026-10-01"), {"Solar": ["10.0"] * 24})
    )
    client = _client()

    with pytest.raises(FetchValidationError, match="non-numeric"):
        client.fetch_series(SOLAR, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))


@respx.mock
def test_non_finite_value_raises_validation_error() -> None:
    payload = _power(_unix_hours("2026-10-01"), {"Solar": [float("nan")] + [1.0] * 23})
    respx.get(POWER_URL).respond(
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    client = _client()

    with pytest.raises(FetchValidationError, match="non-finite"):
        client.fetch_series(SOLAR, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))


@respx.mock
def test_price_unit_deviation_raises_validation_error() -> None:
    respx.get(PRICE_URL).respond(
        json=_price_response(_unix_hours("2026-10-01"), [50.0] * 24, unit="USD/MWh")
    )
    client = _client()

    with pytest.raises(FetchValidationError, match="unit"):
        client.fetch_series(PRICE, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))


@respx.mock
def test_missing_production_types_raises_validation_error() -> None:
    respx.get(POWER_URL).respond(json={"unix_seconds": _unix_hours("2026-10-01")})
    client = _client()

    with pytest.raises(FetchValidationError, match="production_types"):
        client.fetch_series(SOLAR, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))


@respx.mock
def test_series_length_mismatch_raises_validation_error() -> None:
    respx.get(POWER_URL).respond(
        json=_power(_unix_hours("2026-10-01"), {"Solar": [1.0] * 23})
    )
    client = _client()

    with pytest.raises(FetchValidationError, match="values"):
        client.fetch_series(SOLAR, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))


@respx.mock
def test_price_length_mismatch_raises_validation_error() -> None:
    respx.get(PRICE_URL).respond(
        json=_price_response(_unix_hours("2026-10-01"), [50.0] * 23)
    )
    client = _client()

    with pytest.raises(FetchValidationError, match="price"):
        client.fetch_series(PRICE, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))


@respx.mock
def test_non_integer_unix_seconds_raises_validation_error() -> None:
    respx.get(POWER_URL).respond(
        json=_power(["1759276800"], {"Solar": [1.0]})  # type: ignore[arg-type]
    )
    client = _client()

    with pytest.raises(FetchValidationError, match="unix_seconds"):
        client.fetch_series(SOLAR, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))


def test_weather_metric_raises_config_error() -> None:
    client = _client()

    with pytest.raises(FetchConfigError, match="not covered"):
        client.fetch_series(
            "weather.temperature_2m", DE_LU, _dt("2026-10-01"), _dt("2026-10-02")
        )


def test_unknown_metric_raises_config_error() -> None:
    client = _client()

    with pytest.raises(FetchConfigError, match="not covered"):
        client.fetch_series(
            "generation.foo", DE_LU, _dt("2026-10-01"), _dt("2026-10-02")
        )


def test_weather_point_region_raises_config_error() -> None:
    client = _client()

    with pytest.raises(FetchConfigError, match="not covered"):
        client.fetch_series(SOLAR, "DE-BERLIN", _dt("2026-10-01"), _dt("2026-10-02"))


def test_unknown_region_raises_config_error() -> None:
    client = _client()

    with pytest.raises(FetchConfigError, match="not covered"):
        client.fetch_series(SOLAR, "AT", _dt("2026-10-01"), _dt("2026-10-02"))


def test_naive_start_raises_config_error() -> None:
    client = _client()

    with pytest.raises(FetchConfigError, match="timezone-aware"):
        client.fetch_series(SOLAR, DE_LU, datetime(2026, 10, 1), _dt("2026-10-02"))


def test_start_not_before_end_raises_config_error() -> None:
    client = _client()

    with pytest.raises(FetchConfigError, match="before end"):
        client.fetch_series(SOLAR, DE_LU, _dt("2026-10-02"), _dt("2026-10-01"))


@respx.mock
def test_power_non_object_response_raises_validation_error() -> None:
    respx.get(POWER_URL).respond(json=[])
    client = _client()

    with pytest.raises(FetchValidationError, match="not a JSON object"):
        client.fetch_series(SOLAR, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))


@respx.mock
def test_power_missing_unix_seconds_raises_validation_error() -> None:
    respx.get(POWER_URL).respond(json={"production_types": []})
    client = _client()

    with pytest.raises(FetchValidationError, match="missing 'unix_seconds'"):
        client.fetch_series(SOLAR, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))


@respx.mock
def test_production_type_without_name_raises_validation_error() -> None:
    respx.get(POWER_URL).respond(
        json={"unix_seconds": _unix_hours("2026-10-01"), "production_types": [{}]}
    )
    client = _client()

    with pytest.raises(FetchValidationError, match="without string 'name'"):
        client.fetch_series(SOLAR, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))


@respx.mock
def test_price_non_object_response_raises_validation_error() -> None:
    respx.get(PRICE_URL).respond(json=[])
    client = _client()

    with pytest.raises(FetchValidationError, match="not a JSON object"):
        client.fetch_series(PRICE, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))


@respx.mock
def test_price_missing_unix_seconds_raises_validation_error() -> None:
    respx.get(PRICE_URL).respond(json={"price": []})
    client = _client()

    with pytest.raises(FetchValidationError, match="missing 'unix_seconds'"):
        client.fetch_series(PRICE, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))


@respx.mock
def test_price_non_integer_unix_seconds_raises_validation_error() -> None:
    respx.get(PRICE_URL).respond(
        json=_price_response(["1759276800"], [50.0])  # type: ignore[arg-type]
    )
    client = _client()

    with pytest.raises(FetchValidationError, match="unix_seconds"):
        client.fetch_series(PRICE, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))


@respx.mock
def test_price_null_value_skips_hour() -> None:
    # A None price means the hourly mean would be partial -> missing row.
    prices: list[object] = [10.0, 20.0, 30.0, 40.0] * 2 + [50.0] * 88
    prices[1] = None
    respx.get(PRICE_URL).respond(
        json=_price_response(_unix_quarters("2026-10-01"), prices)
    )
    client = _client()

    records = list(
        client.fetch_series(PRICE, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))
    )

    assert len(records) == 23
    assert _dt("2026-10-01", 0) not in {record.ts_utc for record in records}


@respx.mock
def test_single_timestamp_price_defaults_to_hourly_steps() -> None:
    # Fewer than two timestamps: steps per hour defaults to 1 -> the one
    # value becomes an hourly record.
    respx.get(PRICE_URL).respond(
        json=_price_response([int(_dt("2026-10-01", 10).timestamp())], [42.0])
    )
    client = _client()

    records = list(
        client.fetch_series(PRICE, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))
    )

    assert len(records) == 1
    assert records[0].ts_utc == _dt("2026-10-01", 10)
    assert records[0].value == pytest.approx(42.0)


@respx.mock
def test_price_unexpected_step_size_raises_validation_error() -> None:
    base = int(_dt("2026-10-01", 10).timestamp())
    unix = [base, base + 700, base + 1400]
    respx.get(PRICE_URL).respond(json=_price_response(unix, [1.0, 2.0, 3.0]))
    client = _client()

    with pytest.raises(FetchValidationError, match="step size"):
        client.fetch_series(PRICE, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))


def test_derived_metric_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    derived = METRICS[SOLAR].model_copy(update={"derived": True})
    monkeypatch.setattr(energy_charts, "METRICS", {**METRICS, SOLAR: derived})
    client = _client()

    with pytest.raises(FetchConfigError, match="derived"):
        client.fetch_series(SOLAR, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))


def test_unknown_metric_entry_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Metric is covered by the source map but missing from METRICS.
    monkeypatch.setattr(energy_charts, "METRICS", {})
    client = _client()

    with pytest.raises(FetchConfigError, match="unknown metric"):
        client.fetch_series(SOLAR, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))


def test_region_without_bidding_zone_entry_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Region is mapped for the source but has no REGIONS entry -> not a
    # usable bidding zone.
    monkeypatch.setattr(energy_charts, "REGIONS", {})
    client = _client()

    with pytest.raises(FetchConfigError, match="bidding zone"):
        client.fetch_series(SOLAR, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))


def test_clock_and_sleep_kwargs_pass_through() -> None:
    clock = FakeClock()

    client = EnergyChartsClient(
        settings=FetchSettings(http_backoff_base_seconds=0.0),
        clock=clock,
        sleep=clock.sleep,
    )

    assert client is not None


def test_import_guard_rejects_series_name_without_source_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Module-level guard: every _SERIES_NAMES target metric must be covered
    # by SOURCE_METRIC_MAP (never hardcoded keys).
    import importlib

    import wattfrosch_fetch.contracts as contracts_module
    from wattfrosch_fetch.clients import energy_charts as energy_charts_module

    modified = {
        source: {m for m in metrics if m != SOLAR}
        for source, metrics in contracts_module.SOURCE_METRIC_MAP.items()
    }
    monkeypatch.setattr(contracts_module, "SOURCE_METRIC_MAP", modified)

    with pytest.raises(FetchConfigError, match="generation.solar"):
        importlib.reload(energy_charts_module)

    monkeypatch.undo()
    importlib.reload(energy_charts_module)
