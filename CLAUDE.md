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

A second XLSX sheet (`Utenbys eiendommer`, ~160 rows) lists Oslo-owned land
*outside* Oslo (Oslomarka forest + drinking-water catchment in ~20 neighbouring
kommuner); these are processed too and surfaced as a toggleable map layer.

This is *only* the kommune's own slice of the cadastre — it cannot answer
"what's privately owned" without pulling the full Matrikkel register. The
pipeline is structurally multi-kommune (parcels carry their own KNR); Oslo is
the currently-configured source via `KOMMUNE` in `source.env`.

```
XLSX ──(geocode.py)──> eiendommer.geojson ──(index.html)──> map in browser
                            ↑
            fetch_xlsx.py downloads the XLSX from kommune.no
```

## Commands

Dependencies are Poetry-managed, `package-mode = false` (script-only, not a
distributable package).

```bash
poetry install
poetry run python fetch_xlsx.py            # download today's XLSX (kommune filename)
poetry run python fetch_xlsx.py --url-only # just resolve + print the URL
poetry run python geocode.py <path.xlsx>   # → eiendommer.geojson, geocode_cache.sqlite, missing.csv
poetry run python -m http.server 8000      # serve index.html locally
```

There are no tests, no linter config, and no build step.

## Architecture

### `fetch_xlsx.py` — kommune-page scraper + downloader

Standalone module + CLI. Reads `source.env` for the page URL + anchor
marker (`XLSX_LINK_MARKER`, default `(XLSX)`), GETs the eiendomsoversikt
page, matches anchors by their visible text containing the marker, and
streams the resolved URL to disk. Used by both the GitHub Actions workflow
and humans running things locally. Stays **format-agnostic** — matches by
text, not by href pattern — so a future CMS URL change at the kommune
doesn't break the downloader.

### Two-resolver geocoder (`geocode.py`)

The matrikkelnummer in the `Eiendom` column (`KNR-GNR/BNR/FNR/SNR`) is the
join key. KNR is `0301` for the in-Oslo sheet and the host municipality for
the utenbys sheet, so the pipeline is structurally multi-kommune (Oslo is just
the configured source via `KOMMUNE` in `source.env`; the resolvers no longer
hardcode it). The script:

1. Parses every row's matrikkel via `MATRIKKEL_RE` (both sheets).
2. **Deduplicates to ~6,796 unique `(knr, gnr, bnr)` triples** before hitting
   the network — important because the resolvers are free public APIs.
3. For each pair, calls **two independent resolvers** (see below). State
   is tracked separately for each via `addr_source` / `geom_source` cache
   columns, so success on one resolver doesn't lock out the other.
4. Uses a **`ThreadPoolExecutor` (3 workers by default; `WORKERS` env var
   in `source.env` to tune)** for concurrency. Triples throughput over the
   serial baseline while staying well within "polite for a free public
   service" range. SQLite connection is shared with `check_same_thread=False`
   + a module-level `_cache_lock` around every SELECT/INSERT.
5. Joins back to every XLSX row → writes `eiendommer.geojson` with a
   `metadata` block (vintage parsed from filename, source filename,
   generated-at timestamp, total row count, located count). The map reads
   metadata to populate the eyebrow + footer text dynamically.

**Resolvers**:

- **Geonorge address API** (`ws.geonorge.no/adresser/v1/sok`) — no key,
  returns WGS84 directly via `utkoordsys=4326`. Multiple matching addresses
  are averaged on their `representasjonspunkt`. We also keep `postnummer`
  and `poststed` from the first match.
- **Kartverket "Matrikkelen – Eiendomskart Teig" WFS**
  (`wfs.geonorge.no/skwms1/wfs.matrikkelen-eiendomskart-teig`) — no key,
  GML 3.2 only (no GeoJSON output), Filter Encoding 2.0 with
  `app:kommunenummer` + `app:matrikkelnummerTekst`. A single `(gnr, bnr)`
  can return multiple `app:Teig` features (very common for road land);
  `parse_teig_response` assembles them into a MultiPolygon. CRS
  `urn:ogc:def:crs:EPSG::4326` requested directly — no `pyproj` reprojection.

Retry/backoff in each resolver (4 attempts, 1.5s × attempt for 429/5xx).
Per-thread politeness sleeps: `SLEEP = 0.05` (Geonorge), `TEIG_SLEEP = 0.125`
(Kartverket). Combined load at 3 workers ≈ 3 req/s per resolver — safe.

**Output shape** (the pipeline contract):

`eiendommer.geojson` is a FeatureCollection with:

- `metadata` — `{ vintage, sourceFile, generatedAt, totalRows, located,
  utenbysRows, utenbysLocated }`. `totalRows`/`located` count the **in-Oslo**
  sheet only (what the intro copy refers to); `utenbysRows`/`utenbysLocated`
  the out-of-kommune layer.
- `features[i].geometry` — Polygon / MultiPolygon (Kartverket) OR Point
  (Geonorge address representation point) as fallback
- `features[i].properties`:
  - `eiendom` — `0301-10/68/0/0`
  - `adresse` — Geonorge `adressetekst` (street, no postal code)
  - `postnummer` / `poststed` — when Geonorge provided them
  - `bydel` — Oslo bydel from the XLSX (empty for utenbys features)
  - `eier` — owning agency name (one of the four)
  - `utenbys` — `true` on features from the `Utenbys eiendommer` sheet
    (Oslo-owned land in another kommune); absent/falsy on in-Oslo features
  - `kommune` — host municipality name (utenbys features only, e.g. "Asker")
  - `matrikkelenhetstype` — e.g. "Grunneiendom" (utenbys features only)
  - `bruksnavn` — descriptive name from the XLSX (often "Uregistrert grunn"
    — see `featureTitle` in index.html which skips that as a title)
  - `areal` — `Beregnet areal (m²)`, nullable
  - `center` — `[lon, lat]` of the Geonorge address point, set only on
    Polygon features that also have an address. The map uses this as the
    cluster-marker anchor so polygons get both the parcel outline *and* a
    marker at the building's front door

### Cache schema v4 (`geo` table in `geocode_cache.sqlite`)

```sql
CREATE TABLE geo (
    knr TEXT,                            -- kommunenummer ('0301' Oslo)
    gnr INTEGER, bnr INTEGER,
    lat REAL, lon REAL,                  -- Geonorge representation point
    adressetekst TEXT,                   -- Geonorge street text
    source TEXT,                         -- v1 legacy, unused; preserved for old caches
    geometry_json TEXT,                  -- Kartverket polygon as GeoJSON string
    addr_source TEXT,                    -- NULL | 'geonorge_adresse' | 'none'
    geom_source TEXT,                    -- NULL | 'kartverket_teig' | 'none'
    postnummer TEXT, poststed TEXT,
    PRIMARY KEY (knr, gnr, bnr)
)
```

`open_cache` migrates v1 (single `source` field), v2 (no postnummer), and
v3 (no `knr`) caches in place. The v3→v4 step adds `knr`, backfills existing
rows to `'0301'` (a v3 cache is all-Oslo), and rebuilds the table with the
composite PK so utenbys parcels in other kommuner can't collide with Oslo's
`(gnr, bnr)` namespace — tested to preserve all rows and be idempotent. A row
with `addr_source='geonorge_adresse'` AND `postnummer IS NULL` still triggers
a Geonorge re-fetch so old addressed pairs pick up the postal fields.

### `index.html` — the map

Single hand-written file. **`geocode.py` does NOT generate it.** No build
step; loads Leaflet + markercluster + leaflet.heat + Tailwind Play CDN from
CDN. Falls back to embedded sample features when `eiendommer.geojson` isn't
fetchable, so the page is useful before geocoding finishes.

#### Layout

- **Map fills the full viewport** (position:absolute, inset:0). Sidebars
  are overlays on top — neither resizes the map, so Leaflet's tile grid
  is stable through any UI motion.
- **Left sidebar** (`#rail`, 340 px) — kommune view: stats, view mode
  (Punkter / Tetthet / Areal-vekt), per-agency toggles, an "Andre lag"
  section with the "Eiendommer utenfor Oslo" (utenbys) layer toggle, bydel
  select + top bydeler ranking, "Vis tomtegrenser" checkbox, distance-mode
  toggle. The utenbys section auto-hides when the dataset has no such rows.
- **Right sidebar** (`#rail-r`, 300 px) — "Mine punkter": add-pin form
  (address search + "Klikk på kart" mode), distance-mode toggle, pin list.
- Both rails slide via `transform: translateX(-100% | 100%)`. Tab toggles
  on the outer edge + an in-rail `×` button.
- **FOUC guard** in `<head>` applies `rail-collapsed` / `rail-r-collapsed`
  to `<html>` before first paint, based on localStorage.
- Leaflet's corner controls (`leaflet-top.leaflet-left`, etc.) auto-shift
  with the rails via CSS transform so nothing ends up hidden under an
  overlay.

#### Theming + Tailwind

- **Light/dark mode automatic** via `prefers-color-scheme: light` CSS
  variable overrides. Marker halo / cluster halo / Leaflet attribution all
  read from CSS vars so they flip palettes without any JS dance.
- Three basemaps: CARTO `dark_all` (Mørk), CARTO `light_all` (Lys), Esri
  World Imagery (Satellitt). Default base on first visit follows OS
  preference; saved choice wins.
- **Tailwind via Play CDN** with Preflight disabled (`corePlugins.preflight:
  false`) so it doesn't clobber the existing form/popup styling. Available
  for new markup; existing CSS uses CSS variables that double as Tailwind
  colour tokens (`bg-panel`, `text-ink`, `bg-eby`, etc.).

#### Owners + colours

The `OWNERS` array is the closed set of recognised agency names + colours
+ short Norwegian descriptions. Colour palette tokens: `--c-eby`, `--c-bygg`,
`--c-bolig`, `--c-havn`, plus `--c-pin` (magenta) for user pins —
intentionally outside the four-agency palette to avoid confusion.

#### User pin system (right rail)

- **State**: `userPins: [{id, lat, lon, label, matrikkel, teig, pinnedFeatures}]`
  persisted under `localStorage["kombo.userPins.v1"]`.
- **Add a pin**: three entry points — Geonorge address search in the right
  rail (debounced), "Klikk på kart" toggle + map click, or right-click the
  map → context menu → prompt for optional name.
- **Enrich**: `enrichPin()` runs async after add. Reverse-geocodes via
  `adresser/v1/punktsok` for label + matrikkel, then queries the Kartverket
  Teig WFS **from the browser** (same Filter Encoding 2 XML shape as
  `geocode.py`, GML parsed with DOMParser → GeoJSON). Result: each user pin
  has its parcel outline drawn in magenta around it.
- **Proximity popup**: ranks the 25 nearest kommune features within
  `MAX_PROXIMITY_M = 150`, split into "Bygg" (15 max, agencies that aren't
  EBY) + "Tomter / veiareal" (10 max, EBY). Distance computed by
  `geometryClosest(lat, lon, geom)` (point-to-polygon-edge) or
  `featureClosestForMode(pin, feature)` (which honours
  `state.distanceMode` = `'edge'` | `'marker'`).
- **Pinned comparison lines**: click a row → solid magenta line with
  centre-tooltip distance label, persisted via `pin.pinnedFeatures` (array
  of eiendomsstrings — *not* coordinates — so a distance-mode flip can
  reroute the endpoint without losing pin identity).
- **Pinned-line target visibility**: a dedicated `pinnedTargetOverlay`
  L.layerGroup renders the target feature (anchor marker + optional
  polygon when `state.showPolygons`) regardless of view mode, but only
  when the main map isn't already showing it (cluster mode + active
  agency). Heat / Areal-vekt modes always include the overlay.

#### State + persistence

`localStorage["kombo.v1"]` holds: `bydel`, `mode`, `base`, `active`,
`center`, `zoom`, `railCollapsed`, `railRCollapsed`, `modeInfoOpen`,
`showPolygons`, `showUtenbys`, `distanceMode`. UI-only fields stay here.

`localStorage["kombo.userPins.v1"]` holds the user pin array (separate
key so user data is independent of UI prefs).

**Default `active`**: just Boligbygg Oslo KF on first visit
(`DEFAULT_ACTIVE_OWNERS`). An *explicit* empty selection persists — the
fallback only kicks in when `active` is absent from storage entirely.

#### URL hash sync

`urlFromState()` / `applyUrlHash()` round-trip a view-state subset through
`location.hash` so links are shareable. Hash schema:

```
#z=<int>&lat=<5dp>&lon=<5dp>&v=clusters|heat|heatArea&base=dark|light|sat
 &own=<short codes, comma-separated>&bydel=<urlencoded>&teig=0|1&utb=0|1&dm=edge|marker
```

`utb=1` turns on the "Eiendommer utenfor Oslo" (utenbys) layer.

Owner short codes: `eby`, `bygg`, `bolig`, `havn`. Empty `own=` encodes
the explicit-no-agencies state. `applyUrlHash()` runs after `loadState()`
so a shared link overrides personal preferences. `syncUrlDebounced()` runs
from `saveState()` at 250 ms debounce.

#### Popup `featureTitle(p)` helper

The kommune feature title falls back as: `addresse` → meaningful `bruksnavn`
(not "Uregistrert grunn" or empty) → `Eiendom <gnr>/<bnr>`. The map
displayed thousands of "Uregistrert grunn" labels before this; keep the
fallback if you touch the popup.

#### Grunnboken link

Both popups (kommune + user pin) carry a "Grunnboken ↗" link to
`https://eiendomsregisteret.kartverket.no/eiendom/<knr>/<gnr>/<bnr>`,
built via `grunnbokenUrl()`.

### Auto-sync workflow (`.github/workflows/sync.yml`)

Runs every Monday 06:00 UTC. Calls `fetch_xlsx.py` to download the latest
file, runs `geocode.py`, commits the regenerated `eiendommer.geojson` +
`missing.csv` if anything changed, pushes back to `main`.
`actions/cache@v4` preserves `geocode_cache.sqlite` between runs so only
new pairs hit the upstream APIs.

The kommune republishes every six months, so weekly polling is plenty.

### `source.env`

`.env`-style config loaded by `fetch_xlsx.py`, `geocode.py` (both auto-load
via tiny built-in dotenv reader, no new dep), and the workflow (`source
./source.env`). Vars already in the shell win, so CI can override. Holds:

- `SOURCE_HOST`, `SOURCE_PAGE_PATH` — where the eiendomsoversikt page lives
- `XLSX_LINK_MARKER` — anchor text substring to find the download link
- `KOMMUNE` — 4-digit kommunenummer, default `0301` (Oslo)
- `INCLUDE_EIER` — comma-separated agency filter, empty = all four
- `WORKERS` — geocoder thread count, default 3

### GitHub Pages deployment

Repo is public; Pages serves `main` root. `CNAME` file at root makes Pages
serve at `https://kombo.ichiva.no/` (matching CNAME record at Cloudflare,
proxied is fine). Every push to `main` redeploys, so the auto-sync
workflow's geojson commits also redeploy the site automatically.

## Conventions

- XLSX column names in `cols` (`main()`) are the contract with the source
  spreadsheet. If a new spreadsheet drops with renamed columns, this is
  the single place to update.
- `geocode_cache.sqlite` is gitignored but treat it as durable data, not
  throwaway. A full cold re-geocode of ~6,636 pairs against both APIs is
  ~30 min with the default 3-worker pool.
- Output GeoJSON uses `[lon, lat]` order (GeoJSON spec) even though the
  cache stores `(lat, lon)`. Conversion happens at output build time.
- `eier` filtering in `main()` strips whitespace before matching
  `INCLUDE_EIER` — defends against the leading-space hygiene bug seen in
  the April 2025 release.
- The April 2025 XLSX URL was `/getfile.php/<id>-<unix-ts>/...` vs today's
  `/get-file/<id>/<hash>/`. `fetch_xlsx.py`'s anchor-text-based link
  finder survives the CMS migration; **do not narrow the href regex** to
  a specific pattern.
- The XLSX has a second sheet `Utenbys eiendommer` (~160 rows, Oslo-owned
  properties in *other* kommuner — Oslomarka forest land + drinking-water
  catchment, ~157 EBY / 3 Oslobygg) with a different schema (`Kommune`,
  `Matrikkelenhetstype`, `Tinglyst`, `Eierandel`; no bydel/bruksnavn/areal).
  **Both sheets are now processed.** Utenbys rows are geocoded against their
  *own* kommune (per-row KNR) via the Teig WFS — most have no registered
  address — and emitted as features flagged `utenbys: true` + `kommune`.
  The map exposes them as a toggleable "Eiendommer utenfor Oslo" layer
  (`state.showUtenbys`, hash `utb=1`), independent of the agency + bydel
  filters and excluded from the bydel/area stats. `UTENBYS_SCHEMA` /
  `OSLO_SCHEMA` in `geocode.py` are the per-sheet column contracts.
- Pinned-line storage uses **feature identity** (eiendomsstring) not
  coordinates, so a distance-mode flip can re-derive the endpoint without
  losing the pin. Legacy `pin.pinnedClosests` entries are dropped on load.
- `featureClosestForMode` (and `geometryClosest`) work in a local
  metres-from-pin projection. Accurate at city scale (< few km), not for
  cross-Norway distances. We're inside one kommune so this is fine.
