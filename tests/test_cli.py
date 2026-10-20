"""CLI tests: flags, validation, exit-code mapping, summary output.

Clients are stubbed via the ``CLIENTS`` registry (monkeypatched) — no HTTP at
all; client behavior itself is covered by the respx test suites per client.
"""

import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow.parquet as pq
import pytest

from wattfrosch_fetch import cli
from wattfrosch_fetch.clients import CLIENTS
from wattfrosch_fetch.contracts import METRICS, MeasurementRecord

BASE_ARGS = (
    "--source",
    "stub",
    "--metric",
    "consumption.load",
    "--region",
    "DE-LU",
    "--from",
    "2026-10-10",
    "--to",
    "2026-10-12",
)


@dataclass
class _StubClient:
    records: list[MeasurementRecord] = field(default_factory=list)
    error: Exception | None = None
    calls: list[tuple[str, str, datetime, datetime]] = field(default_factory=list)
    closed: bool = False

    def fetch_series(
        self,
        metric: str,
        region: str,
        start: datetime,
        end: datetime,
    ):
        self.calls.append((metric, region, start, end))
        if self.error is not None:
            raise self.error
        return iter(self.records)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _StubClient:
    instance = _StubClient()
    monkeypatch.setitem(CLIENTS, "stub", lambda: instance)
    return instance


def _record(ts: datetime) -> MeasurementRecord:
    return MeasurementRecord(
        ts_utc=ts,
        region="DE-LU",
        metric="consumption.load",
        value=42.0,
        source="smard",
        resolution="PT1H",
    )


def _records(hours: int) -> list[MeasurementRecord]:
    day = datetime(2026, 10, 10, tzinfo=UTC)
    return [_record(day + timedelta(hours=h)) for h in range(hours)]


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "--source" in out
    assert "--metric" in out
    assert "--format" in out


def test_success_prints_counts_and_closes_client(
    stub: _StubClient,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub.records = _records(48)
    argv = (*BASE_ARGS, "--out", str(tmp_path / "out.parquet"))
    assert cli.main(list(argv)) == 0
    err = capsys.readouterr().err
    assert "metric=consumption.load records=48" in err
    assert stub.closed
    assert len(stub.calls) == 1
    metric, region, start, end = stub.calls[0]
    assert metric == "consumption.load"
    assert region == "DE-LU"
    assert start == datetime(2026, 10, 10, tzinfo=UTC)
    assert end == datetime(2026, 10, 12, tzinfo=UTC)


def test_repeatable_metric_fetches_each(
    stub: _StubClient,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub.records = _records(24)
    argv = (
        *BASE_ARGS,
        "--metric",
        "price.day_ahead",
        "--out",
        str(tmp_path / "out.parquet"),
    )
    assert cli.main(list(argv)) == 0
    assert [call[0] for call in stub.calls] == ["consumption.load", "price.day_ahead"]
    err = capsys.readouterr().err
    assert "metric=consumption.load records=24" in err
    assert "metric=price.day_ahead records=24" in err


def test_unknown_source_fails_with_exit_1(
    stub: _StubClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ("--source", "nope", *BASE_ARGS[2:])
    assert cli.main(list(argv)) == 1
    assert "unknown source" in capsys.readouterr().err
    assert not stub.closed


def test_unknown_metric_fails_with_exit_1(
    stub: _StubClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = (*BASE_ARGS[:-8], "--metric", "generation.bogus", *BASE_ARGS[-6:])
    assert cli.main(list(argv)) == 1
    assert "unknown metric" in capsys.readouterr().err


def test_derived_metric_fails_with_exit_1(
    monkeypatch: pytest.MonkeyPatch,
    stub: _StubClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    derived = METRICS["consumption.load"].model_copy(update={"derived": True})
    monkeypatch.setattr(
        cli, "METRICS", {**METRICS, "consumption.derived_stub": derived}
    )
    argv = (*BASE_ARGS[:-8], "--metric", "consumption.derived_stub", *BASE_ARGS[-6:])
    assert cli.main(list(argv)) == 1
    assert "derived" in capsys.readouterr().err


def test_unknown_region_fails_with_exit_1(
    stub: _StubClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = (*BASE_ARGS[:4], "--region", "AT", *BASE_ARGS[6:])
    assert cli.main(list(argv)) == 1
    assert "unknown region" in capsys.readouterr().err


def test_from_not_before_to_fails_with_exit_1(
    stub: _StubClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = (*BASE_ARGS[:-2], "--to", "2026-10-10")
    assert cli.main(list(argv)) == 1
    assert "--from" in capsys.readouterr().err


def test_malformed_date_fails_with_exit_1(
    stub: _StubClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = (*BASE_ARGS[:6], "--from", "not-a-date", *BASE_ARGS[8:])
    assert cli.main(list(argv)) == 1
    assert "ISO 8601" in capsys.readouterr().err


def test_missing_required_flag_is_usage_error_exit_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ("--source", "stub", "--region", "DE-LU")
    assert cli.main(list(argv)) == 1
    assert "error:" in capsys.readouterr().err


def test_invalid_format_fails_with_exit_1(
    stub: _StubClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = (*BASE_ARGS, "--format", "xlsx")
    assert cli.main(list(argv)) == 1
    assert "invalid choice" in capsys.readouterr().err


def test_empty_result_exits_zero_with_warning(
    stub: _StubClient,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = (*BASE_ARGS, "--out", str(tmp_path / "out.parquet"))
    assert cli.main(list(argv)) == 0
    err = capsys.readouterr().err
    assert "metric=consumption.load records=0" in err
    assert "warning: empty result" in err


def test_http_error_maps_to_exit_2(
    stub: _StubClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub.error = cli.FetchHTTPError("connection reset after retries")
    assert cli.main(list(BASE_ARGS)) == 2
    assert "connection reset" in capsys.readouterr().err


def test_validation_error_maps_to_exit_3(
    stub: _StubClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub.error = cli.FetchValidationError("series has parallel arrays")
    assert cli.main(list(BASE_ARGS)) == 3
    assert "parallel arrays" in capsys.readouterr().err


def test_config_error_from_client_maps_to_exit_1(
    stub: _StubClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub.error = cli.FetchConfigError("metric not covered by source")
    assert cli.main(list(BASE_ARGS)) == 1
    assert "not covered" in capsys.readouterr().err


def test_out_writes_ndjson_file(
    stub: _StubClient,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub.records = _records(2)
    out = tmp_path / "out.json"
    argv = (*BASE_ARGS, "--out", str(out), "--format", "json")
    assert cli.main(list(argv)) == 0
    lines = out.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["value"] == 42.0
    assert stub.closed


def test_out_writes_parquet_file(
    stub: _StubClient,
    tmp_path: Any,
) -> None:
    stub.records = _records(2)
    out = tmp_path / "out.parquet"
    argv = (*BASE_ARGS, "--out", str(out), "--format", "parquet")
    assert cli.main(list(argv)) == 0
    table = pq.read_table(out)
    assert table.num_rows == 2
    assert table.column("value").to_pylist() == [42.0, 42.0]


def test_stdout_sink_writes_json(
    stub: _StubClient,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    stub.records = _records(1)
    assert cli.main(list(BASE_ARGS + ("--format", "json"))) == 0
    lines = capsysbinary.readouterr().out.decode().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["metric"] == "consumption.load"


def test_stdout_sink_writes_parquet(
    stub: _StubClient,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    stub.records = _records(1)
    assert cli.main(list(BASE_ARGS)) == 0
    table = pq.read_table(io.BytesIO(capsysbinary.readouterr().out))
    assert table.num_rows == 1


def test_format_extension_mismatch_warns(
    stub: _StubClient,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub.records = _records(1)
    out = tmp_path / "data.csv"
    argv = (*BASE_ARGS, "--out", str(out), "--format", "parquet")
    assert cli.main(list(argv)) == 0
    assert "--format parquet does not match" in capsys.readouterr().err
    assert pq.read_table(out).num_rows == 1


def test_empty_result_with_out_writes_empty_file(
    stub: _StubClient,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "out.parquet"
    argv = (*BASE_ARGS, "--out", str(out))
    assert cli.main(list(argv)) == 0
    assert "warning: empty result" in capsys.readouterr().err
    assert pq.read_table(out).num_rows == 0
