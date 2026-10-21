# Roadmap - wattfrosch-fetch

Duration: 1 week (5 working days).

## Day 1
- Repo skeleton; dependency on wattfrosch-contracts
- Base HTTP layer: retry/backoff, rate limiting; caching OFF in v1
  (hishel backend planned)

## Day 2
- Open-Meteo client (no API key -> simplest entry point)
- Region via SOURCE_REGION_MAP ("lat,lon" weather-point strings)
- Unit conversions: °C -> K for weather.temperature_2m, km/h -> m/s for
  weather.wind_speed_10m (conversions.py)
- Archive/forecast window split (ERA5 archive + forecast endpoint,
  1-day overlap, archive wins)
- Normalization to MeasurementRecord (no NULL rows, PT1H only)

## Day 3
- Energy-Charts client + normalization
- Keys from SOURCE_METRIC_MAP (same-key assumption) and SOURCE_REGION_MAP
  (lowercase ids); respect region validity windows (DE-AT-LU -> DE-LU boundary)

## Day 4
- SMARD client + normalization (filter IDs from SOURCE_METRIC_MAP /
  SMARD_readme.md in wattfrosch-contracts; region codes identical)
- Note: smard cannot cover generation.oil (no documented filter) -> skip
  that metric for smard

## Day 5
- CLI with uniform flags (--source --metric --region --from --to --out --format)
- Export to parquet/csv/json (pyarrow; export.py)

## Definition of Done
- all three clients return valid records
- unit conversion correct (temperature in K, wind speed in m/s),
  no NULL/NaN/Inf rows
- derived metrics never fetched; half-open intervals; PT1H only
- CLI exports; tests green (mocked responses, 100 % coverage)
- Dockerfile builds — OPEN, not yet implemented

Status: done except Dockerfile.

## Open for detail work
- Dockerfile (path dependency on wattfrosch-contracts needs an image solution)
- caching backend (hishel, later)
