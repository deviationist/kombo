# KomBo — Oslo kommunes boliger på kart

A two-step pipeline that maps **Oslo kommune's municipal housing** — the places
the city owns where people actually live.

```
XLSX ──(geocode.py)──> eiendommer.geojson ──(index.html)──> map in browser
```

## Scope: housing only

The source spreadsheet lists all 7,022 municipally-owned properties across four
agencies, but most of that is land and road parcels. KomBo filters to the
housing stock via `INCLUDE_EIER` in `geocode.py`:

| Eier / fester | Antall | In scope? |
|---|---|---|
| **Boligbygg Oslo KF** | **528** | ✅ municipal housing (default) |
| Oslobygg KF | 795 | public buildings — schools, offices, ~5 care homes |
| Eiendoms- og byfornyelsesetaten | 5,621 | land + road parcels |
| Oslo Havn KF | 78 | harbour |

The 528 Boligbygg rows dedupe to **356 unique (gnr, bnr) pairs** and **91% have
a registered address**, so geocoding runs in about a minute with near-complete
coverage. (199 rows are *eierseksjoner* — individual flats — that share a
building's gnr/bnr, so they map to the same point. Fine for a coverage view.)

To widen the net to municipal care/senior homes (gamlehjem, eldresenter,
sykehjem, trygdebolig — a handful under Oslobygg KF), add the agency to
`INCLUDE_EIER`. To map the entire register instead, set `INCLUDE_EIER = []`.

## Why geocoding is needed

There are no coordinates in the file. What every row *does* have is a clean
**matrikkelnummer** in the `Eiendom` column:

```
0301 - 10 / 68 / 0 / 0
KNR    GNR  BNR FNR SNR     (KNR 0301 = Oslo)
```

`geocode.py` resolves `(gnr, bnr)` to lat/lon using Geonorge's open address API
(no key, returns WGS84):

```
https://ws.geonorge.no/adresser/v1/sok?kommunenummer=0301&gardsnummer=10&bruksnummer=68&utkoordsys=4326
```

It deduplicates 7 022 rows down to **6 636 unique (gnr, bnr) pairs**, caches
every lookup in SQLite (so re-runs are instant and resumable), and writes
`eiendommer.geojson`.

## Run it

Dependencies are managed with Poetry (`pyproject.toml`, non-package mode):

```bash
poetry install
poetry run python geocode.py "Oversikt-over-Oslo-kommunes-eiendommer-mai-2026_nett-2.xlsx"
# → eiendommer.geojson, geocode_cache.sqlite, missing.csv

# then just open the map (any static server; it fetches the geojson):
poetry run python -m http.server 8000   # → http://localhost:8000/index.html
```

`poetry install` generates a `poetry.lock` on first run. To drop into a shell
with the venv active instead of prefixing every command: `poetry shell` (or the
`poetry-plugin-shell` plugin on Poetry 2.x), then run `python geocode.py …`.

`index.html` already renders with a small sample if `eiendommer.geojson` isn't
there yet, so you can see the UI before geocoding finishes.

## Coverage

With the housing filter, ~91% of rows geocode by address — the missing handful
land in `missing.csv`. (Across the *full* register it's only ~28%, because road
land and unregistered land have no address; that's why scoping to housing fixes
the coverage problem rather than needing parcel polygons.) If you do switch to
the full register later and want the road/land parcels too, you'd resolve those
against Kartverket's *"Matrikkelen – Eiendomskart Teig"* WFS keyed on the
matrikkelnummer — the geocoder is structured so a second resolver for the
missing pairs slots in without touching the rest.

## The map (`index.html`)

Single file, no build step, CDN deps (Leaflet + markercluster + leaflet.heat).
Self-hostable behind nginx on xavi.

- **Punkter** — clustered markers, coloured by owning agency
- **Tetthet** — heat layer (where ownership clusters)
- **Areal-vekt** — heat weighted by parcel size (big sites glow brighter — this
  is the "which areas are dominated by municipal land" view)
- Toggle agencies on/off; filter by bydel; live count + total areal (daa) and
  per-agency bars for the current selection

### Swapping the basemap / using Mapbox or MapLibre

It uses the free CARTO dark basemap (no token). To use Mapbox GL instead, that's
a rewrite to `mapbox-gl-js` with a token; for a token-free vector alternative,
MapLibre GL + a free style is the closer drop-in. The GeoJSON output is standard,
so it feeds any of them — Google Maps, Mapbox, MapLibre, Leaflet, or QGIS.
