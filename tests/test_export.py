"""Export tests: parquet/csv/NDJSON round-trips, guard, empty input.

Records are constructed directly — no HTTP, no client involvement.
"""

import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow.parquet as pq
import pytest

from wattfrosch_fetch.contracts import MeasurementRecord
from wattfrosch_fetch.export import (
    write_csv,
    write_ndjson,
    write_parquet,
    write_records,
)
from wattfrosch_fetch.http import FetchConfigError, FetchValidationError

_BASE: dict[str, Any] = {
    "ts_utc": "2026-10-10T00:00:00Z",
    "region": "DE-LU",
    "metric": "consumption.load",
    "value": 288.15,
    "source": "smard",
    "resolution": "PT1H",
}


def _record(**overrides: Any) -> MeasurementRecord:
    return MeasurementRecord.model_validate({**_BASE, **overrides})


def _records(count: int) -> list[MeasurementRecord]:
    start = datetime(2026, 10, 10, tzinfo=UTC)
    return [
        _record(ts_utc=(start + timedelta(hours=h)).isoformat()) for h in range(count)
    ]


_COLUMNS = ("ts_utc", "region", "metric", "value", "source", "resolution")
_CSV_HEADER = '"ts_utc","region","metric","value","source","resolution"'


def test_parquet_round_trip_schema_and_values() -> None:
    sink = io.BytesIO()
    write_parquet(_records(3), sink)
    table = pq.read_table(io.BytesIO(sink.getvalue()))
    assert table.num_rows == 3
    assert table.column_names == list(_COLUMNS)
    assert str(table.schema.field("ts_utc").type) == "timestamp[us, tz=UTC]"
    assert str(table.schema.field("value").type) == "double"
    values = table.column("value").to_pylist()
    assert values == [288.15, 288.15, 288.15]
    ts = table.column("ts_utc").to_pylist()[0]
    assert ts == datetime(2026, 10, 10, tzinfo=UTC)


def test_export_keeps_kelvin_without_conversion() -> None:
    record = _record(
        metric="weather.temperature_2m",
        region="DE-KOELN",
        source="open-meteo",
        value=273.15,
    )
    sink = io.BytesIO()
    write_parquet([record], sink)
    table = pq.read_table(io.BytesIO(sink.getvalue()))
    assert table.column("value").to_pylist() == [273.15]


def test_csv_round_trip_header_and_iso_timestamp() -> None:
    sink = io.BytesIO()
    write_csv(_records(2), sink)
    lines = sink.getvalue().decode().splitlines()
    assert lines[0] == _CSV_HEADER
    assert lines[1].startswith('"2026-10-10T00:00:00+00:00","DE-LU"')
    assert len(lines) == 3


def test_ndjson_one_object_per_line() -> None:
    sink = io.BytesIO()
    write_ndjson(_records(2), sink)
    lines = sink.getvalue().decode().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert set(first) == set(_COLUMNS)
    assert first["ts_utc"] == "2026-10-10T00:00:00Z"
    assert first["value"] == 288.15
    assert first["resolution"] == "PT1H"


def test_write_records_dispatches_by_format() -> None:
    parquet_sink = io.BytesIO()
    write_records(_records(1), parquet_sink, "parquet")
    assert pq.read_table(io.BytesIO(parquet_sink.getvalue())).num_rows == 1

    csv_sink = io.BytesIO()
    write_records(_records(1), csv_sink, "csv")
    assert b"ts_utc" in csv_sink.getvalue()

    json_sink = io.BytesIO()
    write_records(_records(1), json_sink, "json")
    assert json.loads(json_sink.getvalue().decode().splitlines()[0])["value"] == 288.15


def test_write_records_unknown_format_fails_config() -> None:
    with pytest.raises(FetchConfigError, match="unknown export format"):
        write_records([], io.BytesIO(), "xlsx")


def test_guard_rejects_non_finite_values() -> None:
    for value in (float("nan"), float("inf"), float("-inf")):
        record = MeasurementRecord.model_construct(
            ts_utc=datetime(2026, 10, 10, tzinfo=UTC),
            region="DE-LU",
            metric="consumption.load",
            value=value,
            source="smard",
            resolution="PT1H",
        )
        with pytest.raises(FetchValidationError, match="non-finite value"):
            write_csv([record], io.BytesIO())
        with pytest.raises(FetchValidationError, match="non-finite value"):
            write_ndjson([record], io.BytesIO())
        with pytest.raises(FetchValidationError, match="non-finite value"):
            write_parquet([record], io.BytesIO())


def test_empty_input_writes_schema_only_or_nothing() -> None:
    parquet_sink = io.BytesIO()
    write_parquet([], parquet_sink)
    table = pq.read_table(io.BytesIO(parquet_sink.getvalue()))
    assert table.num_rows == 0
    assert table.column_names == list(_COLUMNS)

    csv_sink = io.BytesIO()
    write_csv([], csv_sink)
    assert csv_sink.getvalue().decode().splitlines() == [_CSV_HEADER]

    json_sink = io.BytesIO()
    write_ndjson([], json_sink)
    assert json_sink.getvalue() == b""
