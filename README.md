# Oslo kommunes eiendommer → kart

A two-step pipeline that turns the municipal property spreadsheet into an
interactive map showing **where Oslo kommune owns land and buildings**.

```
XLSX ──(geocode.py)──> eiendommer.geojson ──(index.html)──> map in browser
```

## What the data actually is (read this first)

The spreadsheet lists **7 022 properties, all owned by Oslo kommune** — split
across four municipal bodies:

| Eier / fester | Antall | Rolle |
|---|---|---|
| Eiendoms- og byfornyelsesetaten | 5 621 | Land, road parcels, urban-renewal sites |
| Oslobygg KF | 795 | Public buildings (schools, care homes, offices) |
| Boligbygg Oslo KF | 528 | Municipal housing |
| Oslo Havn KF | 78 | Harbour / port |

So this file answers *"which areas does the municipality own, and through which
agency?"* — but it **cannot** show what's owned by private people or housing
associations (borettslag/sameier). That data isn't in here. To compare municipal
vs. private ownership you'd need the full **Matrikkel** ownership register from
Kartverket (the spreadsheet is just the municipality's own slice of it).

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

## Coverage and the road-land gap

~2 200 rows have a street address and geocode cleanly. The other ~4 800 are
mostly **veigrunn** (road land) and **uregistrert grunn** (unregistered land)
that simply have *no* address — those land in `missing.csv`.

To map those too you need the **parcel geometry** (the polygon), not an address.
The source is Kartverket's *"Matrikkelen – Eiendomskart Teig"* dataset, available
as WFS/WMS via Geonorge, keyed on the matrikkelnummer. That route gives you the
actual lot outlines (better than points for an area/ownership view) but is
heavier to wire up and the exact WFS filter syntax is worth verifying against the
current Geonorge service description before you build on it. The geocoder is
structured so you can add a second resolver for the missing pairs without
touching the rest.

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
