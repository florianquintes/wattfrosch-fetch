# wattfrosch-fetch

Erste Stufe des wattfrosch-Baukastens: Daten von externen Wetter- und
Energie-Quellen holen und in einem einheitlichen, normalisierten Format
bereitstellen.

## Rolle im System
Kennt alle externen Schnittstellen (Application Programming Interfaces, APIs)
und übersetzt ihre unterschiedlichen Formate in den gemeinsamen Daten-Contract
aus wattfrosch-contracts. Alle anderen Module hängen nur an diesem Contract,
nicht an diesem Modul.

## In Scope
- HTTP-Clients je Datenquelle
- Normalisierung auf MeasurementRecord (aus wattfrosch-contracts)
- Einheiten-Konversion in die Metrik-Einheit (z.B. °C -> K für
  weather.temperature_2m; Pflicht laut Contract "Responsibility boundaries")
- Überspringen von derived-Metriken (die werden nie gefetcht, nur in transform
  berechnet)
- Retry/Backoff, Rate-Limiting, Response-Caching
- CLI zum Export (Parquet, CSV, JSON)
- Nutzung von SOURCE_METRIC_MAP (interne Metrik -> externer Schlüssel) und
  SOURCE_REGION_MAP (interne Region -> externer Code) aus wattfrosch-contracts

## Out of Scope
- Datenbank, Transformation/Analytik, Orchestrierung
- Definition des Contracts (liegt in wattfrosch-contracts)

## Schnittstellen
- Nutzt: wattfrosch-contracts (MeasurementRecord, METRICS, REGIONS, SOURCES,
  SOURCE_METRIC_MAP, SOURCE_REGION_MAP, CONTRACT_VERSION)
- Bietet: je einen Client pro Quelle mit einheitlicher Abrufmethode
  (fetch_series(metric, region, start, end) -> Iterable[MeasurementRecord])
- CLI-Schalter (einheitlich über alle Module): --source, --metric, --region,
  --from, --to, --out, --format

## Contract-Regeln, die fetch einhält
- Keine NULL-Zeilen: fehlende Werte sind fehlende Zeilen (Lücken füllt
  später transform).
- Halb-offene Intervalle [start, end); ts_utc ist Intervallstart,
  resolution ist v1 nur "PT1H" (stündlich).
- Region nur liefern, wenn sie zum Zeitpunkt ts_utc gültig ist (Bidding-Zone-
  Historie DE-AT-LU -> DE-LU ab 2018-10-01 beachten; "AT" allein ist nie gültig).
- Keine NULL-Werte, keine NaN/Inf-Werte.
- Temperatur in Kelvin liefern (Quelle liefert °C -> Konversion im Fetch).
- SMARD kann generation.oil nicht liefern (kein dokumentierter Filter in der
  SMARD-API); die Filter-IDs für alle übrigen Energie-Metriken stehen fertig
  in SOURCE_METRIC_MAP bzw. SMARD_readme.md in wattfrosch-contracts.
- Externe Quellen werden streng überwacht (contracts ist bewusst lenient,
  Abweichungen der API meldet fetch).

## Tech (Vorschlag)
httpx, pydantic, tenacity, hishel, respx, uv
CLI: argparse (oder typer) - Schalternamen müssen zu den anderen Modulen passen.

## Struktur (Vorschlag)
src/wattfrosch_fetch/
  http.py          # Basis-Layer: Retry, Backoff, Rate-Limit, Cache
  clients/         # ein Modul je Datenquelle
  normalize.py     # Rohdaten -> MeasurementRecord
  cli.py
tests/
Dockerfile
pyproject.toml

## Quickstart (Platzhalter)
Abhängigkeit: wattfrosch-contracts >= 0.1.0 (getaggt und auf PyPI/wheel
verfügbar; CONTRACT_VERSION kann für Kompatibilitätsprüfung importiert werden).

uv sync
uv run pytest
uv run wattfrosch-fetch --help

## Fertig, wenn
- Clients liefern valide MeasurementRecords
- CLI exportiert nach Parquet/CSV/JSON
- Tests grün (mit gemockten Antworten)
- Dockerfile baut

## Offene Punkte (mit OpenCode erarbeiten)
- Welche Quellen zuerst, welche optional?
- Caching-Strategie?
- Fehler-/Teilausfall-Verhalten pro Quelle?
- Wie oft sind Antworten paginiert, und wie wird paginiert?
- Umgang mit generation.oil: gar nicht anbieten (smard-Lücke) oder später über
  eine weitere Quelle beziehen?
