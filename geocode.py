#!/usr/bin/env python3
"""
geocode.py - Turn the Oslo municipal property XLSX into map-ready GeoJSON.

Each row's "Eiendom" is a Norwegian matrikkelnummer: KNR-GNR/BNR/FNR/SNR.
KNR is the kommunenummer — '0301' for the in-Oslo sheet, the host municipality
for the "Utenbys eiendommer" sheet (Oslo-owned marka + waterworks land in ~20
other kommuner). We resolve coordinates from Kartverket/Geonorge's open APIs
(no API key, WGS84 lat/lon out of the box). The pipeline is structurally
multi-kommune; Oslo is just the currently-configured source (see KOMMUNE).

Strategy:
  1. Parse the matrikkel into (knr, gnr, bnr) for every row of both sheets.
  2. Deduplicate to unique (knr, gnr, bnr) parcels before hitting the network.
  3. Query the resolvers once per unique parcel, cache every result in SQLite
     so re-runs are free and resumable.
  4. Join coordinates back to every row, write eiendommer.geojson (utenbys rows
     flagged so the map can offer them as a toggleable layer).
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

import os, sys, re, json, time, sqlite3, csv, threading
from concurrent.futures import ThreadPoolExecutor
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
# Home kommune this build targets. The geocoders no longer use it — each parcel
# carries its own knr (so the out-of-kommune "Utenbys" sheet resolves against
# its host municipality, and the tool is structurally multi-kommune). KOMMUNE
# stays as the documented source knob (source.env / CI) identifying which
# kommune's register this run is for; default Oslo.
KOMMUNE = os.environ.get("KOMMUNE", "0301")
SLEEP = 0.05               # per-thread; combined fleet stays polite (see WORKERS)
RETRIES = 4

# Kartverket Eiendomskart Teig WFS — parcel polygon fallback when Geonorge
# returns no address (road land, unregistered land). No auth, GML 3.2 only.
TEIG_WFS = "https://wfs.geonorge.no/skwms1/wfs.matrikkelen-eiendomskart-teig"
TEIG_SLEEP = 0.125         # per-thread; see WORKERS for combined req/s

# Concurrency. Three workers tripled throughput in testing without rate-
# limit pushback from either Geonorge or Kartverket — combined load is ~3 req/s
# per resolver, well inside "polite for a free public service" territory.
WORKERS = int(os.environ.get("WORKERS", "3"))

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
    """Open the SQLite geocode cache. Cache schema v4 keys on (knr, gnr, bnr)
    so parcels in *other* kommuner (the "Utenbys eiendommer" sheet — Oslo's
    marka + waterworks land in ~20 neighbouring municipalities) can't collide
    with Oslo's (gnr, bnr) namespace. v3 added postnummer + poststed; v2 the
    addr_source / geom_source split. Migrates v1/v2/v3 caches in place — a v3
    cache is all-Oslo, so its rows backfill to knr='0301'.
    """
    # check_same_thread=False lets the worker pool share the connection;
    # we serialise writes through _cache_lock so SQLite never sees overlapping
    # statements on the same connection.
    con = sqlite3.connect(path, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS geo (
            knr TEXT,                 -- kommunenummer ('0301' for Oslo)
            gnr INTEGER, bnr INTEGER,
            lat REAL, lon REAL,
            adressetekst TEXT,
            source TEXT,              -- v1 legacy, retained for back-compat
            geometry_json TEXT,
            addr_source TEXT,         -- NULL | 'geonorge_adresse' | 'none'
            geom_source TEXT,         -- NULL | 'kartverket_teig' | 'none'
            postnummer TEXT,          -- 4-digit ZIP, from Geonorge
            poststed TEXT,            -- postal area name, from Geonorge
            PRIMARY KEY (knr, gnr, bnr)
        )
    """)
    cols = {row[1] for row in con.execute("PRAGMA table_info(geo)").fetchall()}
    if "geometry_json" not in cols:
        con.execute("ALTER TABLE geo ADD COLUMN geometry_json TEXT")
    if "addr_source" not in cols:
        con.execute("ALTER TABLE geo ADD COLUMN addr_source TEXT")
    if "geom_source" not in cols:
        con.execute("ALTER TABLE geo ADD COLUMN geom_source TEXT")
    if "postnummer" not in cols:
        con.execute("ALTER TABLE geo ADD COLUMN postnummer TEXT")
    if "poststed" not in cols:
        con.execute("ALTER TABLE geo ADD COLUMN poststed TEXT")
    # v3 → v4: introduce knr. SQLite can't ALTER a PRIMARY KEY, so add the
    # column (existing rows are all Oslo → '0301') and rebuild the table with
    # the composite PK. Done once; thereafter "knr" is present and we skip it.
    if "knr" not in cols:
        con.execute("ALTER TABLE geo ADD COLUMN knr TEXT")
        con.execute("UPDATE geo SET knr='0301' WHERE knr IS NULL")
        con.executescript("""
            CREATE TABLE geo_v4 (
                knr TEXT, gnr INTEGER, bnr INTEGER,
                lat REAL, lon REAL, adressetekst TEXT, source TEXT,
                geometry_json TEXT, addr_source TEXT, geom_source TEXT,
                postnummer TEXT, poststed TEXT,
                PRIMARY KEY (knr, gnr, bnr)
            );
            INSERT INTO geo_v4
                SELECT knr, gnr, bnr, lat, lon, adressetekst, source,
                       geometry_json, addr_source, geom_source, postnummer, poststed
                FROM geo;
            DROP TABLE geo;
            ALTER TABLE geo_v4 RENAME TO geo;
        """)
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


_cache_lock = threading.Lock()


def cache_get(con, knr, gnr, bnr):
    """Return (lat, lon, adressetekst, addr_source, geom_source, geometry_json,
    postnummer, poststed) or None.
    """
    with _cache_lock:
        row = con.execute(
            "SELECT lat, lon, adressetekst, addr_source, geom_source, geometry_json, "
            "       postnummer, poststed "
            "FROM geo WHERE knr=? AND gnr=? AND bnr=?",
            (knr, gnr, bnr)).fetchone()
    return row


def cache_put(con, knr, gnr, bnr, *, lat=None, lon=None, adressetekst=None,
              addr_source=None, geom_source=None, geometry_json=None,
              postnummer=None, poststed=None):
    """Insert or replace a cache row. All fields are kwargs to make call sites
    self-documenting."""
    with _cache_lock:
        con.execute(
            "INSERT OR REPLACE INTO geo "
            "(knr, gnr, bnr, lat, lon, adressetekst, addr_source, geom_source, "
            " geometry_json, postnummer, poststed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (knr, gnr, bnr, lat, lon, adressetekst, addr_source, geom_source,
             geometry_json, postnummer, poststed))
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


def kartverket_teig_lookup(knr, gnr, bnr, session):
    """Return GeoJSON geometry (Polygon/MultiPolygon) or None.

    `knr` is the parcel's own kommunenummer — '0301' for the Oslo sheet, or
    the host municipality's number for "Utenbys eiendommer" (e.g. '3203'
    Asker). The Teig WFS is national, so a correct knr is all that's needed.
    """
    filter_xml = (
        '<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0" '
        'xmlns:app="' + NS["app"] + '">'
        '<fes:And>'
        '<fes:PropertyIsEqualTo>'
        '<fes:ValueReference>app:kommunenummer</fes:ValueReference>'
        f'<fes:Literal>{knr}</fes:Literal>'
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


def geonorge_lookup(knr, gnr, bnr, session):
    """Return (lat, lon, adressetekst, postnummer, poststed) or 5×None.

    `knr` is the parcel's own kommunenummer (see kartverket_teig_lookup)."""
    NONE_TUPLE = (None, None, None, None, None)
    params = {
        "kommunenummer": knr,
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
                    return NONE_TUPLE
                # Average the representation points of all matching addresses
                # so a large multi-address parcel lands near its centre.
                pts = [a["representasjonspunkt"] for a in addrs
                       if a.get("representasjonspunkt")]
                if not pts:
                    return NONE_TUPLE
                lat = sum(p["lat"] for p in pts) / len(pts)
                lon = sum(p["lon"] for p in pts) / len(pts)
                # Use the first address for the human-readable fields. All
                # matches for a (gnr, bnr) usually share the same postal area;
                # in the rare cases they don't, "first" is good enough.
                first = addrs[0]
                txt = first.get("adressetekst") or ""
                postnummer = first.get("postnummer") or None
                poststed   = first.get("poststed") or None
                return (lat, lon, txt, postnummer, poststed)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            return NONE_TUPLE
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
    return NONE_TUPLE


def _txt(r, col):
    """A trimmed string cell, or "" for missing/NaN. The kommune uses "·" as
    an empty-address placeholder in the utenbys sheet — treat it as blank."""
    if not col or col not in r or pd.isna(r[col]):
        return ""
    s = str(r[col]).strip()
    return "" if s == "·" else s


def _geom_anchor(geom):
    """[lon, lat] of the first exterior vertex of a (Multi)Polygon, or None.
    Used to give an out-of-Oslo parcel a marker position when it has no
    registered address (most of them) — mirrors the client's geomAnchor()."""
    if not geom:
        return None
    coords = geom.get("coordinates")
    try:
        if geom.get("type") == "Polygon":
            return list(coords[0][0])
        if geom.get("type") == "MultiPolygon":
            return list(coords[0][0][0])
    except (IndexError, TypeError):
        return None
    return None


# Sheet schemas. Sheet 1 is the in-Oslo register; sheet 2 ("Utenbys
# eiendommer") is Oslo-owned land in OTHER kommuner — a different column
# layout with no bydel/bruksnavn/areal, but its own `Kommune` per row. The
# pipeline is structurally multi-kommune (parcels carry their own knr); Oslo
# is just the currently-configured source.
OSLO_SHEET = "Eiendommer i Oslo kommune"
UTENBYS_SHEET = "Utenbys eiendommer"
OSLO_SCHEMA = {
    "eiendom": "Eiendom", "adresse": "Adresse", "bydel": "Bydel",
    "eier": "Eiers/festers kontaktinstans", "bruksnavn": "Bruksnavn",
    "areal": "Beregnet areal (m²)",
}
UTENBYS_SCHEMA = {
    "eiendom": "Eiendom", "adresse": "Adresse",
    "eier": "Eiers kontaktinstans", "kommune": "Kommune",
    "matrikkelenhetstype": "Matrikkelenhets\ntype",
}


def _build_records(df, schema, *, utenbys):
    """Turn a sheet DataFrame into [(parsed_matrikkel | None, props), ...].
    `props` is the GeoJSON feature property bag; geometry is joined later."""
    recs = []
    for _, r in df.iterrows():
        p = parse_matrikkel(r[schema["eiendom"]])
        props = {
            "eiendom": str(r[schema["eiendom"]]).strip(),
            "adresse": _txt(r, schema.get("adresse")),
            "bydel": _txt(r, schema.get("bydel")),
            "eier": _txt(r, schema.get("eier")),
            "bruksnavn": _txt(r, schema.get("bruksnavn")),
            "areal": (None if not schema.get("areal") or pd.isna(r[schema["areal"]])
                      else float(r[schema["areal"]])),
            "utenbys": utenbys,
        }
        if utenbys:
            props["kommune"] = _txt(r, schema.get("kommune"))
            mt = _txt(r, schema.get("matrikkelenhetstype"))
            if mt:
                props["matrikkelenhetstype"] = mt
        recs.append((p, props))
    return recs


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    xlsx = Path(sys.argv[1])

    # Sheet 1 — the in-kommune register (the main dataset).
    df_oslo = pd.read_excel(xlsx, sheet_name=0)
    # Sheet 2 — Oslo-owned parcels in other kommuner. Optional: a future
    # source spreadsheet may not ship it, so degrade gracefully rather than
    # hard-fail (keeps the tool usable for kommuner without an utenbys list).
    try:
        df_utb = pd.read_excel(xlsx, sheet_name=UTENBYS_SHEET)
    except (ValueError, KeyError):
        df_utb = None
        print(f"No '{UTENBYS_SHEET}' sheet found — processing in-kommune rows only.")

    def _filter_eier(df, eier_col, label):
        if not INCLUDE_EIER or df is None:
            return df
        before = len(df)
        # .str.strip() defends against the hygiene bug seen in the April 2025
        # release (139 rows had a leading space, e.g. " Boligbygg Oslo KF").
        df = df[df[eier_col].str.strip().isin(INCLUDE_EIER)].reset_index(drop=True)
        print(f"Filtered {label} to {INCLUDE_EIER}: {len(df)} of {before} rows.")
        return df

    df_oslo = _filter_eier(df_oslo, OSLO_SCHEMA["eier"], "Oslo")
    df_utb = _filter_eier(df_utb, UTENBYS_SCHEMA["eier"], "utenbys")

    records = _build_records(df_oslo, OSLO_SCHEMA, utenbys=False)
    if df_utb is not None:
        records += _build_records(df_utb, UTENBYS_SCHEMA, utenbys=True)

    # Deduplicate to unique (knr, gnr, bnr) triples before hitting the network.
    triples = sorted({(p["knr"], p["gnr"], p["bnr"]) for p, _ in records if p})
    n_utb = sum(1 for _, pr in records if pr["utenbys"])
    print(f"{len(records)} rows ({len(records)-n_utb} in-kommune + {n_utb} utenbys), "
          f"{len(triples)} unique (knr,gnr,bnr) parcels to resolve.")

    con = open_cache()
    session = requests.Session()
    session.headers.update({
        "User-Agent": "kombo/1.0 (+https://github.com/deviationist/kombo)",
    })

    counts = {"both": 0, "addr": 0, "poly": 0, "none": 0, "done": 0}
    counts_lock = threading.Lock()
    total = len(triples)

    def process(triple):
        knr, gnr, bnr = triple
        row = cache_get(con, knr, gnr, bnr) or (None,) * 8
        lat, lon, txt, addr_src, geom_src, geom_json, postnr, poststed = row

        # 1. Geonorge address lookup — call if we've never tried, OR if we
        # have an addressed cache row from before postnummer/poststed were
        # captured (v2 → v3 backfill, no extra row state needed).
        if addr_src is None or (addr_src == "geonorge_adresse" and postnr is None):
            lat, lon, txt, postnr, poststed = geonorge_lookup(knr, gnr, bnr, session)
            addr_src = "geonorge_adresse" if lat is not None else "none"
            cache_put(con, knr, gnr, bnr,
                      lat=lat, lon=lon, adressetekst=txt,
                      addr_source=addr_src, geom_source=geom_src,
                      geometry_json=geom_json,
                      postnummer=postnr, poststed=poststed)
            time.sleep(SLEEP)

        # 2. Kartverket Teig WFS — also always attempted now, so the user can
        # see the parcel outline even for addressed properties. Skipped only
        # if we've previously tried for this parcel.
        if geom_src is None:
            geom = kartverket_teig_lookup(knr, gnr, bnr, session)
            geom_src = "kartverket_teig" if geom is not None else "none"
            geom_json = json.dumps(geom) if geom is not None else None
            cache_put(con, knr, gnr, bnr,
                      lat=lat, lon=lon, adressetekst=txt,
                      addr_source=addr_src, geom_source=geom_src,
                      geometry_json=geom_json,
                      postnummer=postnr, poststed=poststed)
            time.sleep(TEIG_SLEEP)

        has_addr = addr_src == "geonorge_adresse"
        has_poly = geom_src == "kartverket_teig"
        with counts_lock:
            if has_addr and has_poly: counts["both"] += 1
            elif has_addr:            counts["addr"] += 1
            elif has_poly:            counts["poly"] += 1
            else:                     counts["none"] += 1
            counts["done"] += 1
            d = counts["done"]
            if d % 250 == 0:
                print(f"  {d}/{total} resolved "
                      f"(both {counts['both']}, addr-only {counts['addr']}, "
                      f"poly-only {counts['poly']}, none {counts['none']})...",
                      flush=True)

    print(f"Running with {WORKERS} worker thread(s).", flush=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        # list() forces the generator to drain so we wait for everything.
        list(ex.map(process, triples))

    addr_hits, poly_hits = counts["addr"], counts["poly"]
    both, neither = counts["both"], counts["none"]

    located = both + addr_hits + poly_hits
    print(f"Done. {located}/{len(triples)} parcels located "
          f"({located/max(1,len(triples))*100:.0f}%) — "
          f"{both} both, {addr_hits} address only, {poly_hits} polygon only, "
          f"{neither} neither.")

    # Build GeoJSON + missing list.
    # Prefer Polygon geometry when available (shows the actual parcel shape);
    # fall back to Point at the Geonorge address representation point. When
    # both are present, the marker position for the map's cluster layer is
    # carried in props.center so the client can render a centroid marker on
    # top of the polygon without recomputing it.
    features, missing = [], []
    for p, props in records:
        row = cache_get(con, p["knr"], p["gnr"], p["bnr"]) if p else None
        if not row:
            missing.append(props); continue
        lat, lon, _adr, addr_src, geom_src, geom_json, postnr, poststed = row
        has_poly = geom_src == "kartverket_teig" and geom_json
        has_addr = addr_src == "geonorge_adresse" and lat is not None

        # The XLSX `Adresse` column is the canonical per-row text but is
        # often blank for eierseksjoner (e.g. Casparis gate 4 has 50 flats
        # listed without an address each). Fall back to the Geonorge
        # adressetekst on the parcel so the popup + featureTitle have
        # something other than "Eiendom <gnr>/<bnr>" to show.
        if not props["adresse"] and _adr:
            props["adresse"] = _adr

        # Carry the full Geonorge-resolved address into every feature when
        # we have one — the popup can format it as "Lilleakerveien 49,
        # 0284 OSLO" without doing a second lookup.
        if postnr:
            props["postnummer"] = postnr
        if poststed:
            props["poststed"] = poststed

        if has_poly:
            geom = json.loads(geom_json)
            # Marker anchor: prefer the Geonorge address point (semantically
            # "the parcel's front door"). For utenbys parcels — almost none
            # of which have a registered address — fall back to a polygon
            # vertex so the layer still drops a clusterable marker instead of
            # an outline-only feature that's invisible until you zoom in.
            if has_addr:
                props["center"] = [lon, lat]
            elif props["utenbys"]:
                a = _geom_anchor(geom)
                if a:
                    props["center"] = a
            features.append({"type": "Feature", "geometry": geom, "properties": props})
        elif has_addr:
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": props,
            })
        else:
            missing.append(props)

    # Extract the kommune-published vintage from the XLSX filename (e.g.
    # "Oversikt-over-Oslo-kommunes-eiendommer-mai-2026_nett-2.xlsx" →
    # "mai 2026") so the map header can show it without anyone hand-editing
    # index.html every six months when the kommune republishes.
    MONTHS_NO = ("januar", "februar", "mars", "april", "mai", "juni",
                 "juli", "august", "september", "oktober", "november", "desember")
    vintage = None
    m = re.search(
        r"(" + "|".join(MONTHS_NO) + r")[-_ ]?(\d{4})",
        xlsx.name, flags=re.IGNORECASE)
    if m:
        vintage = f"{m.group(1).lower()} {m.group(2)}"

    # Partition Oslo vs. utenbys so the metadata keeps the in-kommune counts
    # (what the map's intro copy refers to) separate from the out-of-kommune
    # layer the client toggles on demand.
    def _is_utb(props):  return bool(props.get("utenbys"))
    oslo_feat = [f for f in features if not _is_utb(f["properties"])]
    utb_feat  = [f for f in features if _is_utb(f["properties"])]
    oslo_miss = [m for m in missing if not _is_utb(m)]
    utb_miss  = [m for m in missing if _is_utb(m)]

    fc = {
        "type": "FeatureCollection",
        "metadata": {
            "vintage": vintage,           # e.g. "mai 2026", or null if not found
            "sourceFile": xlsx.name,
            "generatedAt": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds"),
            "totalRows": len(oslo_feat) + len(oslo_miss),   # in-kommune rows present
            "located": len(oslo_feat),
            "utenbysRows": len(utb_feat) + len(utb_miss),   # out-of-kommune rows present
            "utenbysLocated": len(utb_feat),
        },
        "features": features,
    }
    Path("eiendommer.geojson").write_text(
        json.dumps(fc, ensure_ascii=False), encoding="utf-8")

    with open("missing.csv", "w", newline="", encoding="utf-8") as f:
        # Common columns across both sheets; `kommune` is blank for in-Oslo
        # rows and filled for utenbys ones, so the two are distinguishable.
        fields = ("eiendom", "adresse", "bydel", "eier", "bruksnavn", "areal", "kommune")
        w = csv.DictWriter(f, fieldnames=list(fields))
        w.writeheader()
        for m in missing:
            w.writerow({k: m.get(k) for k in fields})

    n_pts  = sum(1 for f in features if f["geometry"]["type"] == "Point")
    n_poly = len(features) - n_pts
    print(f"Wrote eiendommer.geojson "
          f"({len(features)} located rows — {n_pts} points, {n_poly} polygons; "
          f"{len(utb_feat)} utenbys).")
    print(f"Wrote missing.csv ({len(missing)} rows without geometry).")


if __name__ == "__main__":
    main()
