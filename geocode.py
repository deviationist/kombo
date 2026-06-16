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

import sys, re, json, time, sqlite3, csv
from pathlib import Path

import pandas as pd
import requests

GEONORGE = "https://ws.geonorge.no/adresser/v1/sok"
KOMMUNE = "0301"            # Oslo
SLEEP = 0.05               # be polite to a free public service
RETRIES = 4

# Restrict to specific owning agencies. Empty list [] = all 7,022 properties.
# "Boligbygg Oslo KF" is the municipal *housing* stock — where people live.
# To also include municipal care/senior homes, add "Oslobygg KF" and rely on
# the bruksnavn filter below, or just list the agencies you want here.
INCLUDE_EIER = ["Boligbygg Oslo KF"]

MATRIKKEL_RE = re.compile(r"^(\d+)-(\d+)/(\d+)/(\d+)/(\d+)$")


def parse_matrikkel(value):
    m = MATRIKKEL_RE.match(str(value).strip())
    if not m:
        return None
    knr, gnr, bnr, fnr, snr = m.groups()
    return {"knr": knr, "gnr": int(gnr), "bnr": int(bnr),
            "fnr": int(fnr), "snr": int(snr)}


def open_cache(path="geocode_cache.sqlite"):
    con = sqlite3.connect(path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS geo (
            gnr INTEGER, bnr INTEGER,
            lat REAL, lon REAL,
            adressetekst TEXT, source TEXT,
            PRIMARY KEY (gnr, bnr)
        )
    """)
    con.commit()
    return con


def cache_get(con, gnr, bnr):
    row = con.execute(
        "SELECT lat, lon, adressetekst, source FROM geo WHERE gnr=? AND bnr=?",
        (gnr, bnr)).fetchone()
    return row  # None, or (lat, lon, adressetekst, source)


def cache_put(con, gnr, bnr, lat, lon, adressetekst, source):
    con.execute(
        "INSERT OR REPLACE INTO geo VALUES (?,?,?,?,?,?)",
        (gnr, bnr, lat, lon, adressetekst, source))
    con.commit()


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
        df = df[df[cols["eier"]].isin(INCLUDE_EIER)].reset_index(drop=True)
        print(f"Filtered to {INCLUDE_EIER}: {len(df)} of {before} rows.")

    parsed = df[cols["eiendom"]].map(parse_matrikkel)
    pairs = sorted({(p["gnr"], p["bnr"]) for p in parsed if p})
    print(f"{len(df)} rows, {len(pairs)} unique (gnr,bnr) pairs to resolve.")

    con = open_cache()
    session = requests.Session()
    session.headers.update({"User-Agent": "oslo-eiendom-map/1.0"})

    done = found = 0
    for gnr, bnr in pairs:
        if cache_get(con, gnr, bnr) is None:
            lat, lon, txt = geonorge_lookup(gnr, bnr, session)
            src = "geonorge_adresse" if lat is not None else "none"
            cache_put(con, gnr, bnr, lat, lon, txt, src)
            time.sleep(SLEEP)
        row = cache_get(con, gnr, bnr)
        if row and row[0] is not None:
            found += 1
        done += 1
        if done % 250 == 0:
            print(f"  {done}/{len(pairs)} resolved, {found} located...")

    print(f"Done. {found}/{len(pairs)} pairs located by address "
          f"({found/len(pairs)*100:.0f}%).")

    # Build GeoJSON + missing list
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
        coord = cache_get(con, p["gnr"], p["bnr"]) if p else None
        if coord and coord[0] is not None:
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [coord[1], coord[0]]},
                "properties": props,
            })
        else:
            missing.append(props)

    fc = {"type": "FeatureCollection", "features": features}
    Path("eiendommer.geojson").write_text(
        json.dumps(fc, ensure_ascii=False), encoding="utf-8")

    with open("missing.csv", "w", newline="", encoding="utf-8") as f:
        sample = features[0]["properties"] if features else (missing[0] if missing else None)
        if sample:
            w = csv.DictWriter(f, fieldnames=list(sample.keys()))
            w.writeheader()
            for m in missing:
                w.writerow(m)

    print(f"Wrote eiendommer.geojson ({len(features)} located rows).")
    print(f"Wrote missing.csv ({len(missing)} rows without a located address).")
    print("\nNote: most 'missing' rows are road land (veigrunn) and unregistered")
    print("land that simply have no street address. To map those too, resolve")
    print("them against the cadastral parcel geometry (Kartverket 'Matrikkelen –")
    print("Eiendomskart Teig' WFS) keyed on the matrikkelnummer — see README.md.")


if __name__ == "__main__":
    main()
