"""Tests for the SMARD client (respx only — no real API calls)."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import respx

from wattfrosch_fetch.clients.smard import SmardClient
from wattfrosch_fetch.http import FetchConfigError, FetchSettings, FetchValidationError

BASE = "https://www.smard.de/app/chart_data"

LIGNITE = "generation.lignite"
LOAD = "consumption.load"
OTHER_RENEWABLE = "generation.other_renewable"
PRICE = "price.day_ahead"

FILTER_IDS = {
    LIGNITE: "1223",
    LOAD: "410",
    OTHER_RENEWABLE: "1228",
    PRICE: "4169",
}

DE_LU = "DE-LU"
DE_AT_LU = "DE-AT-LU"

# Bidding-zone change: DE-AT-LU ends 2018-10-01, DE-LU starts 2018-10-01.
BOUNDARY_DAY = "2018-10-01"


def _client(**settings_kwargs: object) -> SmardClient:
    settings = FetchSettings(http_backoff_base_seconds=0.0, **settings_kwargs)  # type: ignore[arg-type]
    return SmardClient(settings=settings)


def _dt(day: str, hour: int = 0) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:00").replace(tzinfo=UTC)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _index_url(filter_id: str, region: str) -> str:
    return f"{BASE}/{filter_id}/{region}/index_hour.json"


def _bucket_url(filter_id: str, region: str, ts_ms: int) -> str:
    # filterCopy/regionCopy quirk: the path parameters repeat in the stem.
    return f"{BASE}/{filter_id}/{region}/{filter_id}_{region}_hour_{ts_ms}.json"


def _hourly_rows(start_ms: int, values: list[object]) -> list[list[object]]:
    return [[start_ms + hour * 3_600_000, value] for hour, value in enumerate(values)]


def _bucket(start_ms: int, values: list[object]) -> dict:
    return {
        "meta_data": {"version": 1, "created": 1759874769021},
        "series": _hourly_rows(start_ms, values),
    }


def _index(timestamps_ms: list[int]) -> dict:
    return {"timestamps": timestamps_ms}


def _mock_index(
    filter_id: str, region: str, timestamps_ms: list[int], *, status_code: int = 200
) -> respx.Route:
    if status_code == 200:
        return respx.get(_index_url(filter_id, region)).respond(
            json=_index(timestamps_ms)
        )
    return respx.get(_index_url(filter_id, region)).respond(status_code=status_code)


def _mock_bucket(
    filter_id: str, region: str, ts_ms: int, payload: dict, *, status_code: int = 200
) -> respx.Route:
    if status_code == 200:
        return respx.get(_bucket_url(filter_id, region, ts_ms)).respond(json=payload)
    return respx.get(_bucket_url(filter_id, region, ts_ms)).respond(
        status_code=status_code
    )


@respx.mock
def test_hourly_rows_pass_through() -> None:
    day = "2026-10-01"
    bucket_ms = _ms(_dt(day))
    index_route = _mock_index(FILTER_IDS[LOAD], DE_LU, [bucket_ms])
    bucket_route = _mock_bucket(
        FILTER_IDS[LOAD], DE_LU, bucket_ms, _bucket(bucket_ms, [100.5] * 24)
    )
    client = _client()

    records = list(client.fetch_series(LOAD, DE_LU, _dt(day), _dt("2026-10-02")))

    assert index_route.call_count == 1
    assert bucket_route.call_count == 1
    # Bucket URL quirk: filter and region repeat in the file stem.
    assert bucket_route.calls.last.request.url.path == (
        f"/app/chart_data/410/DE-LU/410_DE-LU_hour_{bucket_ms}.json"
    )
    assert len(records) == 24
    assert records[0].ts_utc == _dt(day)
    assert records[0].value == pytest.approx(100.5)
    assert records[0].source == "smard"
    assert records[0].region == DE_LU
    assert records[0].metric == LOAD
    assert records[0].resolution == "PT1H"


@respx.mock
def test_filter_id_comes_from_source_metric_map() -> None:
    day = "2026-10-01"
    bucket_ms = _ms(_dt(day))
    index_route = _mock_index(FILTER_IDS[LIGNITE], DE_LU, [bucket_ms])
    _mock_bucket(FILTER_IDS[LIGNITE], DE_LU, bucket_ms, _bucket(bucket_ms, [1.0] * 24))
    client = _client()

    records = list(client.fetch_series(LIGNITE, DE_LU, _dt(day), _dt("2026-10-02")))

    assert len(records) == 24
    assert index_route.calls.last.request.url.path == (
        "/app/chart_data/1223/DE-LU/index_hour.json"
    )


@respx.mock
def test_price_uses_documented_filter_4169() -> None:
    day = "2026-10-01"
    bucket_ms = _ms(_dt(day))
    _mock_index(FILTER_IDS[PRICE], DE_LU, [bucket_ms])
    bucket_route = _mock_bucket(
        FILTER_IDS[PRICE], DE_LU, bucket_ms, _bucket(bucket_ms, [42.0] * 24)
    )
    client = _client()

    records = list(client.fetch_series(PRICE, DE_LU, _dt(day), _dt("2026-10-02")))

    assert len(records) == 24
    assert bucket_route.calls.last.request.url.path == (
        f"/app/chart_data/4169/DE-LU/4169_DE-LU_hour_{bucket_ms}.json"
    )


@respx.mock
def test_other_renewable_available() -> None:
    # Unlike energy-charts, SMARD covers generation.other_renewable (1228).
    day = "2026-10-01"
    bucket_ms = _ms(_dt(day))
    _mock_index(FILTER_IDS[OTHER_RENEWABLE], DE_LU, [bucket_ms])
    _mock_bucket(
        FILTER_IDS[OTHER_RENEWABLE], DE_LU, bucket_ms, _bucket(bucket_ms, [7.0] * 24)
    )
    client = _client()

    records = list(
        client.fetch_series(OTHER_RENEWABLE, DE_LU, _dt(day), _dt("2026-10-02"))
    )

    assert len(records) == 24
    assert records[0].metric == OTHER_RENEWABLE


@respx.mock
def test_half_open_window_filters_rows() -> None:
    day = "2026-10-01"
    bucket_ms = _ms(_dt(day))
    _mock_index(FILTER_IDS[LOAD], DE_LU, [bucket_ms])
    _mock_bucket(FILTER_IDS[LOAD], DE_LU, bucket_ms, _bucket(bucket_ms, [1.0] * 24))
    client = _client()

    records = list(client.fetch_series(LOAD, DE_LU, _dt(day, 10), _dt(day, 12)))

    # Half-open [10:00, 12:00): 12:00 row (inclusive end) is dropped.
    assert [record.ts_utc for record in records] == [_dt(day, 10), _dt(day, 11)]


@respx.mock
def test_rows_outside_window_not_requested_twice() -> None:
    # Only the intersecting bucket is fetched, not every index entry.
    day = "2026-10-05"
    bucket_ms = _ms(_dt(day))
    _mock_index(FILTER_IDS[LOAD], DE_LU, [bucket_ms - 7 * 24 * 3_600_000, bucket_ms])
    bucket_route = _mock_bucket(
        FILTER_IDS[LOAD], DE_LU, bucket_ms, _bucket(bucket_ms, [2.0] * 24)
    )
    client = _client()

    records = list(client.fetch_series(LOAD, DE_LU, _dt(day), _dt("2026-10-06")))

    assert bucket_route.call_count == 1
    assert len(records) == 24


@respx.mock
def test_gap_between_buckets_stays_gap() -> None:
    # Two weekly buckets, each covering only its first day with rows.
    first_day, second_day = "2026-10-01", "2026-10-08"
    first_ms, second_ms = _ms(_dt(first_day)), _ms(_dt(second_day))
    _mock_index(FILTER_IDS[LOAD], DE_LU, [first_ms, second_ms])
    _mock_bucket(FILTER_IDS[LOAD], DE_LU, first_ms, _bucket(first_ms, [1.0] * 24))
    _mock_bucket(FILTER_IDS[LOAD], DE_LU, second_ms, _bucket(second_ms, [2.0] * 24))
    client = _client()

    records = list(client.fetch_series(LOAD, DE_LU, _dt(first_day), _dt("2026-10-09")))

    assert len(records) == 48
    assert records[0].ts_utc == _dt(first_day)
    assert records[23].ts_utc == _dt(first_day, 23)
    # The days in between have no rows -> gap stays gap (no NULL rows).
    assert records[24].ts_utc == _dt(second_day)


@respx.mock
def test_null_values_skipped() -> None:
    day = "2026-10-01"
    bucket_ms = _ms(_dt(day))
    _mock_index(FILTER_IDS[LOAD], DE_LU, [bucket_ms])
    # Leading null like the live API; nulls inside the window are skipped.
    _mock_bucket(
        FILTER_IDS[LOAD],
        DE_LU,
        bucket_ms,
        _bucket(bucket_ms, [None, 1.0, None, 3.0] + [0.0] * 20),
    )
    client = _client()

    records = list(client.fetch_series(LOAD, DE_LU, _dt(day), _dt("2026-10-02")))

    assert [record.ts_utc for record in records] == [
        _dt(day, 1),
        _dt(day, 3),
        *[_dt(day, hour) for hour in range(4, 24)],
    ]


@respx.mock
def test_non_finite_value_raises() -> None:
    day = "2026-10-01"
    bucket_ms = _ms(_dt(day))
    _mock_index(FILTER_IDS[LOAD], DE_LU, [bucket_ms])
    payload = _bucket(bucket_ms, [float("nan")] + [1.0] * 23)
    respx.get(_bucket_url(FILTER_IDS[LOAD], DE_LU, bucket_ms)).respond(
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    client = _client()

    with pytest.raises(FetchValidationError, match="non-finite"):
        list(client.fetch_series(LOAD, DE_LU, _dt(day), _dt("2026-10-02")))


@respx.mock
@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"meta_data": {}, "series": "nope"}, "missing 'series' array"),
        ({"meta_data": {}, "series": [[_ms(_dt("2026-10-01"))]]}, "pairs"),
        (
            {"meta_data": {}, "series": [["2026-10-01", 1.0]]},
            "pairs",
        ),
        (
            {"meta_data": {}, "series": [[_ms(_dt("2026-10-01")), "1.0"]]},
            "non-numeric value",
        ),
        ({"series": [[_ms(_dt("2026-10-01")), 1.0]]}, "missing 'meta_data'"),
        ({"meta_data": {}, "series": []}, None),
    ],
)
def test_malformed_bucket_payloads(payload: dict, match: str | None) -> None:
    day = "2026-10-01"
    bucket_ms = _ms(_dt(day))
    _mock_index(FILTER_IDS[LOAD], DE_LU, [bucket_ms])
    respx.get(_bucket_url(FILTER_IDS[LOAD], DE_LU, bucket_ms)).respond(json=payload)
    client = _client()

    if match is None:
        # An empty series is valid — it just yields no rows.
        records = list(client.fetch_series(LOAD, DE_LU, _dt(day), _dt("2026-10-02")))
        assert records == []
        return
    with pytest.raises(FetchValidationError, match=match):
        list(client.fetch_series(LOAD, DE_LU, _dt(day), _dt("2026-10-02")))


@respx.mock
def test_index_404_yields_empty_result() -> None:
    _mock_index(FILTER_IDS[LOAD], DE_LU, [], status_code=404)
    client = _client()

    records = list(
        client.fetch_series(LOAD, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))
    )

    assert records == []


@respx.mock
def test_index_empty_timestamps_yields_empty_result() -> None:
    _mock_index(FILTER_IDS[LOAD], DE_LU, [])
    client = _client()

    records = list(
        client.fetch_series(LOAD, DE_LU, _dt("2026-10-01"), _dt("2026-10-02"))
    )

    assert records == []


@respx.mock
def test_index_not_increasing_raises() -> None:
    day = "2026-10-01"
    bucket_ms = _ms(_dt(day))
    _mock_index(FILTER_IDS[LOAD], DE_LU, [bucket_ms, bucket_ms])
    client = _client()

    with pytest.raises(FetchValidationError, match="not increasing"):
        list(client.fetch_series(LOAD, DE_LU, _dt(day), _dt("2026-10-02")))


@respx.mock
def test_bucket_404_on_listed_timestamp_raises() -> None:
    day = "2026-10-01"
    bucket_ms = _ms(_dt(day))
    _mock_index(FILTER_IDS[LOAD], DE_LU, [bucket_ms])
    _mock_bucket(FILTER_IDS[LOAD], DE_LU, bucket_ms, {}, status_code=404)
    client = _client()

    with pytest.raises(FetchValidationError, match="missing \\(404\\)"):
        list(client.fetch_series(LOAD, DE_LU, _dt(day), _dt("2026-10-02")))


@respx.mock
def test_duplicate_timestamp_across_buckets_raises() -> None:
    # The second bucket repeats a row from the first -> API deviation.
    first_day, second_day = "2026-10-01", "2026-10-08"
    first_ms, second_ms = _ms(_dt(first_day)), _ms(_dt(second_day))
    _mock_index(FILTER_IDS[LOAD], DE_LU, [first_ms, second_ms])
    _mock_bucket(FILTER_IDS[LOAD], DE_LU, first_ms, _bucket(first_ms, [1.0] * 24))
    # Second bucket repeats the first bucket's first row -> duplicate ts.
    _mock_bucket(FILTER_IDS[LOAD], DE_LU, second_ms, _bucket(first_ms, [9.0] * 25))
    client = _client()

    with pytest.raises(FetchValidationError, match="duplicate timestamp"):
        list(client.fetch_series(LOAD, DE_LU, _dt(first_day), _dt("2026-10-09")))


@respx.mock
def test_window_outside_validity_zero_requests() -> None:
    index_route = _mock_index(FILTER_IDS[LOAD], DE_AT_LU, [])
    client = _client()

    records = list(
        client.fetch_series(LOAD, DE_AT_LU, _dt("2026-10-01"), _dt("2026-10-02"))
    )

    assert records == []
    assert index_route.call_count == 0


@respx.mock
def test_de_at_lu_clamped_at_boundary() -> None:
    # Last DE-AT-LU bucket starts 2018-10-01 00:00 CEST (= 2018-09-30 22:00 UTC);
    # the window end clamps to 2018-10-01 00:00 UTC.
    bucket_ms = _ms(_dt(BOUNDARY_DAY) - timedelta(hours=2))
    _mock_index(FILTER_IDS[LOAD], DE_AT_LU, [bucket_ms])
    _mock_bucket(
        FILTER_IDS[LOAD],
        DE_AT_LU,
        bucket_ms,
        _bucket(bucket_ms, [1.0, 2.0] + [0.0] * 166),
    )
    client = _client()

    records = list(
        client.fetch_series(LOAD, DE_AT_LU, _dt("2018-09-30"), _dt("2018-10-02"))
    )

    assert [record.ts_utc for record in records] == [
        _dt("2018-09-30", 22),
        _dt("2018-09-30", 23),
    ]


@respx.mock
def test_de_lu_clamped_at_boundary() -> None:
    # DE-LU starts 2018-10-01; rows before the boundary are never emitted.
    bucket_ms = _ms(_dt(BOUNDARY_DAY))
    _mock_index(FILTER_IDS[LOAD], DE_LU, [bucket_ms])
    _mock_bucket(FILTER_IDS[LOAD], DE_LU, bucket_ms, _bucket(bucket_ms, [1.0] * 24))
    client = _client()

    records = list(
        client.fetch_series(LOAD, DE_LU, _dt("2018-09-29"), _dt("2018-10-01", 12))
    )

    assert [record.ts_utc for record in records] == [
        _dt(BOUNDARY_DAY, hour) for hour in range(12)
    ]


def test_weather_metric_config_error() -> None:
    client = _client()

    with pytest.raises(FetchConfigError, match="not covered by source 'smard'"):
        list(
            client.fetch_series(
                "weather.temperature_2m",
                DE_LU,
                _dt("2026-10-01"),
                _dt("2026-10-02"),
            )
        )


def test_generation_oil_config_error() -> None:
    # No documented SMARD filter — never invented (contract rule).
    client = _client()

    with pytest.raises(FetchConfigError, match="not covered by source 'smard'"):
        list(
            client.fetch_series(
                "generation.oil", DE_LU, _dt("2026-10-01"), _dt("2026-10-02")
            )
        )


def test_weather_point_region_config_error() -> None:
    client = _client()

    with pytest.raises(FetchConfigError, match="not covered by source 'smard'"):
        list(
            client.fetch_series(LOAD, "DE-BERLIN", _dt("2026-10-01"), _dt("2026-10-02"))
        )


def test_naive_datetimes_config_error() -> None:
    client = _client()

    with pytest.raises(FetchConfigError, match="timezone-aware"):
        list(
            client.fetch_series(
                LOAD,
                DE_LU,
                datetime(2026, 10, 1),
                _dt("2026-10-02"),
            )
        )


def test_start_not_before_end_config_error() -> None:
    client = _client()

    with pytest.raises(FetchConfigError, match="half-open"):
        list(client.fetch_series(LOAD, DE_LU, _dt("2026-10-02"), _dt("2026-10-01")))
