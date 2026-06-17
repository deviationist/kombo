# KomBo — Oslo kommunes eiendommer på kart

> **KomBo** (fra «kommunale boliger») — navnet stammer fra boligfokuset, men
> kartet viser i dag *alle* fire kommunale etater (EBY, Oslobygg, Boligbygg, Havn).

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
instant and resumable. The cache schema (v3) carries lat/lon + adressetekst
+ postnummer + poststed (Geonorge) plus the polygon GeoJSON (Kartverket),
with separate `addr_source` / `geom_source` columns so each resolver's
attempt state is tracked independently.

## Run it

Dependencies are managed with Poetry (`pyproject.toml`, non-package mode):

```bash
poetry install

# (optional) download today's XLSX from the kommune in one command
poetry run python fetch_xlsx.py            # → writes the file with the kommune's filename
poetry run python fetch_xlsx.py --url-only # just print the resolved URL

# geocode + render
poetry run python geocode.py "Oversikt-over-Oslo-kommunes-eiendommer-mai-2026_nett-2.xlsx"
# → eiendommer.geojson, geocode_cache.sqlite, missing.csv

# serve the map (any static server; index.html fetches the geojson):
poetry run python -m http.server 8000   # → http://localhost:8000/index.html
```

`geocode.py` uses a 3-worker `ThreadPoolExecutor` (configurable via
`WORKERS` in `source.env`) to triple throughput against the upstream APIs
without going past polite use. A full cold run takes about 30–40 min;
re-runs against a warm cache are seconds. Cache hits skip the network
entirely, so partial runs are resumable — kill at any point, restart and
it picks up.

`index.html` renders an embedded sample if `eiendommer.geojson` isn't there
yet, so you can poke at the UI before geocoding finishes.

## Auto-sync from upstream

`.github/workflows/sync.yml` runs every Monday: scrapes Oslo kommune's
[eiendomsoversikt page](https://www.oslo.kommune.no/plan-bygg-og-eiendom/kart-og-eiendomsinformasjon/kommunal-eiendom/eiendomsoversikt/)
for the latest XLSX link via `fetch_xlsx.py` (anchor text `(XLSX)` —
format-agnostic; the kommune migrated their CMS at some point and the old
`/getfile.php/<id>-<timestamp>/` became `/get-file/<id>/<hash>/`, both handled
by the same scraper), runs `geocode.py`, and commits the regenerated
`eiendommer.geojson` if anything changed. The kommune republishes the file
every six months, so weekly polling is plenty.

Page URL + anchor marker + filter scope all live in `source.env` so a future
restructure can be fixed without touching the workflow YAML.

## The map (`index.html`)

Single file, no build step, CDN deps (Leaflet + markercluster + leaflet.heat +
Tailwind Play CDN). Deployed automatically to GitHub Pages on every push to
`main`.

**Left sidebar** — the kommune view:

- **Punkter** — clustered markers, coloured by owning agency. Cluster bubbles
  inherit the colour when all children share one owner; mixed clusters go
  neutral grey
- **Tetthet** — heat layer (where ownership clusters)
- **Areal-vekt** — heat weighted by parcel size (big sites glow brighter)
- **Vis tomtegrenser** — Kartverket Teig outlines styled per agency colour
  (cluster mode only — they fight the heat visualisation in the other modes)
- Toggle agencies, filter by bydel (the dropdown shows live counts per
  district), switch basemap (CARTO Mørk / Lys / Esri Satellitt)
- "Topp bydeler" ranking — top 10 districts by parcel count in the current
  selection
- Popup links straight into Google Maps Street View AND
  Grunnboken/eiendomsregisteret for each parcel

**Right sidebar** — "Mine punkter" (sammenligning):

- Add a pin by typing an address (debounced Geonorge search) OR clicking
  "Klikk på kart" + clicking a spot OR right-clicking the map → "Legg til
  markør her" (prompts for an optional name)
- Each pin is auto-enriched: reverse-geocoded for address + matrikkel,
  then the parcel's Teig polygon is fetched from Kartverket and drawn in
  magenta around the pin
- Pin popup ranks the 25 nearest kommune parcels within 150 m, split into
  "Bygg" (Oslobygg / Boligbygg / Havn) and "Tomter / veiareal" (EBY).
  Distance measured to the closest tomtekant (default) or the address
  marker — toggle via "Avstand måles til"
- Hover a row to preview the measurement line; click to pin it (solid,
  labelled with distance, persists across reloads). Click the line later
  to re-open the pin's popup. Click the row again to unpin
- Pinned-line targets remain visible on the map regardless of view mode
  or agency filter — the line always has something to point at
- Sidebar list lets you focus a pin, rename it, or delete it; the in-popup
  "Slett punkt" does the same

**Persistence**:

- Selections, map centre/zoom, sidebar collapse state, etc. persist in
  `localStorage` (key `kombo.v1`); pins under a separate key
  (`kombo.userPins.v1`)
- Light/dark palette follows `prefers-color-scheme`; saved layer choice
  wins on subsequent visits

**Shareable URLs**: the current view is round-tripped through
`location.hash` — paste any link to land on the same map state
(`#z=12&lat=…&own=bygg,bolig&bydel=Sagene&teig=0&dm=marker`). UI-only
preferences (rail collapsed, mode-info open) stay in localStorage only.

### Swapping the basemap / using Mapbox or MapLibre

Three basemaps ship by default — CARTO `dark_all`, CARTO `light_all`, and
Esri World Imagery (all token-free). To swap to Mapbox GL or MapLibre GL,
that's a rewrite of the `L.tileLayer` calls; the GeoJSON output is standard,
so it feeds any of them — Google Maps, Mapbox, MapLibre, Leaflet, or QGIS.
