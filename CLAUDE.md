# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A two-step pipeline that turns the Oslo kommune property spreadsheet
(`Oversikt-over-Oslo-kommunes-eiendommer-mai-2026_nett-2.xlsx`, 7,022 rows)
into an interactive Leaflet map of **every municipally-owned property** across
all four agencies: Eiendoms- og byfornyelsesetaten (tomter/veiareal, ~5,621
rows), Oslobygg KF (formålsbygg, ~795), Boligbygg Oslo KF (utleieboliger,
~528), Oslo Havn KF (havn, ~78). Live at **<https://kombo.ichiva.no/>** via
GitHub Pages.

This is *only* the kommune's own slice of the cadastre — it cannot answer
"what's privately owned" without pulling the full Matrikkel register.

```
XLSX ──(geocode.py)──> eiendommer.geojson ──(index.html)──> map in browser
```

## Commands

Dependencies are Poetry-managed, `package-mode = false` (script-only, not a
distributable package).

```bash
poetry install
poetry run python geocode.py "Oversikt-over-Oslo-kommunes-eiendommer-mai-2026_nett-2.xlsx"
# → eiendommer.geojson, geocode_cache.sqlite, missing.csv

poetry run python -m http.server 8000   # serves index.html locally
```

There are no tests, no linter config, and no build step.

## Architecture

### Two-resolver geocoder (`geocode.py`)

The matrikkelnummer in the `Eiendom` column (`KNR-GNR/BNR/FNR/SNR`, KNR always
`0301` for Oslo) is the join key. The script:

1. Parses every row's matrikkel via `MATRIKKEL_RE`.
2. **Deduplicates to ~6,636 unique `(gnr, bnr)` pairs** before hitting the
   network — important because the resolvers are free public APIs.
3. Calls **two independent resolvers** for each pair, chained in `main()`:
   - **Geonorge address API** (`ws.geonorge.no/adresser/v1/sok`) — no key,
     returns WGS84 directly via `utkoordsys=4326`. Multiple matching addresses
     are averaged on their `representasjonspunkt`. We also keep `postnummer`
     and `poststed` from the first match for the popup.
   - **Kartverket "Matrikkelen – Eiendomskart Teig" WFS**
     (`wfs.geonorge.no/skwms1/wfs.matrikkelen-eiendomskart-teig`) — no key,
     GML 3.2 only (no GeoJSON output), Filter Encoding 2.0 with
     `app:kommunenummer` + `app:matrikkelnummerTekst`. A single
     `(gnr, bnr)` can return multiple `app:Teig` features (very common for
     road land); the parser in `parse_teig_response` assembles them into a
     `MultiPolygon`. CRS `urn:ogc:def:crs:EPSG::4326` is requested directly
     so no `pyproj` reprojection is needed.
4. **Caches every API answer in `geocode_cache.sqlite`** keyed on
   `(gnr, bnr)`. Re-runs are instant and resumable; deleting the cache forces
   a re-fetch. The two resolvers track independent state via separate
   `addr_source` / `geom_source` columns (cache schema v3), so a successful
   address with no polygon can later get its polygon filled in without
   re-asking Geonorge.
5. Joins back to every XLSX row → writes `eiendommer.geojson` (Polygons
   where Kartverket has the geometry, Points where only Geonorge has an
   address) and `missing.csv` (rows neither resolver could place).

Retry/backoff lives in each resolver (4 attempts, 1.5s × attempt for 429/5xx).
Politeness sleeps: `SLEEP = 0.05` (Geonorge), `TEIG_SLEEP = 0.125`
(Kartverket). Be polite if you tune these.

**Output shape** (the pipeline contract):

`eiendommer.geojson` is a FeatureCollection where each feature has:

- `geometry` — `Polygon` / `MultiPolygon` (from Kartverket) OR `Point`
  (Geonorge address representation point) as fallback
- `properties.eiendom` — `0301-10/68/0/0`
- `properties.adresse` — Geonorge `adressetekst` (street, no postal code)
- `properties.postnummer` — 4-digit ZIP, when Geonorge provided it
- `properties.poststed` — postal area name, when Geonorge provided it
- `properties.bydel` — Oslo bydel from the XLSX
- `properties.eier` — owning agency name (one of the four)
- `properties.bruksnavn` — descriptive name from the XLSX, often empty
- `properties.areal` — `Beregnet areal (m²)`, nullable
- `properties.center` — `[lon, lat]` of the Geonorge address point, set only
  on Polygon features that also have an address. The map uses this as the
  cluster-marker anchor so polygons get both a parcel outline *and* a
  marker at the building's front door.

### Cache schema (`geo` table in `geocode_cache.sqlite`)

```sql
CREATE TABLE geo (
    gnr INTEGER, bnr INTEGER,
    lat REAL, lon REAL,                  -- Geonorge representation point
    adressetekst TEXT,                   -- Geonorge street text
    source TEXT,                         -- v1 legacy, unused; preserved for old caches
    geometry_json TEXT,                  -- Kartverket polygon as GeoJSON string
    addr_source TEXT,                    -- NULL | 'geonorge_adresse' | 'none'
    geom_source TEXT,                    -- NULL | 'kartverket_teig' | 'none'
    postnummer TEXT, poststed TEXT,
    PRIMARY KEY (gnr, bnr)
)
```

`open_cache` migrates v1 (single `source` field) and v2 (no postnummer)
caches in place. A row with `addr_source='geonorge_adresse'` AND
`postnummer IS NULL` triggers a Geonorge re-fetch on the next run so old
addressed pairs pick up the new fields automatically.

### `index.html` — the map

Single hand-written file. **`geocode.py` does NOT generate it.** No build
step; loads Leaflet + markercluster + leaflet.heat + Tailwind Play CDN from
CDN. Falls back to embedded sample features when `eiendommer.geojson` isn't
fetchable, so the page is useful before geocoding finishes.

Things worth knowing about the UI:

- **Sidebar is an overlay** on a full-viewport map (positioned absolutely,
  z-index 500). Slides via `transform: translateX(-100%)`. The map never
  resizes — Leaflet's tile grid stays put, no flicker. The FOUC guard in
  `<head>` applies `rail-collapsed` to `<html>` before first paint based on
  localStorage.
- **Light/dark mode is automatic** via `prefers-color-scheme: light` CSS
  variable overrides. Marker halo / cluster halo / Leaflet attribution
  background all read from CSS vars so they flip palettes without JS.
  Three basemaps in the layer control: CARTO `dark_all` (Mørk), CARTO
  `light_all` (Lys), Esri World Imagery (Satellitt). Default basemap on a
  fresh visit follows the OS preference; saved choice wins.
- **localStorage** (key `kombo.v1`) persists: `bydel`, `mode`, `base`,
  `active` (owner set), `center`, `zoom`, `railCollapsed`, `modeInfoOpen`.
- **Default owner selection** on a fresh visit is `Boligbygg Oslo KF` only,
  via `DEFAULT_ACTIVE_OWNERS` near the top of the script.
- **Tailwind via Play CDN** with Preflight disabled (`corePlugins.preflight:
  false`) so it doesn't clobber the existing form/popup styling. Available
  for new markup; existing CSS uses CSS variables that double as Tailwind
  colour tokens (`bg-panel`, `text-ink`, etc.).
- **The `OWNERS` array** is the closed set of recognised agency names (also
  drives the colour palette + Norwegian descriptions). Keep in sync with the
  `Eiers/festers kontaktinstans` values in the XLSX.

### Auto-sync workflow (`.github/workflows/sync.yml`)

Runs every Monday 06:00 UTC. Scrapes the kommune's
`/plan-bygg-og-eiendom/.../eiendomsoversikt/` page for the latest XLSX
download link (matching by anchor text `(XLSX)`, not href pattern — the
kommune migrated their CMS at one point from `/getfile.php/...` to
`/get-file/...`), downloads it, runs `geocode.py`, commits the regenerated
`eiendommer.geojson` + `missing.csv` if anything changed, pushes back to
`main`. `actions/cache@v4` preserves `geocode_cache.sqlite` between runs so
only new pairs hit the upstream APIs.

The kommune republishes the XLSX every six months, so weekly polling is
plenty. Page URL + anchor marker live in `source.env`.

### `source.env`

`.env`-style config loaded by both the workflow (`source ./source.env`) and
`geocode.py` (auto-loaded via tiny built-in dotenv reader, no new dep). Vars
already in the shell win, so CI can override. Holds:

- `SOURCE_HOST`, `SOURCE_PAGE_PATH` — where the eiendomsoversikt page lives
- `XLSX_LINK_MARKER` — anchor text substring to find the download link
- `KOMMUNE` — 4-digit kommunenummer, default `0301` (Oslo). Trivially
  repointable to another kommune.
- `INCLUDE_EIER` — comma-separated agency filter, empty = all four (default)

### GitHub Pages deployment

Repo is public; Pages serves `main` root. CNAME file at root makes Pages
serve at `https://kombo.ichiva.no/` (matching CNAME record at Cloudflare,
DNS-only). Every push to `main` triggers a redeploy, so the auto-sync
workflow's geojson commits also redeploy the site automatically.

## Conventions

- The XLSX column names in `cols` (in `main()`) are the contract with the
  source spreadsheet. If a new spreadsheet drops with renamed columns, this
  is the single place to update.
- `geocode_cache.sqlite` is gitignored but treat it as durable data, not
  throwaway. A full cold re-geocode of ~6,636 pairs against both APIs is
  30–60 minutes plus upstream latency.
- Output GeoJSON uses `[lon, lat]` order (GeoJSON spec) even though the
  cache stores `(lat, lon)`. Conversion happens at output build time.
- `eier` filtering in `main()` strips whitespace before matching
  `INCLUDE_EIER` — defends against the leading-space hygiene bug seen in
  the April 2025 release of the XLSX.
- The April 2025 XLSX was at a different URL scheme (`/getfile.php/<id>-<unix-ts>/`
  vs. today's `/get-file/<id>/<hash>/`). The workflow's anchor-text-based
  link finder survives the CMS migration; **do not narrow the href regex**
  to a specific pattern.
- The XLSX has a second sheet `Utenbys eiendommer` (~150 rows, properties
  in *other* kommuner — forest land, water sources, etc.) with a different
  schema. The pipeline currently only processes the first sheet
  (`Eiendommer i Oslo kommune`); the second sheet is intentionally skipped
  for now.
