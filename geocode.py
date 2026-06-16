#!/usr/bin/env python3
"""
geocode.py - Turn the Oslo municipal property XLSX into map-ready GeoJSON.

Each row's "Eiendom" is a Norwegian matrikkelnummer: KNR-GNR/BNR/FNR/SNR
(KNR is always 0301 = Oslo). We resolve coordinates from Kartverket/Geonorge's
open address API (no API key, WGS84 lat/lon out of the box).

Strategy:
  1. Parse the matrikkel into (gnr, bnr).
  2. Deduplicate: ~6,600 unique (gnr, bnr) pairs instead of 7,022 rows.
  3. Query Geonorge once per unique pair, cache every result in SQLite so
     re-runs are free and resumable.
  4. Join coordinates back to every row, write eiendommer.geojson.
  5. Write missing.csv for parcels with no registered address (mostly
     "veigrunn"/road land + unregistered land) so you can see coverage.

Usage:
    poetry install
    poetry run python geocode.py "Oversikt-over-Oslo-kommunes-eiendommer-mai-2026_nett-2.xlsx"

Output:
    eiendommer.geojson   <- feed this to index.html
    geocode_cache.sqlite <- coordinate cache (safe to keep / delete)
    missing.csv          <- rows that couldn't be located by address
"""

import os, sys, re, json, time, sqlite3, csv
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd
import requests


def _load_env_file(path="source.env"):
    """Tiny dotenv-style loader. Pre-existing env vars win — set in shell to
    override what's in the file. No expansion, no quoting tricks beyond
    stripping a single layer of "..." or '...'.
    """
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if (len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'")):
            val = val[1:-1]
        os.environ.setdefault(key.strip(), val)


_load_env_file()


GEONORGE = "https://ws.geonorge.no/adresser/v1/sok"
KOMMUNE = os.environ.get("KOMMUNE", "0301")  # default: Oslo
SLEEP = 0.05               # ~20 req/s — polite for Geonorge's free address API
RETRIES = 4

# Kartverket Eiendomskart Teig WFS — parcel polygon fallback when Geonorge
# returns no address (road land, unregistered land). No auth, GML 3.2 only.
TEIG_WFS = "https://wfs.geonorge.no/skwms1/wfs.matrikkelen-eiendomskart-teig"
TEIG_SLEEP = 0.125         # ~8 req/s — no published rate limit, conservative

NS = {
    "wfs": "http://www.opengis.net/wfs/2.0",
    "gml": "http://www.opengis.net/gml/3.2",
    "app": "http://skjema.geonorge.no/SOSI/produktspesifikasjon/"
           "Matrikkelen-Eiendomskart-Teig/20211101",
}

# Comma-separated agencies to include (empty = the full register). See
# source.env for the full set of recognised values and rationale. Whitespace
# around each item is stripped so values can be written tight or spaced.
INCLUDE_EIER = [s.strip() for s in os.environ.get("INCLUDE_EIER", "").split(",")
                if s.strip()]

MATRIKKEL_RE = re.compile(r"^(\d+)-(\d+)/(\d+)/(\d+)/(\d+)$")


def parse_matrikkel(value):
    m = MATRIKKEL_RE.match(str(value).strip())
    if not m:
        return None
    knr, gnr, bnr, fnr, snr = m.groups()
    return {"knr": knr, "gnr": int(gnr), "bnr": int(bnr),
            "fnr": int(fnr), "snr": int(snr)}


def open_cache(path="geocode_cache.sqlite"):
    """Open the SQLite geocode cache. Cache schema v2 decouples the two
    resolvers via separate addr_source / geom_source columns, so we can
    independently track "attempted Geonorge", "got an address", "attempted
    Teig", "got a polygon" — instead of overloading one `source` field.

    Migrates v1 caches in place.
    """
    con = sqlite3.connect(path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS geo (
            gnr INTEGER, bnr INTEGER,
            lat REAL, lon REAL,
            adressetekst TEXT,
            source TEXT,              -- v1 legacy, retained for back-compat
            geometry_json TEXT,
            addr_source TEXT,         -- NULL | 'geonorge_adresse' | 'none'
            geom_source TEXT,         -- NULL | 'kartverket_teig' | 'none'
            PRIMARY KEY (gnr, bnr)
        )
    """)
    cols = {row[1] for row in con.execute("PRAGMA table_info(geo)").fetchall()}
    if "geometry_json" not in cols:
        con.execute("ALTER TABLE geo ADD COLUMN geometry_json TEXT")
    if "addr_source" not in cols:
        con.execute("ALTER TABLE geo ADD COLUMN addr_source TEXT")
    if "geom_source" not in cols:
        con.execute("ALTER TABLE geo ADD COLUMN geom_source TEXT")
    # One-time v1 → v2 backfill of addr_source / geom_source from the legacy
    # `source` field. v1 source values:
    #   'geonorge_adresse' → Geonorge succeeded, Teig never attempted
    #   'kartverket_teig'  → Geonorge attempted+failed, Teig succeeded
    #   'none'             → both attempted, both failed
    con.execute("""
        UPDATE geo SET
            addr_source = CASE
                WHEN source = 'geonorge_adresse' THEN 'geonorge_adresse'
                WHEN source IN ('kartverket_teig', 'none') THEN 'none'
                ELSE addr_source
            END,
            geom_source = CASE
                WHEN source = 'kartverket_teig' THEN 'kartverket_teig'
                WHEN source = 'none' THEN 'none'
                ELSE geom_source
            END
        WHERE addr_source IS NULL OR geom_source IS NULL
    """)
    con.commit()
    return con


def cache_get(con, gnr, bnr):
    """Return (lat, lon, adressetekst, addr_source, geom_source, geometry_json)
    or None.
    """
    row = con.execute(
        "SELECT lat, lon, adressetekst, addr_source, geom_source, geometry_json "
        "FROM geo WHERE gnr=? AND bnr=?",
        (gnr, bnr)).fetchone()
    return row


def cache_put(con, gnr, bnr, *, lat=None, lon=None, adressetekst=None,
              addr_source=None, geom_source=None, geometry_json=None):
    """Insert or replace a cache row. All fields are kwargs to make call sites
    self-documenting."""
    con.execute(
        "INSERT OR REPLACE INTO geo "
        "(gnr, bnr, lat, lon, adressetekst, addr_source, geom_source, geometry_json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (gnr, bnr, lat, lon, adressetekst, addr_source, geom_source, geometry_json))
    con.commit()


def _parse_poslist(text):
    """GML posList 'lat1 lon1 lat2 lon2 …' → GeoJSON ring [[lon, lat], …]."""
    nums = text.split()
    return [[float(nums[i + 1]), float(nums[i])] for i in range(0, len(nums), 2)]


def parse_teig_response(xml_bytes):
    """Parse the WFS GetFeature response → GeoJSON geometry, or None.

    A single (gnr, bnr) can map to multiple `app:Teig` features — common for
    road land. We return a MultiPolygon when there are several teiger, a
    Polygon for a single teig, or None when the response is empty.
    """
    root = ET.fromstring(xml_bytes)
    polygons = []
    for member in root.findall("wfs:member", NS):
        teig = member.find("app:Teig", NS)
        if teig is None:
            continue
        # Each Teig has one geometry under app:område → gml:Polygon.
        polygon = teig.find(".//{%s}Polygon" % NS["gml"])
        if polygon is None:
            continue
        rings = []
        ext = polygon.find("gml:exterior/gml:LinearRing/gml:posList", NS)
        if ext is None or not ext.text:
            continue
        rings.append(_parse_poslist(ext.text))
        for hole in polygon.findall("gml:interior/gml:LinearRing/gml:posList", NS):
            if hole.text:
                rings.append(_parse_poslist(hole.text))
        polygons.append(rings)
    if not polygons:
        return None
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


def kartverket_teig_lookup(gnr, bnr, session):
    """Return GeoJSON geometry (Polygon/MultiPolygon) or None."""
    filter_xml = (
        '<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0" '
        'xmlns:app="' + NS["app"] + '">'
        '<fes:And>'
        '<fes:PropertyIsEqualTo>'
        '<fes:ValueReference>app:kommunenummer</fes:ValueReference>'
        f'<fes:Literal>{KOMMUNE}</fes:Literal>'
        '</fes:PropertyIsEqualTo>'
        '<fes:PropertyIsEqualTo>'
        '<fes:ValueReference>app:matrikkelnummerTekst</fes:ValueReference>'
        f'<fes:Literal>{gnr}/{bnr}</fes:Literal>'
        '</fes:PropertyIsEqualTo>'
        '</fes:And></fes:Filter>'
    )
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": "app:Teig",
        "srsName": "urn:ogc:def:crs:EPSG::4326",
        "count": "200",
        "filter": filter_xml,
    }
    for attempt in range(RETRIES):
        try:
            r = session.get(TEIG_WFS, params=params, timeout=30)
            if r.status_code == 200:
                try:
                    return parse_teig_response(r.content)
                except ET.ParseError:
                    return None
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
    return None


def geonorge_lookup(gnr, bnr, session):
    """Return (lat, lon, adressetekst) or (None, None, None)."""
    params = {
        "kommunenummer": KOMMUNE,
        "gardsnummer": gnr,
        "bruksnummer": bnr,
        "treffPerSide": 100,
        "utkoordsys": 4326,   # WGS84 lat/lon
    }
    for attempt in range(RETRIES):
        try:
            r = session.get(GEONORGE, params=params, timeout=20)
            if r.status_code == 200:
                addrs = r.json().get("adresser", [])
                if not addrs:
                    return (None, None, None)
                # Average the representation points of all matching addresses
                # so a large multi-address parcel lands near its centre.
                pts = [a["representasjonspunkt"] for a in addrs
                       if a.get("representasjonspunkt")]
                if not pts:
                    return (None, None, None)
                lat = sum(p["lat"] for p in pts) / len(pts)
                lon = sum(p["lon"] for p in pts) / len(pts)
                txt = addrs[0].get("adressetekst") or ""
                return (lat, lon, txt)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            return (None, None, None)
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
    return (None, None, None)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    xlsx = Path(sys.argv[1])
    df = pd.read_excel(xlsx)

    cols = {
        "eiendom": "Eiendom",
        "adresse": "Adresse",
        "bydel": "Bydel",
        "eier": "Eiers/festers kontaktinstans",
        "bruksnavn": "Bruksnavn",
        "areal": "Beregnet areal (m²)",
    }

    if INCLUDE_EIER:
        before = len(df)
        # .str.strip() defends against the hygiene bug seen in the April 2025
        # release (139 rows had a leading space, e.g. " Boligbygg Oslo KF").
        df = df[df[cols["eier"]].str.strip().isin(INCLUDE_EIER)].reset_index(drop=True)
        print(f"Filtered to {INCLUDE_EIER}: {len(df)} of {before} rows.")

    parsed = df[cols["eiendom"]].map(parse_matrikkel)
    pairs = sorted({(p["gnr"], p["bnr"]) for p in parsed if p})
    print(f"{len(df)} rows, {len(pairs)} unique (gnr,bnr) pairs to resolve.")

    con = open_cache()
    session = requests.Session()
    session.headers.update({
        "User-Agent": "kombo/1.0 (+https://github.com/deviationist/kombo)",
    })

    done = 0
    addr_hits = poly_hits = both = neither = 0
    for gnr, bnr in pairs:
        row = cache_get(con, gnr, bnr) or (None,) * 6
        lat, lon, txt, addr_src, geom_src, geom_json = row

        # 1. Geonorge address lookup — only if never attempted for this pair.
        if addr_src is None:
            lat, lon, txt = geonorge_lookup(gnr, bnr, session)
            addr_src = "geonorge_adresse" if lat is not None else "none"
            cache_put(con, gnr, bnr,
                      lat=lat, lon=lon, adressetekst=txt,
                      addr_source=addr_src, geom_source=geom_src,
                      geometry_json=geom_json)
            time.sleep(SLEEP)

        # 2. Kartverket Teig WFS — also always attempted now, so the user can
        # see the parcel outline even for addressed properties. Skipped only
        # if we've previously tried for this pair.
        if geom_src is None:
            geom = kartverket_teig_lookup(gnr, bnr, session)
            geom_src = "kartverket_teig" if geom is not None else "none"
            geom_json = json.dumps(geom) if geom is not None else None
            cache_put(con, gnr, bnr,
                      lat=lat, lon=lon, adressetekst=txt,
                      addr_source=addr_src, geom_source=geom_src,
                      geometry_json=geom_json)
            time.sleep(TEIG_SLEEP)

        has_addr = addr_src == "geonorge_adresse"
        has_poly = geom_src == "kartverket_teig"
        if has_addr and has_poly: both += 1
        elif has_addr:            addr_hits += 1
        elif has_poly:            poly_hits += 1
        else:                     neither += 1
        done += 1
        if done % 250 == 0:
            print(f"  {done}/{len(pairs)} resolved "
                  f"(both {both}, addr-only {addr_hits}, poly-only {poly_hits}, "
                  f"none {neither})...")

    located = both + addr_hits + poly_hits
    print(f"Done. {located}/{len(pairs)} pairs located "
          f"({located/len(pairs)*100:.0f}%) — "
          f"{both} both, {addr_hits} address only, {poly_hits} polygon only, "
          f"{neither} neither.")

    # Build GeoJSON + missing list.
    # Prefer Polygon geometry when available (shows the actual parcel shape);
    # fall back to Point at the Geonorge address representation point. When
    # both are present, the marker position for the map's cluster layer is
    # carried in props.center so the client can render a centroid marker on
    # top of the polygon without recomputing it.
    features, missing = [], []
    for i, p in enumerate(parsed):
        r = df.iloc[i]
        eiendom = str(r[cols["eiendom"]]).strip()
        areal = r[cols["areal"]]
        props = {
            "eiendom": eiendom,
            "adresse": ("" if pd.isna(r[cols["adresse"]]) else str(r[cols["adresse"]]).strip()),
            "bydel": ("" if pd.isna(r[cols["bydel"]]) else str(r[cols["bydel"]]).strip()),
            "eier": str(r[cols["eier"]]).strip(),
            "bruksnavn": ("" if pd.isna(r[cols["bruksnavn"]]) else str(r[cols["bruksnavn"]]).strip()),
            "areal": (None if pd.isna(areal) else float(areal)),
        }
        row = cache_get(con, p["gnr"], p["bnr"]) if p else None
        if not row:
            missing.append(props); continue
        lat, lon, _adr, addr_src, geom_src, geom_json = row
        has_poly = geom_src == "kartverket_teig" and geom_json
        has_addr = addr_src == "geonorge_adresse" and lat is not None

        if has_poly:
            geom = json.loads(geom_json)
            # Marker anchor: prefer the Geonorge address point (semantically
            # "the parcel's front door"); else first vertex of the polygon.
            if has_addr:
                props["center"] = [lon, lat]
            features.append({"type": "Feature", "geometry": geom, "properties": props})
        elif has_addr:
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": props,
            })
        else:
            missing.append(props)

    fc = {"type": "FeatureCollection", "features": features}
    Path("eiendommer.geojson").write_text(
        json.dumps(fc, ensure_ascii=False), encoding="utf-8")

    with open("missing.csv", "w", newline="", encoding="utf-8") as f:
        # Use the row schema without the per-feature "source" we add above.
        sample_props = {k: None for k in
                        ("eiendom", "adresse", "bydel", "eier", "bruksnavn", "areal")}
        w = csv.DictWriter(f, fieldnames=list(sample_props.keys()))
        w.writeheader()
        for m in missing:
            w.writerow({k: m.get(k) for k in sample_props})

    n_pts  = sum(1 for f in features if f["geometry"]["type"] == "Point")
    n_poly = len(features) - n_pts
    print(f"Wrote eiendommer.geojson "
          f"({len(features)} located rows — {n_pts} points, {n_poly} polygons).")
    print(f"Wrote missing.csv ({len(missing)} rows without geometry).")


if __name__ == "__main__":
    main()
