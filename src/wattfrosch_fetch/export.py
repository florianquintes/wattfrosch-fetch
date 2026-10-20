"""Export MeasurementRecords to parquet, csv or NDJSON (``--format``).

All writers take a binary sink (file opened in ``"wb"`` or
``sys.stdout.buffer``). Column order and schema follow the contract field
order of ``MeasurementRecord``; values are already in contract units
(e.g. Kelvin for temperature) — export never converts.

The contract already rejects NULL/NaN/Inf at construction
(``allow_inf_nan=False``); the finite-value guard here is the final
defense gate before anything lands in a file or on stdout.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from typing import BinaryIO, Final

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from wattfrosch_fetch.contracts import MeasurementRecord
from wattfrosch_fetch.http import FetchConfigError, FetchValidationError

_FIELDS: Final[tuple[str, ...]] = (
    "ts_utc",
    "region",
    "metric",
    "value",
    "source",
    "resolution",
)

_PARQUET_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        ("ts_utc", pa.timestamp("us", tz="UTC")),
        ("region", pa.string()),
        ("metric", pa.string()),
        ("value", pa.float64()),
        ("source", pa.string()),
        ("resolution", pa.string()),
    ]
)

_CSV_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        ("ts_utc", pa.string()),
        ("region", pa.string()),
        ("metric", pa.string()),
        ("value", pa.float64()),
        ("source", pa.string()),
        ("resolution", pa.string()),
    ]
)


def _guard(records: Sequence[MeasurementRecord]) -> None:
    for record in records:
        if not math.isfinite(record.value):
            raise FetchValidationError(
                f"non-finite value {record.value!r} for metric {record.metric!r} "
                f"at {record.ts_utc.isoformat()}"
            )


def _parquet_table(records: Sequence[MeasurementRecord]) -> pa.Table:
    return pa.Table.from_pylist(
        [record.model_dump() for record in records],
        schema=_PARQUET_SCHEMA,
    )


def _csv_table(records: Sequence[MeasurementRecord]) -> pa.Table:
    rows = []
    for record in records:
        row = record.model_dump()
        row["ts_utc"] = record.ts_utc.isoformat()
        rows.append(row)
    return pa.Table.from_pylist(rows, schema=_CSV_SCHEMA)


def write_parquet(records: Sequence[MeasurementRecord], sink: BinaryIO) -> None:
    _guard(records)
    pq.write_table(_parquet_table(records), sink)


def write_csv(records: Sequence[MeasurementRecord], sink: BinaryIO) -> None:
    _guard(records)
    pacsv.write_csv(_csv_table(records), sink)


def write_ndjson(records: Sequence[MeasurementRecord], sink: BinaryIO) -> None:
    _guard(records)
    sink.writelines(
        (json.dumps(record.model_dump(mode="json")) + "\n").encode()
        for record in records
    )


_WRITERS: Final[
    Mapping[str, Callable[[Sequence[MeasurementRecord], BinaryIO], None]]
] = {
    "parquet": write_parquet,
    "csv": write_csv,
    "json": write_ndjson,
}


def write_records(
    records: Sequence[MeasurementRecord],
    sink: BinaryIO,
    format: str,
) -> None:
    writer = _WRITERS.get(format)
    if writer is None:
        raise FetchConfigError(
            f"unknown export format {format!r}; known formats: {sorted(_WRITERS)}",
        )
    writer(records, sink)
