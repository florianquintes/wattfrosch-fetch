"""CLI entry point with the uniform wattfrosch flags (--source, --metric, ...).

Exit codes (README "Exit-Codes des CLI"):
- 0 success (empty result is not an error, warn on stderr)
- 1 config/usage error (``FetchConfigError``, argparse errors)
- 2 HTTP/transport error after retries exhausted (``FetchHTTPError``)
- 3 response validation error (``FetchValidationError``)
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, NoReturn

from wattfrosch_fetch.clients import create_client
from wattfrosch_fetch.contracts import METRICS, REGIONS, MeasurementRecord
from wattfrosch_fetch.export import write_records
from wattfrosch_fetch.http import (
    FetchConfigError,
    FetchHTTPError,
    FetchValidationError,
)

_EXIT_OK: Final = 0
_EXIT_CONFIG: Final = 1
_EXIT_HTTP: Final = 2
_EXIT_VALIDATION: Final = 3

_FORMATS: Final[frozenset[str]] = frozenset({"parquet", "csv", "json"})

_EMPTY_RESULT_WARNING: Final = (
    "warning: empty result; gaps are filled in transform, not in fetch"
)


class _ArgumentParser(argparse.ArgumentParser):
    """argparse usage errors must exit 1 (default 2 collides with exit code 2)."""

    def error(self, message: str) -> NoReturn:
        raise FetchConfigError(message)


def _parse_bound(value: str, flag: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise FetchConfigError(
            f"{flag} is not an ISO 8601 date or datetime: {value!r}",
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _build_parser() -> _ArgumentParser:
    parser = _ArgumentParser(
        prog="wattfrosch-fetch",
        description="Fetch external weather and energy data and normalize it "
        "to the wattfrosch data contract (MeasurementRecord).",
    )
    parser.add_argument("--source", required=True, help="data source name")
    parser.add_argument(
        "--metric",
        action="append",
        required=True,
        metavar="METRIC",
        help="metric key, repeatable",
    )
    parser.add_argument("--region", required=True, help="internal region code")
    parser.add_argument(
        "--from",
        dest="from_",
        required=True,
        metavar="ISO8601",
        help="window start, half-open [from, to)",
    )
    parser.add_argument(
        "--to",
        dest="to_",
        required=True,
        metavar="ISO8601",
        help="window end, exclusive",
    )
    parser.add_argument(
        "--out",
        help="output path; data goes to stdout when omitted",
    )
    parser.add_argument(
        "--format",
        choices=sorted(_FORMATS),
        default="parquet",
        help="export format (default: parquet)",
    )
    return parser


def _validate_args(args: argparse.Namespace) -> tuple[datetime, datetime]:
    for metric in args.metric:
        entry = METRICS.get(metric)
        if entry is None:
            raise FetchConfigError(
                f"unknown metric {metric!r}; known metrics: {sorted(METRICS)}",
            )
        if entry.derived:
            raise FetchConfigError(
                f"metric {metric!r} is derived and is never fetched; "
                "derived metrics are computed in transform",
            )
    if args.region not in REGIONS:
        raise FetchConfigError(
            f"unknown region {args.region!r}; known regions: {sorted(REGIONS)}",
        )
    start = _parse_bound(args.from_, "--from")
    end = _parse_bound(args.to_, "--to")
    if start >= end:
        raise FetchConfigError(
            f"--from {start.isoformat()} is not before --to {end.isoformat()}; "
            "the window is half-open [from, to)",
        )
    return start, end


def _fetch_records(
    args: argparse.Namespace,
    start: datetime,
    end: datetime,
) -> Iterator[tuple[str, list[MeasurementRecord]]]:
    client = create_client(args.source)
    try:
        for metric in args.metric:
            records = list(client.fetch_series(metric, args.region, start, end))
            yield metric, records
    finally:
        client.close()


def _warn_format_mismatch(args: argparse.Namespace) -> None:
    suffix = Path(args.out).suffix.lstrip(".").lower()
    if suffix in _FORMATS and suffix != args.format:
        print(
            f"warning: --format {args.format} does not match "
            f"--out extension .{suffix}; --format wins",
            file=sys.stderr,
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one fetch; returns the process exit code."""
    parser = _build_parser()
    try:
        try:
            args = parser.parse_args(argv)
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else _EXIT_OK
        start, end = _validate_args(args)
        total = 0
        records: list[MeasurementRecord] = []
        for metric, fetched in _fetch_records(args, start, end):
            print(f"metric={metric} records={len(fetched)}", file=sys.stderr)
            total += len(fetched)
            records.extend(fetched)
        if total == 0:
            print(_EMPTY_RESULT_WARNING, file=sys.stderr)
        if args.out is not None:
            _warn_format_mismatch(args)
            with open(args.out, "wb") as sink:
                write_records(records, sink, args.format)
        else:
            write_records(records, sys.stdout.buffer, args.format)
    except FetchConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_CONFIG
    except FetchHTTPError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_HTTP
    except FetchValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_VALIDATION
    return _EXIT_OK
