# kombo — Oslo kommunes eiendommer på kart

A two-step pipeline that maps **every property Oslo kommune owns** — across all
four municipal bodies: from rental housing to schools, from road parcels to the
harbour. Live at **<https://kombo.ichiva.no/>**.

```
XLSX ──(geocode.py)──> eiendommer.geojson ──(index.html)──> map in browser
```

## What's in scope

The source spreadsheet (`Oversikt over Oslo kommunes eiendommer per mai 2026`,
re-published every six months by the kommune) lists every property the city
owns — 7,022 rows split across four owning agencies:

| Eier / fester | Antall | Rolle |
|---|---|---|
| Eiendoms- og byfornyelsesetaten | 5,621 | Tomter og veiareal — kommunens største grunneier |
| Oslobygg KF | 795 | Formålsbygg: skoler, barnehager, sykehjem, idrettsanlegg, kontorer |
| Boligbygg Oslo KF | 528 | Kommunale utleieboliger |
| Oslo Havn KF | 78 | Havnevirksomhet og eiendommer langs sjøsiden |

All four are mapped by default. The scope is configurable via `INCLUDE_EIER` in
`source.env` if you want to filter (e.g. only housing).

## Why two geocoders, not one

There are no coordinates in the source file — only a **matrikkelnummer** per
row (`0301 - 10 / 68 / 0 / 0`, where `0301` = Oslo). Two complementary
resolvers convert it to map geometry:

1. **Geonorge address API** (free, no key) — gives a point at the parcel's
   registered street address. Covers about a third of the register; the
   municipal *buildings* (Boligbygg housing, Oslobygg schools/care homes) all
   have addresses. Also returns `postnummer` + `poststed` for the popup.
2. **Kartverket "Matrikkelen – Eiendomskart Teig" WFS** (free, no key) —
   gives the *polygon* of the parcel itself, used both as a fallback for
   address-less rows AND alongside Geonorge so addressed properties also show
   the building's footprint on the map.

The pipeline deduplicates 7,022 rows to ~6,636 unique `(gnr, bnr)` pairs and
caches every API answer in SQLite (`geocode_cache.sqlite`) so re-runs are
instant and resumable.

## Run it

Dependencies are managed with Poetry (`pyproject.toml`, non-package mode):

```bash
poetry install
poetry run python geocode.py "Oversikt-over-Oslo-kommunes-eiendommer-mai-2026_nett-2.xlsx"
# → eiendommer.geojson, geocode_cache.sqlite, missing.csv

# then serve the map (any static server; index.html fetches the geojson):
poetry run python -m http.server 8000   # → http://localhost:8000/index.html
```

A full cold run hits Geonorge ~6,636 times and Kartverket Teig ~6,500 times.
Polite sleeps (50 ms / 125 ms) keep both services happy; expect 30–60 minutes
on a fresh cache, near-instant on subsequent runs.

`index.html` renders an embedded sample if `eiendommer.geojson` isn't there
yet, so you can poke at the UI before geocoding finishes.

## Auto-sync from upstream

`.github/workflows/sync.yml` runs every Monday: scrapes Oslo kommune's
[eiendomsoversikt page](https://www.oslo.kommune.no/plan-bygg-og-eiendom/kart-og-eiendomsinformasjon/kommunal-eiendom/eiendomsoversikt/)
for the latest XLSX link (anchor text `(XLSX)` — format-agnostic; the kommune
migrated their CMS at some point and the old `/getfile.php/<id>-<timestamp>/`
became `/get-file/<id>/<hash>/`, both handled by the same scraper), downloads
the file, runs `geocode.py`, and commits the regenerated `eiendommer.geojson`
if anything changed. The kommune republishes the file every six months, so
weekly polling is plenty.

Page URL + anchor marker + filter scope all live in `source.env` so a future
restructure can be fixed without touching the workflow YAML.

## The map (`index.html`)

Single file, no build step, CDN deps (Leaflet + markercluster + leaflet.heat +
Tailwind Play CDN). Deployed automatically to GitHub Pages on every push to
`main`.

- **Punkter** — clustered markers, coloured by owning agency
- **Tetthet** — heat layer (where ownership clusters)
- **Areal-vekt** — heat weighted by parcel size (big sites glow brighter)
- **Parcel polygons** — Kartverket Teig outlines styled per agency colour
- Toggle agencies, filter by bydel, switch basemap (CARTO Mørk / Lys / Esri
  Satellitt) — selections + map centre/zoom + sidebar state all persist in
  localStorage
- Light/dark palette follows `prefers-color-scheme`; saved layer choice wins
- Popup links straight into Google Maps Street View per marker

### Swapping the basemap / using Mapbox or MapLibre

Three basemaps ship by default — CARTO `dark_all`, CARTO `light_all`, and
Esri World Imagery (all token-free). To swap to Mapbox GL or MapLibre GL,
that's a rewrite of the `L.tileLayer` calls; the GeoJSON output is standard,
so it feeds any of them — Google Maps, Mapbox, MapLibre, Leaflet, or QGIS.
