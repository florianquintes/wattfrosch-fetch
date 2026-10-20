"""SMARD client: Strommarktdaten der Bundesnetzagentur (Milestone 4).

SMARD paginates implicitly in two steps (Milestone 0 decision "Pagination
per source"): an index file per (filter, region) lists the available weekly
bucket timestamps, and each bucket file covers one week of hourly data.

API facts handled here (verified against the live API):

- index URL: ``chart_data/{filter}/{region}/index_{resolution}.json``
  returning ``{"timestamps": [ms epoch, ...]}``; bucket URL:
  ``chart_data/{filter}/{region}/{filter}_{region}_{resolution}_{ts}.json``
  returning ``{"meta_data": {...}, "series": [[ts_ms, value|null], ...]}``
  (pairs, not parallel arrays);
- the path parameters must be repeated as ``filterCopy``/``regionCopy``
  inside the bucket file name (broken API design per SMARD_readme.md);
- bucket boundaries are Berlin-local midnight, not UTC — irrelevant for the
  rows (epoch milliseconds are absolute), only for bucket selection, which
  uses the index timestamps themselves as interval edges;
- the hourly series uses absolute epoch steps, so a DST spring-forward week
  contains a missing hour (a gap, i.e. a missing row) and no duplicates;
- region validity windows are enforced by the API (the ``DE-AT-LU`` index
  ends at the 2018-10-01 bidding-zone change, ``DE-LU`` starts there), but
  the client still clamps the window to ``REGIONS`` for consistency;
- filter IDs come from ``SOURCE_METRIC_MAP`` (never hardcoded), region
  codes are identical to ``SOURCE_REGION_MAP``; ``generation.oil`` has no
  documented SMARD filter and is not covered by the map — requesting it is
  a config error, never an invented filter;
- units are already contract units (MW, EUR/MWh) — no conversions needed.

Strictness mirrors the other clients: derived metrics, naive datetimes and
uncovered metric/region combinations raise :class:`FetchConfigError``;
``None`` values become missing rows (no NULL rows); non-finite values,
malformed payloads, a 404 on a bucket the index listed and duplicate
timestamps across buckets raise :class:`FetchValidationError`. An index
that is missing (404) or empty yields an empty result (exit 0 + warning),
not an error.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Final

from wattfrosch_fetch.contracts import (
    METRICS,
    REGIONS,
    SOURCE_METRIC_MAP,
    SOURCE_REGION_MAP,
    MeasurementRecord,
)
from wattfrosch_fetch.http import (
    BaseClient,
    FetchConfigError,
    FetchHTTPError,
    FetchSettings,
    FetchValidationError,
)
from wattfrosch_fetch.validity import clamp_to_validity

SOURCE: Final[str] = "smard"

_API_BASE_URL: Final[str] = "https://www.smard.de/app/chart_data"

# The contract mandates PT1H only, so the SMARD resolution is fixed.
_RESOLUTION: Final[str] = "hour"
_MS_PER_HOUR: Final[int] = 3_600_000
# Fallback interval length for the last index entry (a regular week).
_WEEK_MS: Final[int] = 7 * 24 * _MS_PER_HOUR


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None:
        raise FetchConfigError(f"{name} must be timezone-aware (UTC expected)")


def _make_index_validator(url: str) -> Callable[[Any], None]:
    """Strict response shape check: deviations raise FetchValidationError."""

    def validate(data: Any) -> None:
        if not isinstance(data, dict):
            raise FetchValidationError(f"{url}: response is not a JSON object")
        timestamps = data.get("timestamps")
        if not isinstance(timestamps, list):
            raise FetchValidationError(f"{url}: missing 'timestamps' array")
        if not all(
            isinstance(item, int) and not isinstance(item, bool) for item in timestamps
        ):
            raise FetchValidationError(
                f"{url}: 'timestamps' contains non-integer entries"
            )
        if any(earlier >= later for earlier, later in itertools.pairwise(timestamps)):
            raise FetchValidationError(f"{url}: 'timestamps' are not increasing")

    return validate


def _make_bucket_validator(url: str) -> Callable[[Any], None]:
    """Strict response shape check: deviations raise FetchValidationError."""

    def validate(data: Any) -> None:
        if not isinstance(data, dict):
            raise FetchValidationError(f"{url}: response is not a JSON object")
        meta = data.get("meta_data")
        if not isinstance(meta, dict):
            raise FetchValidationError(f"{url}: missing 'meta_data' object")
        series = data.get("series")
        if not isinstance(series, list):
            raise FetchValidationError(f"{url}: missing 'series' array")
        for entry in series:
            if (
                not isinstance(entry, list)
                or len(entry) != 2
                or not isinstance(entry[0], int)
                or isinstance(entry[0], bool)
            ):
                raise FetchValidationError(
                    f"{url}: 'series' entries must be [timestamp_ms, value] pairs"
                )
            value = entry[1]
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise FetchValidationError(
                    f"{url}: 'series' contains a non-numeric value at "
                    f"{entry[0]}: {value!r}"
                )

    return validate


class SmardClient(BaseClient):
    """Client for the SMARD chart_data API."""

    def __init__(
        self,
        *,
        settings: FetchSettings | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if settings is not None:
            kwargs["settings"] = settings
        if clock is not None:
            kwargs["clock"] = clock
        if sleep is not None:
            kwargs["sleep"] = sleep
        super().__init__(base_url=_API_BASE_URL, source=SOURCE, **kwargs)

    def fetch_series(
        self,
        metric: str,
        region: str,
        start: datetime,
        end: datetime,
    ) -> Iterable[MeasurementRecord]:
        """Fetch ``[start, end)`` as hourly records (PT1H, contract units)."""
        self._validate_request(metric, region, start, end)
        window_start, window_end = clamp_to_validity(region, start, end)
        if window_start >= window_end:
            return iter(())

        filter_id = SOURCE_METRIC_MAP[SOURCE][metric]
        region_code = SOURCE_REGION_MAP[SOURCE][region]
        index = self._get_index(filter_id, region_code)
        if index is None:
            return iter(())

        rows: dict[datetime, MeasurementRecord] = {}
        for bucket_ms, bucket_end_ms in self._select_buckets(
            index, window_start, window_end
        ):
            for ts, value in self._get_bucket_rows(
                filter_id,
                region_code,
                bucket_ms,
                bucket_end_ms,
                metric=metric,
                window=(window_start, window_end),
            ):
                if ts in rows:
                    raise FetchValidationError(
                        f"{_API_BASE_URL}: duplicate timestamp {ts.isoformat()} "
                        f"for {metric} in overlapping buckets"
                    )
                rows[ts] = MeasurementRecord(
                    ts_utc=ts,
                    region=region,
                    metric=metric,
                    value=value,
                    source=SOURCE,
                    resolution="PT1H",
                )
        return (rows[ts] for ts in sorted(rows))

    def _validate_request(
        self,
        metric: str,
        region: str,
        start: datetime,
        end: datetime,
    ) -> None:
        metric_map = SOURCE_METRIC_MAP[SOURCE]
        if metric not in metric_map:
            raise FetchConfigError(
                f"metric {metric!r} is not covered by source {SOURCE!r}"
            )
        metric_entry = METRICS.get(metric)
        if metric_entry is None:
            raise FetchConfigError(f"unknown metric {metric!r}")
        if metric_entry.derived:
            raise FetchConfigError(
                f"metric {metric!r} is derived; derived metrics are computed "
                "in transform, never fetched"
            )
        region_map = SOURCE_REGION_MAP[SOURCE]
        if region not in region_map:
            raise FetchConfigError(
                f"region {region!r} is not covered by source {SOURCE!r}"
            )
        region_entry = REGIONS.get(region)
        if region_entry is None or region_entry.kind != "bidding_zone":
            raise FetchConfigError(
                f"region {region!r} is not a bidding zone (required by {SOURCE!r})"
            )
        _require_aware(start, "start")
        _require_aware(end, "end")
        if start >= end:
            raise FetchConfigError(
                f"start {start.isoformat()} must be before end {end.isoformat()} "
                "(half-open [start, end))"
            )

    def _bucket_url(self, filter_id: str, region_code: str, ts_ms: int) -> str:
        """Bucket URL with the filterCopy/regionCopy path quirk applied."""
        stem = f"{filter_id}_{region_code}_{_RESOLUTION}_{ts_ms}"
        return f"{_API_BASE_URL}/{filter_id}/{region_code}/{stem}.json"

    def _index_url(self, filter_id: str, region_code: str) -> str:
        return f"{_API_BASE_URL}/{filter_id}/{region_code}/index_{_RESOLUTION}.json"

    def _get_index(self, filter_id: str, region_code: str) -> dict[str, Any] | None:
        url = self._index_url(filter_id, region_code)
        return self.get_json(
            url, validate=_make_index_validator(url), not_found_ok=True
        )

    def _select_buckets(
        self,
        index: dict[str, Any],
        window_start: datetime,
        window_end: datetime,
    ) -> list[tuple[int, int]]:
        """Bucket intervals ``[ts, next_ts)`` intersecting the window.

        The index timestamps themselves are the interval edges (they are
        Berlin-local midnights, so assuming a fixed 7-day length would
        misalign around DST changes); the last bucket falls back to a
        7-day interval.
        """
        timestamps: list[int] = index["timestamps"]
        start_ms = int(window_start.timestamp() * 1000)
        end_ms = int(window_end.timestamp() * 1000)
        selected: list[tuple[int, int]] = []
        for ts_ms, next_ms in itertools.pairwise(timestamps):
            if next_ms <= start_ms or ts_ms >= end_ms:
                continue
            selected.append((ts_ms, next_ms))
        if timestamps:
            last_ms = timestamps[-1]
            last_end_ms = last_ms + _WEEK_MS
            if last_end_ms > start_ms and last_ms < end_ms:
                selected.append((last_ms, last_end_ms))
        return selected

    def _get_bucket_rows(
        self,
        filter_id: str,
        region_code: str,
        bucket_ms: int,
        bucket_end_ms: int,
        *,
        metric: str,
        window: tuple[datetime, datetime],
    ) -> list[tuple[datetime, float]]:
        """Fetch one bucket and return its in-window non-null rows."""
        url = self._bucket_url(filter_id, region_code, bucket_ms)
        try:
            data = self.get_json(url, validate=_make_bucket_validator(url))
        except FetchHTTPError as exc:
            if exc.status_code == 404:
                raise FetchValidationError(
                    f"{url}: index lists bucket {bucket_ms}, but it is missing (404)"
                ) from exc
            raise

        window_start, window_end = window
        rows: list[tuple[datetime, float]] = []
        for ts_ms, value in data["series"]:
            ts = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
            if not (window_start <= ts < window_end):
                continue
            if value is None:
                continue  # missing value -> missing row, no NULL rows
            number = float(value)
            if not math.isfinite(number):
                raise FetchValidationError(
                    f"{url}: non-finite value for {metric} at {ts.isoformat()}"
                )
            rows.append((ts, number))
        return rows


__all__ = ["SmardClient"]
