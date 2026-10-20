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
from typing import Final, NoReturn

from wattfrosch_fetch.clients import create_client
from wattfrosch_fetch.contracts import METRICS, REGIONS
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
    parser.add_argument("--out", help="output path (export lands in a later issue)")
    parser.add_argument(
        "--format",
        choices=sorted(_FORMATS),
        default="parquet",
        help="export format (export lands in a later issue)",
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


def _fetch_counts(
    args: argparse.Namespace,
    start: datetime,
    end: datetime,
) -> Iterator[tuple[str, int]]:
    client = create_client(args.source)
    try:
        for metric in args.metric:
            records = client.fetch_series(metric, args.region, start, end)
            yield metric, sum(1 for _ in records)
    finally:
        client.close()


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
        for metric, count in _fetch_counts(args, start, end):
            print(f"metric={metric} records={count}", file=sys.stderr)
            total += count
        if total == 0:
            print(_EMPTY_RESULT_WARNING, file=sys.stderr)
        if args.out is not None:
            print(
                "warning: --out is accepted but export is not implemented yet",
                file=sys.stderr,
            )
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
