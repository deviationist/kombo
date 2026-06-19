# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

The **frontend map** — a single hand-written `index.html` (Leaflet) showing
**every property Oslo kommune owns** across all four agencies: Eiendoms- og
byfornyelsesetaten (tomter/veiareal), Oslobygg KF (formålsbygg), Boligbygg Oslo
KF (utleieboliger), Oslo Havn KF (havn). Plus a toggleable layer of Oslo-owned
land *outside* Oslo (Oslomarka forest + drinking-water catchment). Live at
**<https://kombo.ichiva.no/>** via GitHub Pages.

This repo is **just the map**. The data pipeline + API that produce and serve
the dataset live in a separate repo, **[kombo-api](https://github.com/deviationist/kombo-api)**
(downloads the kommune XLSX, geocodes via Geonorge + Kartverket, serves GeoJSON
and a `/nearby` proximity endpoint).

```
kombo-api  ──(HTTP)──>  index.html  ──>  map in browser
 (data system + API)
```

## Data source

`index.html` loads its data at boot from the kombo-api service (see `boot()`):

- `https://kombo-api.ichiva.no/features` — the single source of truth
  (regenerated weekly).

If the API is unreachable, the map renders the embedded `SAMPLE` features
(inline in `index.html`) with a "could not load" banner so it degrades
gracefully instead of blank.

### GeoJSON shape (the contract this map consumes)

FeatureCollection with:

- `metadata` — `{ vintage, sourceFile, generatedAt, dataChangedAt, contentHash,
  totalRows, located, utenbysRows, utenbysLocated }`. `vintage` (e.g. "mai
  2026") is the kommune's release; `dataChangedAt` advances only on a real
  content change (the "Sist oppdatert" footer uses it, falling back to
  `generatedAt`). `totalRows`/`located` are the in-Oslo counts; `utenbys*` the
  out-of-kommune layer.
- `features[i].geometry` — Polygon / MultiPolygon (Kartverket parcel) or Point
  (Geonorge address point) fallback.
- `features[i].properties` — `eiendom` (`0301-10/68/0/0`), `adresse`,
  `postnummer`/`poststed`, `bydel` (empty for utenbys), `eier` (one of the four
  agencies), `utenbys` (true on out-of-Oslo features), `kommune` +
  `matrikkelenhetstype` (utenbys only), `bruksnavn`, `areal` (nullable),
  `center` (`[lon, lat]` cluster-marker anchor on addressed polygons).

To change how the data is produced or served, work in the **kombo-api** repo.

## Commands

No build step, no dependencies. Serve locally:

```bash
python -m http.server 8000   # then open http://localhost:8000/index.html
```

There are no tests and no linter config.

## Architecture — `index.html`

Single hand-written file. No build step; loads Leaflet + markercluster +
leaflet.heat + Tailwind Play CDN from CDN.

#### Layout

- **Map fills the full viewport** (position:absolute, inset:0). Sidebars
  are overlays on top — neither resizes the map, so Leaflet's tile grid
  is stable through any UI motion.
- **Left sidebar** (`#rail`, 340 px) — kommune view: stats, view mode
  (Punkter / Tetthet / Areal-vekt) + "Vis tomtegrenser" checkbox, a **"Lag"**
  section listing the two map layers as toggles — "Eiendommer i Oslo"
  (`#show-oslo`, master gate for the in-Oslo register) and "Eiendommer utenfor
  Oslo" (`#show-utenbys`, the utenbys layer) — each with a feature count, then
  per-agency toggles (Eier/fester), bydel select + top bydeler ranking,
  distance-mode toggle. The utenbys *row* (`#utenbys-row`) auto-hides when the
  dataset has no such rows; the Oslo row always shows.
- **Right sidebar** (`#rail-r`, 300 px) — "Mine punkter": add-pin form
  (address search + "Klikk på kart" mode), distance-mode toggle, pin list.
- Both rails slide via `transform: translateX(-100% | 100%)`. Tab toggles
  on the outer edge + an in-rail `×` button.
- **FOUC guard** in `<head>` applies `rail-collapsed` / `rail-r-collapsed`
  to `<html>` before first paint, based on localStorage.
- Leaflet's corner controls (`leaflet-top.leaflet-left`, etc.) auto-shift
  with the rails via CSS transform so nothing ends up hidden under an
  overlay.
- **Floating search bar** (`#search-bar`, top-left past the zoom controls)
  — searches the kommune dataset by address / matrikkel / bruksnavn
  (`#search-q` input, `#search-results` dropdown). Mirrors the same
  rail-shifting `translateX(var(--rail-w))` transform as the corner controls
  so it never overlaps the left rail.
- **Responsive (mobile/tablet)** — `@media` breakpoints at 1024 px and
  760 px (the latter also `pointer:coarse`) reflow the rails/search bar for
  small screens; the viewport meta disables page-zoom (`maximum-scale` /
  `user-scalable`) so focusing an input doesn't trigger iOS auto-zoom.

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
  Teig WFS **from the browser** (Filter Encoding 2 XML, GML parsed with
  DOMParser → GeoJSON). Result: each user pin has its parcel outline drawn in
  magenta around it.
- **Proximity popup**: ranks the 25 nearest kommune features within
  `MAX_PROXIMITY_M = 150`, split into "Bygg" (15 max, agencies that aren't
  EBY) + "Tomter / veiareal" (10 max, EBY). Distance computed by
  `geometryClosest(lat, lon, geom)` (point-to-polygon-edge) or
  `featureClosestForMode(pin, feature)` (which honours
  `state.distanceMode` = `'edge'` | `'marker'`). **Note:** the kombo-api
  `/nearby` endpoint is a 1:1 server-side port of `geometryClosest` — if you
  change the proximity math here, mirror it there (equal results are the
  contract).
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
`showPolygons`, `showOslo`, `showUtenbys`, `distanceMode`. UI-only fields stay here.

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
 &own=<short codes, comma-separated>&bydel=<urlencoded>&teig=0|1&osl=0|1&utb=0|1&dm=edge|marker
```

`utb=1` turns on the "Eiendommer utenfor Oslo" (utenbys) layer; `osl=0` turns
off the "Eiendommer i Oslo" master layer (default on, so only the off-state is
emitted, mirroring `utb`).

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

## GitHub Pages deployment

Repo is public; Pages serves `main` root. `CNAME` file at root makes Pages
serve at `https://kombo.ichiva.no/` (matching CNAME record at Cloudflare,
proxied is fine). Every push to `main` redeploys.

## Conventions

- **Data production + serving live in [kombo-api](https://github.com/deviationist/kombo-api)** —
  XLSX download, geocoding, cache schema, the `/nearby` API. Don't reintroduce a
  pipeline here.
- Keep `geometryClosest` / `featureClosestForMode` in sync with kombo-api's
  `/nearby` proximity math — equal results are the contract.
- `geometryClosest` works in a local metres-from-pin projection. Accurate at
  city scale (< few km), not for cross-Norway distances. We're inside one
  kommune so this is fine.
- Pinned-line storage uses **feature identity** (eiendomsstring) not
  coordinates, so a distance-mode flip can re-derive the endpoint without
  losing the pin. Legacy `pin.pinnedClosests` entries are dropped on load.
- The utenbys layer is gated by its toggle **and** the agency (Eier/fester)
  filter, but **not** by the bydel filter (utenbys rows have no bydel); it's
  excluded from the bydel/area stats.
- The map consumes the API only — don't reintroduce a committed dataset or a
  geocoder here; that all lives in kombo-api.
