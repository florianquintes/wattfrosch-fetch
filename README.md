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
  weather.temperature_2m, km/h -> m/s für weather.wind_speed_10m;
  Pflicht laut Contract "Responsibility boundaries"; Umsetzungen in
  conversions.py)
- Überspringen von derived-Metriken (die werden nie gefetcht, nur in transform
  berechnet)
- Retry/Backoff, Rate-Limiting; Response-Caching ist in v1 AUS
  (Entscheidung Milestone 0; hishel-Backend später geplant,
  `cache_enabled=True` ist ein Konfigurationsfehler)
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
- Temperatur in Kelvin liefern (Quelle liefert °C -> Konversion im Fetch);
  Windgeschwindigkeit in m/s (Open-Meteo liefert km/h -> Konversion im Fetch).
- SMARD kann generation.oil nicht liefern (kein dokumentierter Filter in der
  SMARD-API); die Filter-IDs für alle übrigen Energie-Metriken stehen fertig
  in SOURCE_METRIC_MAP bzw. SMARD_readme.md in wattfrosch-contracts.
- Externe Quellen werden streng überwacht (contracts ist bewusst lenient,
  Abweichungen der API meldet fetch).

## Tech (Vorschlag)
httpx (sync), pydantic, pydantic-settings (Env-Präfix
`WATTFROSCH_FETCH_`), tenacity, respx (nur Tests), uv.
Caching: v1 aus, hishel als späteres Backend geplant.
CLI: argparse (User-Entscheid gegen typer — keine neue Runtime-Dependency);
Schalternamen müssen zu den anderen Modulen passen (`--metric` ist
wiederholbar; Single-Value-Aufrufe sind eine Teilmenge).

## Struktur (real)
src/wattfrosch_fetch/
  contracts.py     # einzige Import-Stelle für alle Contract-Symbole
  version.py       # Import-Zeit-Guard: assert_contract_version(CONTRACT_VERSION)
  http.py          # Basis-Layer: Retry, Backoff, Rate-Limit (Cache aus, v1)
  clients/         # ein Modul je Datenquelle + CLIENTS-Registry + create_client()
  validity.py      # clamp_to_validity (Region-Gültigkeitsfenster)
  conversions.py   # celsius_to_kelvin, kilometers_per_hour_to_meters_per_second
  normalize.py     # leerer Stub — Normalisierung lebt in den Client-Modulen
  export.py        # write_records: parquet/csv/ndjson via pyarrow
  cli.py
tests/
pyproject.toml

## Quickstart
Abhängigkeit: wattfrosch-contracts >= 0.1.0 (getaggt und auf PyPI/wheel
verfügbar). Contract-Version-Guard: beim Import prüft version.py die
CONTRACT_VERSION gegen `CONTRACT_VERSION_SUPPORTED` ("0.1", Major.Minor-
Vergleich; Patch wird akzeptiert) und wirft sonst `ContractVersionError`
(Fail fast beim Import).

```sh
uv sync
uv run pytest
uv run wattfrosch-fetch --help

# Beispiel: Stromlast für DE-LU, zwei Tage (halb-offen [from, to))
uv run wattfrosch-fetch --source smard --metric consumption.load \
    --region DE-LU --from 2026-10-10 --to 2026-10-12

# Mehrere Metriken in einem Lauf (--metric ist wiederholbar)
uv run wattfrosch-fetch --source energy-charts \
    --metric generation.solar --metric price.day_ahead \
    --region DE-LU --from 2026-10-10 --to 2026-10-12

# Export: --out gibt die Zieldatei, --format das Format (default: parquet)
uv run wattfrosch-fetch --source smard --metric consumption.load \
    --region DE-LU --from 2026-10-10 --to 2026-10-12 \
    --out out/load.parquet --format parquet

# Ohne --out landen die Daten auf stdout (pipe-freundlich; Zusammenfassung
# bleibt auf stderr) — z. B. NDJSON weiterverarbeiten:
uv run wattfrosch-fetch --source smard --metric consumption.load \
    --region DE-LU --from 2026-10-10 --to 2026-10-12 \
    --format json | jq -c '.'
```

Flag-Namen sind über alle wattfrosch-Module einheitlich: `--source`,
`--metric` (wiederholbar), `--region`, `--from`, `--to` (ISO 8601, Datum
ohne Zeit gilt als UTC; halb-offenes Intervall `[from, to)`), `--out`,
`--format` (parquet|csv|json). Ohne `--out` geht der Export an stdout
(parquet binär via stdout-Buffer). Die Spalten folgen der Contract-
Feldreihenfolge (`ts_utc, region, metric, value, source, resolution`);
Werte stehen in Vertragseinheiten (z. B. Kelvin für Temperatur). Ein leeres
Ergebnis ist kein Fehler (Exit 0 + Warnung); mit `--out` wird dann eine
leere Datei (nur Schema) geschrieben. Bei Endungs-Mismatch zwischen
`--out` und `--format` gewinnt `--format` (Warnung auf stderr).

## Fertig, wenn
- Clients liefern valide MeasurementRecords
- CLI exportiert nach Parquet/CSV/JSON
- Tests grün (mit gemockten Antworten, 100 % Coverage)
- Dockerfile baut (OFFEN — noch nicht umgesetzt; Pfad-Abhängigkeit auf
  wattfrosch-contracts funktioniert in einem Image so nicht und braucht
  eine eigene Lösung)

## Quellenrollen und Fehlerverhalten
Alle Quellen sind gleichrangig: `--source` entscheidet immer explizit; es
gibt keine bevorzugte Quelle und kein stilles Ausweichen auf eine andere
Quelle. Eine Metrik/Region-Kombination, die die gewählte Quelle nicht
abdeckt, ist ein Konfigurationsfehler. `generation.oil` ist in v1
ausschließlich über energy-charts verfügbar — SMARD hat keinen dokumentierten
Filter dafür, und fetch erfindet keinen. Ein Alternativ-Provider wäre eine
neue Quelle im Contract (SOURCES/SOURCE_METRIC_MAP), kein Fetch-Workaround.

Fehler-/Teilausfall-Verhalten: Schlägt die Quelle fehl, schlägt der ganze
Lauf fehl — kein Überspringen mit Warnung. Lücken füllt transform;
Wiederholungsläufe sind idempotent (der Store dedupliziert per
`ON CONFLICT`).

Exit-Codes des CLI:
- 0: Erfolg
- 1: Konfigurations-/Usage-Fehler (unbekannte Quelle/Metrik/Region, Metrik
  nicht durch die Quelle abgedeckt, derived-Metrik angefragt, Region im
  Zeitfenster ungültig, ungültiges Format)
- 2: HTTP-/Transportfehler nach ausgeschöpften Retries (4xx/5xx, Timeout,
  Rate-Limit)
- 3: Response-Validierungsfehler (API-Abweichung, strikter
  Validierungs-Hook)
- Ein leeres Ergebnis ist kein Fehler: Exit 0 mit Warnung auf stderr
  (Lücken füllt transform).

## Paginierung und Fensteraufteilung
Die einheitliche Abrufsignatur bleibt `fetch_series(metric, region, start,
end)`; das Batching mehrerer Metriken pro HTTP-Request passiert intern im
Client.

- **Open-Meteo**: keine Paginierung, aber Fensteraufteilung in zwei Requests:
  Archiv (ERA5, `archive-api.open-meteo.com`, `start_date`/`end_date`) für
  `[start, heute - archive_lag_days)` und Forecast-Endpoint
  (`api.open-meteo.com`) für den Rest (bis `heute + forecast_horizon_days`,
  default 16 Tage). Überlappung 1 Tag; bei Duplikaten gewinnt der Archiv-Lauf
  (spiegelt die `ON CONFLICT`-Idempotenz des Stores). Alle angefragten
  stündlichen Variablen werden je Call gebündelt (`hourly=a,b,c`).
- **Energy-Charts**: keine Paginierung. Ein Request pro (Region, Fenster)
  über `start`/`end` (ISO 8601 oder Unix-Sekunden); `/public_power` liefert
  alle Erzeugungs-/Verbrauchsarten in einem Call und wird intern je Metrik
  aufgeteilt. Rate-Limit: 2 Requests/Minute pro Client-IP und Endpunkt
  (`/price` Burst 2); bei 429 wird `Retry-After` berücksichtigt. Kein
  clientseitiges Zerlegen des Fensters in v1. Achtung: die API liefert
  15-Minuten-Schritte (`/public_power` immer, `/price` aktuell; historische
  `/price`-Fenster stündlich) — fetch mittelt je UTC-Stunde auf PT1H;
  unvollständige Stunden (None-Schritt oder Gap) werden übersprungen.
  Mehrere Metriken teilen sich per In-Run-Memoization die letzte Response
  je (URL, Zone, Fenster) — bewusst kein HTTP-Caching, aber nötig, um unter
  dem 2-Requests/Minute-Limit mehrere Metriken in einem Lauf zu holen.
  Quirks: `/price` verlangt die Zone UPPERCASE (`bzn=DE-LU`), die
  Gültigkeitsfenster der Regionen (DE-AT-LU/DE-LU ab 2018-10-01) erzwingt
  die API nicht — der Client klemmt das Fenster an `REGIONS`.
- **SMARD**: implizite Paginierung in zwei Schritten — erst
  `index_{resolution}.json` je (Filter, Region) abrufen, dann die
  Wochentimestamps wählen, die `[start, end)` schneiden, und je Bucket einen
  Request stellen. Pfad-Quirk: `filterCopy`/`regionCopy` müssen die
  Pfad-Parameter wiederholen. Quirks: Bucket-Grenzen sind Berlin-lokale
  Mitternächte (Epoch-ms bleiben absolut → für die Zeilen irrelevant);
  Bucket-Payload ist `{"meta_data": …, "series": [[ts_ms, value|null], …]}`
  (Paare, nicht Parallel-Arrays); `null`-Werte werden zu fehlenden Zeilen;
  Einheiten sind bereits Vertragseinheiten (MW, EUR/MWh). Index-404 oder
  leere Timestamp-Liste → leeres Ergebnis; ein 404 auf einem im Index
  gelisteten Bucket oder doppelte Timestamps über Buckets sind
  API-Abweichungen → `FetchValidationError`. Die Gültigkeitsfenster der
  Regionen erzwingt SMARD selbst, der Client klemmt trotzdem an `REGIONS`
  (gemeinsamer Helper `wattfrosch_fetch.validity`).

Ein leeres Ergebnis (z. B. Energy-Charts 404 „keine Daten im Zeitraum") ist
kein Fehler: Exit 0 mit Warnung auf stderr.

## Offene Punkte (mit OpenCode erarbeiten)
- Dockerfile (Pfad-Abhängigkeit auf wattfrosch-contracts braucht eine
  Image-Lösung)
- normalize.py ist ein leerer Stub; Normalisierung lebt in den Client-Modulen
  (bewusst so entschieden — Stub später entfernen oder füllen)
