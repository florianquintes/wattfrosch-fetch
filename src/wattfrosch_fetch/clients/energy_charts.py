"""Energy-Charts client: power generation, consumption and prices (Milestone 3).

One source name for two endpoints (Milestone 0 decision "Pagination per
source"): ``/public_power`` (generation/consumption, all production types in
one blob) and ``/price`` (day-ahead prices). No pagination and no archive/
forecast split — the full requested window arrives in one response.

API quirks handled here:

- the API delivers **sub-hourly steps** (15 minutes today; hourly for
  historical ``/price`` windows) while the contract mandates ``PT1H`` —
  sub-hourly steps are averaged per UTC hour (all these metrics have
  ``aggregation="mean"``); an hour is skipped unless every expected step is
  present and finite (no partially averaged values);
- the response **includes** the ``end`` timestamp — the half-open
  ``[start, end)`` filter drops it;
- the API does **not** enforce region validity windows (``de-lu`` serves
  pre-2018 data, ``de-at-lu`` serves current data) — the client clamps the
  window to the ``REGIONS`` validity range and only emits ``ts_utc`` where
  the requested region is valid;
- ``/price`` requires the zone in UPPERCASE (``bzn=DE-LU``), while
  ``/public_power`` uses the lowercase id from ``SOURCE_REGION_MAP``;
- series are addressed by display names (e.g. "Fossil brown coal / lignite"),
  translated via the internal ``_SERIES_NAMES`` table; every target metric
  must be covered by ``SOURCE_METRIC_MAP`` (never hardcoded keys);
  ``generation.hydro`` is the sum of "Hydro Run-of-River" and
  "Hydro water reservoir".

Strictness mirrors the Open-Meteo client: metric/region must be covered by
``SOURCE_METRIC_MAP`` / ``SOURCE_REGION_MAP``; derived metrics are refused;
naive datetimes are refused. ``None`` values become missing rows (no NULL
rows); non-finite values and API deviations raise
:class:`FetchValidationError`. A 404 ("no data for that zone/range") and a
window fully outside the region validity range yield an empty result (exit
0 + warning), not an error.
"""

from __future__ import annotations

import itertools
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
from wattfrosch_fetch.http import (
    BaseClient,
    FetchConfigError,
    FetchSettings,
    FetchValidationError,
)
from wattfrosch_fetch.validity import clamp_to_validity

SOURCE: Final[str] = "energy-charts"

_API_BASE_URL: Final[str] = "https://api.energy-charts.info"
_PUBLIC_POWER_URL: Final[str] = f"{_API_BASE_URL}/public_power"
_PRICE_URL: Final[str] = f"{_API_BASE_URL}/price"

_HOUR: Final[timedelta] = timedelta(hours=1)
_PRICE_UNIT: Final[str] = "EUR / MWh"
_PRICE_METRIC: Final[str] = "price.day_ahead"

# API display name (production_types[].name) -> internal metric key. The
# values must stay covered by SOURCE_METRIC_MAP (checked at import).
# Unmapped series ("Cross border electricity trading", "Geothermal",
# "Fossil coal-derived gas", "Renewable share of …") have no internal key
# and are ignored — never invented.
_SERIES_NAMES: Final[dict[str, str]] = {
    "Biomass": "generation.biomass",
    "Fossil brown coal / lignite": "generation.lignite",
    "Fossil hard coal": "generation.hard_coal",
    "Fossil gas": "generation.gas",
    "Fossil oil": "generation.oil",
    "Nuclear": "generation.nuclear",
    "Hydro Run-of-River": "generation.hydro",
    "Hydro water reservoir": "generation.hydro",
    "Hydro pumped storage": "generation.pumped_storage",
    "Others": "generation.other",
    "Waste": "generation.other",
    "Wind offshore": "generation.wind_offshore",
    "Wind onshore": "generation.wind_onshore",
    "Solar": "generation.solar",
    "Load": "consumption.load",
    "Residual load": "consumption.residual_load",
    "Hydro pumped storage consumption": "consumption.pumped_storage",
}

for _key in set(_SERIES_NAMES.values()):
    if _key not in SOURCE_METRIC_MAP[SOURCE]:
        raise FetchConfigError(
            f"series name maps to metric {_key!r}, which is not covered "
            f"by source {SOURCE!r}"
        )


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None:
        raise FetchConfigError(f"{name} must be timezone-aware (UTC expected)")


def _make_power_validator(url: str) -> Callable[[Any], None]:
    """Strict response shape check: deviations raise FetchValidationError."""

    def validate(data: Any) -> None:
        if not isinstance(data, dict):
            raise FetchValidationError(f"{url}: response is not a JSON object")
        unix = data.get("unix_seconds")
        if not isinstance(unix, list):
            raise FetchValidationError(f"{url}: missing 'unix_seconds' array")
        if not all(
            isinstance(item, int) and not isinstance(item, bool) for item in unix
        ):
            raise FetchValidationError(
                f"{url}: 'unix_seconds' contains non-integer entries"
            )
        production_types = data.get("production_types")
        if not isinstance(production_types, list):
            raise FetchValidationError(f"{url}: missing 'production_types' array")
        for entry in production_types:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise FetchValidationError(
                    f"{url}: 'production_types' entry without string 'name'"
                )
            series = entry.get("data")
            if not isinstance(series, list) or len(series) != len(unix):
                raise FetchValidationError(
                    f"{url}: 'production_types[{entry.get('name')!r}].data' "
                    f"has {len(series) if isinstance(series, list) else 'no'} "
                    f"values but 'unix_seconds' has {len(unix)} entries"
                )

    return validate


def _make_price_validator(url: str) -> Callable[[Any], None]:
    """Strict response shape check: deviations raise FetchValidationError."""

    def validate(data: Any) -> None:
        if not isinstance(data, dict):
            raise FetchValidationError(f"{url}: response is not a JSON object")
        unix = data.get("unix_seconds")
        if not isinstance(unix, list):
            raise FetchValidationError(f"{url}: missing 'unix_seconds' array")
        if not all(
            isinstance(item, int) and not isinstance(item, bool) for item in unix
        ):
            raise FetchValidationError(
                f"{url}: 'unix_seconds' contains non-integer entries"
            )
        prices = data.get("price")
        if not isinstance(prices, list) or len(prices) != len(unix):
            raise FetchValidationError(
                f"{url}: 'price' has "
                f"{len(prices) if isinstance(prices, list) else 'no'} values "
                f"but 'unix_seconds' has {len(unix)} entries"
            )
        if data.get("unit") != _PRICE_UNIT:
            raise FetchValidationError(
                f"{url}: unexpected price unit {data.get('unit')!r} "
                f"(expected {_PRICE_UNIT!r})"
            )

    return validate


class EnergyChartsClient(BaseClient):
    """Client for the Energy-Charts public_power and price APIs."""

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
        # In-run memo: the last response per (url, zone, window). One
        # request per (region, window) even when the caller asks for
        # several metrics — not HTTP caching (that decision stays OFF).
        self._memo_key: tuple[str, str, int, int] | None = None
        self._memo_data: Any = None

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

        external = SOURCE_REGION_MAP[SOURCE][region]
        if metric == _PRICE_METRIC:
            data = self._get_json_memoized(
                _PRICE_URL,
                params={
                    "bzn": external.upper(),
                    "start": int(window_start.timestamp()),
                    "end": int(window_end.timestamp()) - 1,
                },
                validate=_make_price_validator(_PRICE_URL),
            )
            records = (
                {}
                if data is None
                else self._collect_price(
                    data,
                    metric=metric,
                    region=region,
                    window=(window_start, window_end),
                )
            )
        else:
            data = self._get_json_memoized(
                _PUBLIC_POWER_URL,
                params={
                    "zone": external,
                    "start": int(window_start.timestamp()),
                    "end": int(window_end.timestamp()) - 1,
                },
                validate=_make_power_validator(_PUBLIC_POWER_URL),
            )
            records = (
                {}
                if data is None
                else self._collect_power(
                    data,
                    metric=metric,
                    region=region,
                    window=(window_start, window_end),
                )
            )

        return (records[ts] for ts in sorted(records))

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

    def _get_json_memoized(
        self,
        url: str,
        *,
        params: dict[str, Any],
        validate: Callable[[Any], None],
    ) -> Any:
        key = (
            url,
            str(params.get("zone") or params.get("bzn")),
            int(params["start"]),
            int(params["end"]),
        )
        if self._memo_key == key:
            return self._memo_data
        data = self.get_json(url, params=params, validate=validate, not_found_ok=True)
        self._memo_key = key
        self._memo_data = data
        return data

    def _checked_number(
        self,
        url: str,
        value: Any,
        metric: str,
        ts: datetime,
    ) -> float:
        """Strict numeric check: bool/non-numeric/non-finite raise."""
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise FetchValidationError(
                f"{url}: non-numeric value for {metric} at {ts.isoformat()}: {value!r}"
            )
        number = float(value)
        if not math.isfinite(number):
            raise FetchValidationError(
                f"{url}: non-finite value for {metric} at {ts.isoformat()}"
            )
        return number

    def _bucketize(
        self,
        *,
        url: str,
        unix_seconds: list[int],
        values: list[Any],
        metric: str,
        window: tuple[datetime, datetime],
    ) -> dict[datetime, list[float]]:
        """Group values into UTC-hour buckets.

        ``None`` values are skipped (missing rows); bool/non-numeric and
        non-finite values raise :class:`FetchValidationError`. The caller
        checks bucket completeness before averaging.
        """
        window_start, window_end = window
        buckets: dict[datetime, list[float]] = {}
        for ts_seconds, value in zip(unix_seconds, values, strict=True):
            ts = datetime.fromtimestamp(ts_seconds, tz=UTC)
            if not (window_start <= ts < window_end):
                continue
            if value is None:
                continue
            hour = ts.replace(minute=0, second=0, microsecond=0)
            buckets.setdefault(hour, []).append(
                self._checked_number(url, value, metric, ts)
            )
        return buckets

    def _make_records(
        self,
        buckets: dict[datetime, list[float]],
        *,
        expected_per_bucket: int,
        metric: str,
        region: str,
    ) -> dict[datetime, MeasurementRecord]:
        """Average complete buckets into hourly records; skip incomplete ones."""
        records: dict[datetime, MeasurementRecord] = {}
        for hour, values in sorted(buckets.items()):
            if len(values) != expected_per_bucket:
                continue  # incomplete hour -> missing row, no partial mean
            records[hour] = MeasurementRecord(
                ts_utc=hour,
                region=region,
                metric=metric,
                value=sum(values) / len(values),
                source=SOURCE,
                resolution="PT1H",
            )
        return records

    def _step_per_hour(self, unix_seconds: list[int], url: str) -> int:
        """Steps per hour derived from the smallest observed spacing."""
        if len(unix_seconds) < 2:
            return 1
        step = min(
            later - earlier for earlier, later in itertools.pairwise(unix_seconds)
        )
        if step <= 0 or 3600 % step != 0:
            raise FetchValidationError(
                f"{url}: unexpected step size {step}s between timestamps"
            )
        return 3600 // step

    def _collect_power(
        self,
        data: dict[str, Any],
        *,
        metric: str,
        region: str,
        window: tuple[datetime, datetime],
    ) -> dict[datetime, MeasurementRecord]:
        url = _PUBLIC_POWER_URL
        unix_seconds: list[int] = data["unix_seconds"]
        series_by_name = {
            entry["name"]: entry["data"] for entry in data["production_types"]
        }
        expected_names = [name for name, key in _SERIES_NAMES.items() if key == metric]
        if not expected_names:
            return {}  # metric not offered by this API -> empty result
        missing = [name for name in expected_names if name not in series_by_name]
        if missing:
            raise FetchValidationError(
                f"{url}: missing series for {metric}: {', '.join(missing)}"
            )
        steps_per_hour = self._step_per_hour(unix_seconds, url)
        window_start, window_end = window
        buckets: dict[datetime, list[float]] = {}
        for index, ts_seconds in enumerate(unix_seconds):
            ts = datetime.fromtimestamp(ts_seconds, tz=UTC)
            if not (window_start <= ts < window_end):
                continue
            # Sum across all series mapping to the metric at this
            # timestamp (e.g. hydro = run-of-river + reservoir); a None
            # in any series makes the timestamp incomplete.
            total: float | None = None
            for name in expected_names:
                value = series_by_name[name][index]
                if value is None:
                    total = None
                    break
                number = self._checked_number(url, value, metric, ts)
                total = number if total is None else total + number
            if total is None:
                continue  # incomplete timestamp -> its hour stays incomplete
            hour = ts.replace(minute=0, second=0, microsecond=0)
            buckets.setdefault(hour, []).append(total)
        return self._make_records(
            buckets,
            expected_per_bucket=steps_per_hour,
            metric=metric,
            region=region,
        )

    def _collect_price(
        self,
        data: dict[str, Any],
        *,
        metric: str,
        region: str,
        window: tuple[datetime, datetime],
    ) -> dict[datetime, MeasurementRecord]:
        url = _PRICE_URL
        unix_seconds: list[int] = data["unix_seconds"]
        per_hour = self._step_per_hour(unix_seconds, url)
        buckets = self._bucketize(
            url=url,
            unix_seconds=unix_seconds,
            values=data["price"],
            metric=metric,
            window=window,
        )
        return self._make_records(
            buckets,
            expected_per_bucket=per_hour,
            metric=metric,
            region=region,
        )


__all__ = ["EnergyChartsClient"]
