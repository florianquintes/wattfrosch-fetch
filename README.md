# wattfrosch-fetch

Fetch external weather and energy data and normalize it to the wattfrosch data
contract (`MeasurementRecord`).

Part of the [wattfrosch](https://github.com/florianquintes/wattfrosch) module
family.

## What is this?

This is the first stage of the wattfrosch pipeline. It knows all external APIs
and translates their differing formats into the shared data contract from
[wattfrosch-contracts](https://github.com/florianquintes/wattfrosch-contracts).
All other wattfrosch modules depend only on that contract, not on this module.

Implemented sources:

| Source | What it provides | Notes |
|---|---|---|
| `smard` | generation, load, prices (SMARD / Bundesnetzagentur) | cannot provide `generation.oil` |
| `energy-charts` | generation, load, prices (Fraunhofer ISE) | 15-minute steps are averaged to hourly |
| `open-meteo` | weather (no API key) | archive (ERA5) + forecast split |

Features:

- One client per source with a uniform signature:
  `fetch_series(metric, region, start, end) -> Iterable[MeasurementRecord]`
  (dispatch via `create_client()` / the `CLIENTS` registry)
- Retries with exponential backoff and `Retry-After` handling (tenacity),
  per-source sliding-window rate limits
- Unit conversions into the contract units (°C → K, km/h → m/s)
- Region validity clamping (SCD-2 windows: DE-AT-LU → DE-LU on 2018-10-01)
- Export to parquet/csv/ndjson (pyarrow) via CLI or `write_records()`
- Strict API monitoring: contracts is deliberately lenient, fetch reports
  deviations (`FetchValidationError`)
- Response caching is OFF in v1 (hishel backend planned later;
  `cache_enabled=True` raises `FetchConfigError`)

Package structure:

```
src/wattfrosch_fetch/
  contracts.py     # single import point for all contract symbols
  version.py       # import-time guard: assert_contract_version(CONTRACT_VERSION)
  http.py          # base layer: retry, backoff, rate limit (cache off, v1)
  clients/         # one module per source + CLIENTS registry + create_client()
  validity.py      # clamp_to_validity (region validity windows)
  conversions.py   # celsius_to_kelvin, kilometers_per_hour_to_meters_per_second
  normalize.py     # empty stub — normalization lives in the client modules
  export.py        # write_records: parquet/csv/ndjson via pyarrow
  cli.py
```

## Installation

Requires Python 3.13+ (`requires-python >= 3.13`). This repo depends on the
sibling repo `wattfrosch-contracts` as an editable path source
(`[tool.uv.sources]`), so it must exist next to this repo or `uv sync` fails:

```sh
git clone https://github.com/florianquintes/wattfrosch-contracts
git clone https://github.com/florianquintes/wattfrosch-fetch
cd wattfrosch-fetch
uv sync --locked
```

The contract version is checked at import time: `version.py` compares
`CONTRACT_VERSION` against `CONTRACT_VERSION_SUPPORTED` ("0.1", major.minor
comparison; patches are accepted) and raises `ContractVersionError` on
mismatch — fail fast before any request is made.

## Usage

CLI (export defaults to parquet; `--metric` is repeatable):

```sh
uv run wattfrosch-fetch --source smard --metric consumption.load \
    --region DE-LU --from 2026-10-10 --to 2026-10-12 \
    --out load.parquet
```

CLI flags (uniform across all wattfrosch modules):

| Flag | Meaning |
|---|---|
| `--source` | required: `smard` \| `energy-charts` \| `open-meteo` |
| `--metric` | required, repeatable; internal keys from `METRICS` (derived metrics are rejected) |
| `--region` | required; internal codes from `REGIONS` |
| `--from` / `--to` | ISO 8601; date-only counts as UTC; half-open `[from, to)` |
| `--out` | target file; omitted → stdout (pipe-friendly, summary on stderr) |
| `--format` | `parquet` \| `csv` \| `json` (NDJSON), default `parquet` |

Output columns follow the contract field order:
`ts_utc, region, metric, value, source, resolution`; values are in contract
units (e.g. kelvin for temperature). A `--out`/`--format` extension mismatch
is resolved in favor of `--format` (with a stderr warning).

Exit codes: `0` success (an **empty result is also success**, with a stderr
warning), `1` configuration/usage error (unknown source/metric/region,
metric not covered by the source, derived metric requested, invalid format),
`2` HTTP/transport error after retries are exhausted, `3` response validation
error (API deviation).

Library:

```python
from datetime import datetime, UTC

from wattfrosch_fetch.clients import create_client

client = create_client("smard")
records = client.fetch_series(
    "consumption.load", "DE-LU",
    datetime(2026, 10, 10, tzinfo=UTC), datetime(2026, 10, 12, tzinfo=UTC),
)
for record in records:
    print(record.ts_utc, record.metric, record.value)

client.close()
```

All sources are equal — `--source` always decides explicitly; there is no
preferred source and no silent fallback. If a source fails, the whole run
fails. Repeated runs are idempotent (the store deduplicates via
`ON CONFLICT`).

## Contributing

Tooling is uv (Python 3.13). CI order matters: lint → typecheck → test.

```bash
uv sync --locked                # install deps into .venv
uv run ruff check .             # lint
uv run ruff format --check .    # formatting gate
uv run pyrefly check            # type check (pyrefly, not mypy)
uv run pytest                   # tests
```

Conventions:

- Type checking is **pyrefly** (always use pyrefly, not mypy).
- Tests mock HTTP with `respx` (dev dependency) — never hit real external
  APIs in tests (the test suite blocks real sockets). Coverage
  `fail_under = 100`.
- CLI flags are uniform across all wattfrosch modules; do not deviate.
- Never hardcode metric keys or region codes — use `SOURCE_METRIC_MAP` /
  `SOURCE_REGION_MAP` from wattfrosch-contracts (see the mappings section).
- Contract rules fetch must uphold: no NULL rows (a missing value is a
  missing row), half-open intervals `[start, end)`, `PT1H` only, temperature
  in kelvin, wind speed in m/s, never fetch derived metrics, no NaN/Inf
  values, region validity windows respected, `generation.oil` only via
  energy-charts.

Open points:

- Dockerfile (the path dependency on wattfrosch-contracts needs an image
  solution)
- `normalize.py` is an empty stub; normalization lives in the client modules
  (deliberate decision — remove or fill the stub later)

## Mappings

fetch translates internal metric keys and region codes to the source-specific
external keys using `SOURCE_METRIC_MAP` and `SOURCE_REGION_MAP` from
wattfrosch-contracts — the full tables live in the
[contracts README](https://github.com/florianquintes/wattfrosch-contracts#mappings).
How each client uses them:

### smard

- Metric keys map to numeric filter IDs from the SMARD API readme
  (e.g. `generation.solar` → `4068`, `consumption.load` → `410`,
  `price.day_ahead` → `4169`); `generation.oil` has no documented filter and
  is rejected as a configuration error.
- Region codes are used as-is (`DE-AT-LU`, `DE-LU`).
- Data access is two-step implicit pagination: `index_hour.json` lists week
  buckets, then one request per bucket. Bucket payload is
  `{"meta_data": ..., "series": [[ts_ms, value|null], ...]}` — `null` values
  become missing rows. Index 404 or an empty timestamp list → empty result;
  a 404 on a listed bucket or duplicate timestamps are API deviations.

### energy-charts

- Metric keys map to themselves (identity); a module-level guard checks at
  import time that every mapped series name exists in `SOURCE_METRIC_MAP`.
  Multiple API series can feed one metric (`generation.hydro` = run-of-river
  + water reservoir, summed).
- Region codes map to lowercase zone ids (`DE-LU` → `de-lu`); the `/price`
  endpoint requires the zone UPPERCASE (`bzn=DE-LU`).
- The API delivers 15-minute steps — fetch averages per UTC hour onto `PT1H`;
  incomplete hours are skipped entirely (no partial means). In-run
  memoization shares the last response per (URL, zone, window) across metrics
  to stay under the 2 requests/minute rate limit.
- `not_found_ok=True`: a 404 ("no data in period") yields an empty result.

### open-meteo

- Weather metrics map to the raw API variable names (`temperature_2m`,
  `wind_speed_10m`, `precipitation`, `cloud_cover`).
- Region codes map to `"lat,lon"` strings (`DE-BERLIN` → `52.52,13.405`).
- The window is split into two requests: archive (ERA5) for
  `[start, today - archive_lag_days)` and the forecast endpoint for the rest
  (up to `today + forecast_horizon_days`, default 16 days), with a 1-day
  overlap where the archive run wins on duplicate timestamps.
- Unit conversions applied: °C → K (temperature), km/h → m/s (wind speed).

### Rate limits (per client instance, sliding window)

| Source | Default limit |
|---|---|
| open-meteo | 60 requests / minute |
| energy-charts | 2 requests / minute |
| smard | 30 requests / minute |

Overridable via the client constructor (`rate_limit=`); unknown sources
without an override raise `FetchConfigError`. Retries back off exponentially
with jitter; a 429 response honors the `Retry-After` header.

## License

See [LICENSE](LICENSE).
