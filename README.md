# KomBo — Oslo kommunes eiendommer på kart

> **KomBo** (fra «kommunale boliger») — navnet stammer fra boligfokuset, men
> kartet viser i dag *alle* fire kommunale etater (EBY, Oslobygg, Boligbygg, Havn).

An interactive Leaflet map of **every property Oslo kommune owns** — across all
four municipal bodies: from rental housing to schools, from road parcels to the
harbour. Live at **<https://kombo.ichiva.no/>**.

This repo is **just the map** (`index.html`). The data — downloading the
kommune's spreadsheet, geocoding every parcel, and serving it — is produced and
served by a separate service, **[kombo-api](https://github.com/deviationist/kombo-api)**.

```
kombo-api  ──>  /eiendommer.geojson  ──(fetch)──>  index.html  ──>  map in browser
```

## What's in scope

The source spreadsheet (re-published every six months by the kommune) lists
every property the city owns — ~7,000 rows across four owning agencies:

| Eier / fester | Antall | Rolle |
|---|---|---|
| Eiendoms- og byfornyelsesetaten | ~5,621 | Tomter og veiareal — kommunens største grunneier |
| Oslobygg KF | ~795 | Formålsbygg: skoler, barnehager, sykehjem, idrettsanlegg, kontorer |
| Boligbygg Oslo KF | ~528 | Kommunale utleieboliger |
| Oslo Havn KF | ~78 | Havnevirksomhet og eiendommer langs sjøsiden |

Plus a toggleable layer of Oslo-owned land *outside* Oslo (Oslomarka forest +
drinking-water catchment in ~20 neighbouring kommuner).

## Data source

`index.html` fetches its dataset at boot, in order:

1. **`https://kombo-api.ichiva.no/eiendommer.geojson`** — the live API (single
   source of truth, regenerated weekly).
2. **`eiendommer.geojson`** committed here — a frozen fallback snapshot so the
   map keeps working if the API is down, and so a static deploy works on its own.

If both fail, an embedded sample dataset renders with a banner. The data
pipeline (XLSX → geocode → GeoJSON), the `/nearby` proximity endpoint, and the
GeoJSON contract all live in **kombo-api**.

## Run it

No build step, no dependencies — it's one static file:

```bash
python -m http.server 8000   # → http://localhost:8000/index.html
```

It fetches from the live API by default; if that's unreachable it uses the
committed `eiendommer.geojson` beside it.

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
- A **"Lag"** section toggles the two layers ("Eiendommer i Oslo" master gate +
  "Eiendommer utenfor Oslo"), then per-agency toggles, bydel filter (with live
  counts), basemap switch (CARTO Mørk / Lys / Esri Satellitt)
- "Topp bydeler" ranking — top 10 districts by parcel count in the current
  selection
- Popup links straight into Grunnboken/eiendomsregisteret for each parcel

**Right sidebar** — "Mine punkter" (sammenligning):

- Add a pin by typing an address (debounced Geonorge search) OR clicking
  "Klikk på kart" + clicking a spot OR right-clicking the map → "Legg til
  markør her"
- Each pin is auto-enriched: reverse-geocoded for address + matrikkel,
  then the parcel's Teig polygon is fetched from Kartverket and drawn in
  magenta around the pin
- Pin popup ranks the 25 nearest kommune parcels within 150 m, split into
  "Bygg" (Oslobygg / Boligbygg / Havn) and "Tomter / veiareal" (EBY).
  Distance measured to the closest tomtekant (default) or the address
  marker — toggle via "Avstand måles til"
- Hover a row to preview the measurement line; click to pin it (solid,
  labelled with distance, persists across reloads). Click the row again to unpin

**Persistence**:

- Selections, map centre/zoom, sidebar collapse state persist in `localStorage`
  (key `kombo.v1`); pins under a separate key (`kombo.userPins.v1`)
- Light/dark palette follows `prefers-color-scheme`; saved layer choice wins

**Shareable URLs**: the current view round-trips through `location.hash` — paste
any link to land on the same map state
(`#z=12&lat=…&own=bygg,bolig&bydel=Sagene&teig=0&dm=marker`).

### Swapping the basemap

Three basemaps ship by default — CARTO `dark_all`, CARTO `light_all`, and
Esri World Imagery (all token-free). The GeoJSON output is standard, so it feeds
any renderer — Google Maps, Mapbox, MapLibre, Leaflet, or QGIS.
