"""Open-Meteo client: archive + forecast weather data (Milestone 2).

Two endpoints under one source name (Milestone 0 decision "Archive vs.
forecast window partition"):

- archive (ERA5): ``[start, today - archive_lag_days)`` — historical
  truth;
- forecast: ``[today - archive_lag_days, now + forecast_horizon_days)`` —
  fills the archive lag and deliberately reaches into the future.

Windows overlap slightly at the boundary (``_OVERLAP_DAYS``); the archive
call runs last within a fetch run and wins on duplicate timestamps,
mirroring the store's ``ON CONFLICT`` overwrite semantics.

Strictness: metric/region must be covered by ``SOURCE_METRIC_MAP`` /
``SOURCE_REGION_MAP``; derived metrics are refused (never fetched); naive
datetimes are refused. Missing values become missing rows (no NULL rows);
non-finite values and API deviations raise :class:`FetchValidationError`.
``timezone=UTC`` is set explicitly — ``ts_utc`` must be UTC.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from wattfrosch_fetch.contracts import (
    METRICS,
    REGIONS,
    SOURCE_METRIC_MAP,
    SOURCE_REGION_MAP,
    MeasurementRecord,
)
from wattfrosch_fetch.conversions import (
    celsius_to_kelvin,
    kilometers_per_hour_to_meters_per_second,
)
from wattfrosch_fetch.http import (
    BaseClient,
    FetchConfigError,
    FetchSettings,
    FetchValidationError,
)

SOURCE: Final[str] = "open-meteo"

_API_BASE_URL: Final[str] = "https://api.open-meteo.com"
_ARCHIVE_URL: Final[str] = "https://archive-api.open-meteo.com/v1/archive"
_FORECAST_URL: Final[str] = "https://api.open-meteo.com/v1/forecast"

_HOUR: Final[timedelta] = timedelta(hours=1)
_OVERLAP_DAYS: Final[int] = 1

_TIMEZONE_PARAM: Final[str] = "UTC"
_ARCHIVE_MODEL: Final[str] = "era5"

# metric -> conversion into the contract unit; temperature ships in °C and
# must be delivered in K (future unit changes get new entries/functions in
# wattfrosch_fetch.conversions).
_CONVERSIONS: Final[dict[str, Callable[[float], float]]] = {
    "weather.temperature_2m": celsius_to_kelvin,
    "weather.wind_speed_10m": kilometers_per_hour_to_meters_per_second,
}


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None:
        raise FetchConfigError(f"{name} must be timezone-aware (UTC expected)")


def _make_hourly_validator(variable: str, url: str) -> Callable[[Any], None]:
    """Strict response shape check: deviations raise FetchValidationError."""

    def validate(data: Any) -> None:
        if not isinstance(data, dict):
            raise FetchValidationError(f"{url}: response is not a JSON object")
        if data.get("error"):
            raise FetchValidationError(
                f"{url}: open-meteo API error: {data.get('reason', 'unknown')}"
            )
        hourly = data.get("hourly")
        if not isinstance(hourly, dict):
            raise FetchValidationError(f"{url}: missing 'hourly' object")
        times = hourly.get("time")
        if not isinstance(times, list):
            raise FetchValidationError(f"{url}: missing 'hourly.time' array")
        values = hourly.get(variable)
        if not isinstance(values, list):
            raise FetchValidationError(f"{url}: missing 'hourly.{variable}' array")
        if len(values) != len(times):
            raise FetchValidationError(
                f"{url}: 'hourly.{variable}' has {len(values)} values "
                f"but 'hourly.time' has {len(times)} entries"
            )

    return validate


class OpenMeteoClient(BaseClient):
    """Client for the Open-Meteo archive and forecast APIs."""

    def __init__(
        self,
        *,
        settings: FetchSettings | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if settings is not None:
            kwargs["settings"] = settings
        if clock is not None:
            kwargs["clock"] = clock
        if sleep is not None:
            kwargs["sleep"] = sleep
        super().__init__(base_url=_API_BASE_URL, source=SOURCE, **kwargs)
        self._now = now_fn if now_fn is not None else (lambda: datetime.now(UTC))

    def fetch_series(
        self,
        metric: str,
        region: str,
        start: datetime,
        end: datetime,
    ) -> Iterable[MeasurementRecord]:
        """Fetch ``[start, end)`` as hourly records, archive winning overlaps."""
        variable = self._validate_request(metric, region, start, end)
        latitude, longitude = SOURCE_REGION_MAP[SOURCE][region].split(",", maxsplit=1)

        now = self._now()
        boundary = datetime.combine(
            now.date() - timedelta(days=self._settings.archive_lag_days),
            datetime.min.time(),
            tzinfo=UTC,
        )
        effective_end = min(
            end, now + timedelta(days=self._settings.forecast_horizon_days)
        )
        archive_end = boundary + timedelta(days=_OVERLAP_DAYS)

        # One record per timestamp; forecast first, archive last — archive
        # wins on the boundary overlap (mirrors store ON CONFLICT).
        records: dict[datetime, MeasurementRecord] = {}

        forecast_start = max(start, boundary)
        if forecast_start < effective_end:
            self._collect(
                records,
                url=_FORECAST_URL,
                metric=metric,
                region=region,
                latitude=latitude,
                longitude=longitude,
                variable=variable,
                window=(forecast_start, effective_end),
            )

        archive_window_end = min(end, archive_end)
        if start < archive_window_end:
            self._collect(
                records,
                url=_ARCHIVE_URL,
                metric=metric,
                region=region,
                latitude=latitude,
                longitude=longitude,
                variable=variable,
                window=(start, archive_window_end),
            )

        return (records[ts] for ts in sorted(records))

    def _validate_request(
        self,
        metric: str,
        region: str,
        start: datetime,
        end: datetime,
    ) -> str:
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
        if region_entry is None or region_entry.kind != "weather_point":
            raise FetchConfigError(
                f"region {region!r} is not a weather point (required by {SOURCE!r})"
            )
        _require_aware(start, "start")
        _require_aware(end, "end")
        if start >= end:
            raise FetchConfigError(
                f"start {start.isoformat()} must be before end {end.isoformat()} "
                "(half-open [start, end))"
            )
        return metric_map[metric]

    def _collect(
        self,
        records: dict[datetime, MeasurementRecord],
        *,
        url: str,
        metric: str,
        region: str,
        latitude: str,
        longitude: str,
        variable: str,
        window: tuple[datetime, datetime],
    ) -> None:
        window_start, window_end = window
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": variable,
            "timezone": _TIMEZONE_PARAM,
            "start_date": window_start.date().isoformat(),
            "end_date": max(
                (window_end - _HOUR).date(),
                window_start.date(),
            ).isoformat(),
        }
        if url == _ARCHIVE_URL:
            params["models"] = _ARCHIVE_MODEL
        data = self.get_json(
            url, params=params, validate=_make_hourly_validator(variable, url)
        )
        hourly = data["hourly"]
        conversion = _CONVERSIONS.get(metric)
        for ts_text, value in zip(hourly["time"], hourly[variable], strict=True):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise FetchValidationError(
                    f"{url}: non-numeric value for {metric} at {ts_text}: {value!r}"
                )
            number = float(value)
            if not math.isfinite(number):
                raise FetchValidationError(
                    f"{url}: non-finite value for {metric} at {ts_text}"
                )
            ts = datetime.fromisoformat(ts_text)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if not (window_start <= ts < window_end):
                continue
            converted = conversion(number) if conversion is not None else number
            records[ts] = MeasurementRecord(
                ts_utc=ts,
                region=region,
                metric=metric,
                value=converted,
                source=SOURCE,
                resolution="PT1H",
            )


__all__ = ["OpenMeteoClient"]
