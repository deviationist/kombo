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
    eiendommer.geojson   <- the geocoded data the map reads
    index.html           <- the map (regenerated every run from this script)
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

MATRIKKEL_RE = re.compile(r"^(\d+)-(\d+)/(\d+)/(\d+)/(\d+)$")


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="no">
<head>
<meta charset="utf-8">
<title>Oslo kommunes eiendommer</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css">
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css">
<style>
  html, body { margin:0; height:100%; font: 13px/1.4 system-ui, -apple-system, Segoe UI, sans-serif; background:#111; color:#ddd; }
  #app { display: flex; height: 100vh; }
  #side { width: 320px; padding: 14px; overflow:auto; box-sizing:border-box; background:#1a1a1a; border-right:1px solid #2a2a2a; }
  #map { flex: 1; }
  h1 { margin: 0 0 4px; font-size: 16px; }
  .sub { color:#888; margin-bottom: 14px; font-size: 12px; }
  fieldset { border:1px solid #2a2a2a; border-radius:6px; padding:8px 10px; margin: 0 0 12px; }
  legend { padding: 0 4px; color:#aaa; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
  label { display: block; padding: 3px 0; cursor: pointer; }
  label.row { display: flex; align-items: center; gap: 8px; }
  .swatch { width: 12px; height: 12px; border-radius: 2px; display: inline-block; flex: 0 0 12px; }
  select { width: 100%; padding: 5px; background: #222; color:#ddd; border: 1px solid #333; border-radius: 4px; }
  .stat { display:flex; justify-content: space-between; padding: 2px 0; }
  .bar { background: #222; height: 8px; border-radius: 3px; margin: 2px 0 6px; overflow:hidden; }
  .bar > div { height: 100%; }
  .name { color:#ccc; font-size: 12px; }
  .val { color:#999; font-size: 11px; white-space: nowrap; }
  .leaflet-container { background:#111; }
  .leaflet-popup-content-wrapper, .leaflet-popup-tip { background:#1a1a1a; color:#ddd; }
</style>
</head>
<body>
<div id="app">
  <div id="side">
    <h1>Oslo kommunes eiendommer</h1>
    <div class="sub" id="loadstatus">Laster …</div>

    <fieldset>
      <legend>Visning</legend>
      <label class="row"><input type="radio" name="view" value="points" checked> Punkter (clustered)</label>
      <label class="row"><input type="radio" name="view" value="density"> Tetthet</label>
      <label class="row"><input type="radio" name="view" value="areal"> Areal-vekt</label>
    </fieldset>

    <fieldset id="owners">
      <legend>Eier / fester</legend>
    </fieldset>

    <fieldset>
      <legend>Bydel</legend>
      <select id="bydel"><option value="">— alle —</option></select>
    </fieldset>

    <fieldset>
      <legend>Utvalg</legend>
      <div id="stats"></div>
    </fieldset>
  </div>
  <div id="map"></div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
<script>
const OWNERS = {
  "Eiendoms- og byfornyelsesetaten": "#f59e0b",
  "Oslobygg KF":                     "#3b82f6",
  "Boligbygg Oslo KF":               "#10b981",
  "Oslo Havn KF":                    "#06b6d4",
};

const SAMPLE = {
  type: "FeatureCollection",
  features: [
    {type:"Feature", geometry:{type:"Point", coordinates:[10.7522, 59.9139]}, properties:{eiendom:"0301-208/1/0/0",  adresse:"Karl Johans gate", bydel:"St. Hanshaugen", eier:"Eiendoms- og byfornyelsesetaten", bruksnavn:"",                  areal: 3200}},
    {type:"Feature", geometry:{type:"Point", coordinates:[10.7400, 59.9270]}, properties:{eiendom:"0301-220/15/0/0", adresse:"Bislett",          bydel:"St. Hanshaugen", eier:"Oslobygg KF",                       bruksnavn:"Bislett bad",        areal: 8400}},
    {type:"Feature", geometry:{type:"Point", coordinates:[10.7700, 59.9180]}, properties:{eiendom:"0301-234/22/0/0", adresse:"Grønland",         bydel:"Gamle Oslo",     eier:"Boligbygg Oslo KF",                 bruksnavn:"",                  areal: 1200}},
    {type:"Feature", geometry:{type:"Point", coordinates:[10.7350, 59.9050]}, properties:{eiendom:"0301-407/3/0/0",  adresse:"Filipstad",        bydel:"Frogner",        eier:"Oslo Havn KF",                      bruksnavn:"Filipstadkaia",      areal: 45000}},
  ]
};

const map = L.map("map", { preferCanvas: true }).setView([59.913, 10.752], 12);
L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
  subdomains: "abcd", maxZoom: 19,
}).addTo(map);

let DATA = null, cluster = null, heat = null;

const ownersBox = document.getElementById("owners");
const bydelSel  = document.getElementById("bydel");
const statsBox  = document.getElementById("stats");
const status    = document.getElementById("loadstatus");

function buildOwnerToggles() {
  let i = 0;
  for (const [name, color] of Object.entries(OWNERS)) {
    const id = "own-" + (i++);
    const lab = document.createElement("label");
    lab.className = "row";
    lab.innerHTML = `<input type="checkbox" id="${id}" checked>
                     <span class="swatch" style="background:${color}"></span>
                     <span class="name"></span>`;
    lab.querySelector(".name").textContent = name;
    const input = lab.querySelector("input");
    input.dataset.owner = name;
    input.addEventListener("change", render);
    ownersBox.appendChild(lab);
  }
}

function buildBydelList(features) {
  const set = new Set();
  for (const f of features) { if (f.properties.bydel) set.add(f.properties.bydel); }
  [...set].sort((a,b)=>a.localeCompare(b,"no")).forEach(b => {
    const o = document.createElement("option");
    o.value = b; o.textContent = b;
    bydelSel.appendChild(o);
  });
}

function activeOwners() {
  const set = new Set();
  document.querySelectorAll("#owners input").forEach(i => { if (i.checked) set.add(i.dataset.owner); });
  return set;
}

function currentView() { return document.querySelector('input[name="view"]:checked').value; }

function filtered() {
  const owners = activeOwners();
  const bydel = bydelSel.value;
  return DATA.features.filter(f => {
    if (!owners.has(f.properties.eier)) return false;
    if (bydel && f.properties.bydel !== bydel) return false;
    return true;
  });
}

function render() {
  if (!DATA) return;
  const feats = filtered();
  if (cluster) { map.removeLayer(cluster); cluster = null; }
  if (heat)    { map.removeLayer(heat);    heat = null; }

  const view = currentView();
  if (view === "points") {
    cluster = L.markerClusterGroup({ chunkedLoading: true, maxClusterRadius: 50 });
    for (const f of feats) {
      const [lon, lat] = f.geometry.coordinates;
      const c = OWNERS[f.properties.eier] || "#888";
      const m = L.circleMarker([lat, lon], { radius: 5, color: c, fillColor: c, fillOpacity: 0.8, weight: 1, opacity: 0.9 });
      const p = f.properties;
      const areal = (p.areal == null) ? "—" : p.areal.toLocaleString("no") + " m²";
      m.bindPopup(
        `<b>${p.eiendom}</b><br>${p.adresse || "<i>uten adresse</i>"}<br>` +
        (p.bruksnavn ? `${p.bruksnavn}<br>` : "") +
        `<span style="color:${c}">${p.eier}</span><br>` +
        `${p.bydel || ""}<br>Areal: ${areal}`
      );
      cluster.addLayer(m);
    }
    map.addLayer(cluster);
  } else {
    const arealView = view === "areal";
    const pts = feats.map(f => {
      const [lon, lat] = f.geometry.coordinates;
      const a = f.properties.areal;
      const w = arealView ? ((a && a > 0) ? Math.log10(a + 10) : 0.3) : 1;
      return [lat, lon, w];
    });
    heat = L.heatLayer(pts, { radius: arealView ? 22 : 18, blur: 22, maxZoom: 17, max: arealView ? 5 : 1.0 });
    map.addLayer(heat);
  }
  renderStats(feats);
}

function renderStats(feats) {
  const byOwner = {};
  let totM2 = 0;
  for (const f of feats) {
    const o = f.properties.eier;
    if (!byOwner[o]) byOwner[o] = { count: 0, areal: 0 };
    byOwner[o].count++;
    if (typeof f.properties.areal === "number") {
      byOwner[o].areal += f.properties.areal;
      totM2 += f.properties.areal;
    }
  }
  const maxCount = Math.max(1, ...Object.values(byOwner).map(v => v.count));
  const fmt = n => n.toLocaleString("no");
  const fmtDaa = n => (n/1000).toLocaleString("no", { maximumFractionDigits: 0 });

  let html = `<div class="stat"><span class="name">Antall</span><span class="val">${fmt(feats.length)}</span></div>
              <div class="stat"><span class="name">Totalt areal</span><span class="val">${fmtDaa(totM2)} daa</span></div>
              <div style="height:8px"></div>`;
  for (const o of Object.keys(OWNERS)) {
    const v = byOwner[o] || { count: 0, areal: 0 };
    const pct = (v.count / maxCount) * 100;
    html += `<div class="stat"><span class="name">${o}</span><span class="val">${fmt(v.count)} · ${fmtDaa(v.areal)} daa</span></div>
             <div class="bar"><div style="width:${pct}%;background:${OWNERS[o]}"></div></div>`;
  }
  statsBox.innerHTML = html;
}

document.querySelectorAll('input[name="view"]').forEach(r => r.addEventListener("change", render));
bydelSel.addEventListener("change", render);
buildOwnerToggles();

fetch("eiendommer.geojson")
  .then(r => { if (!r.ok) throw new Error("no geojson"); return r.json(); })
  .then(d => { DATA = d; status.textContent = `${DATA.features.length.toLocaleString("no")} lokaliserte eiendommer`; finish(); })
  .catch(() => { DATA = SAMPLE; status.textContent = "Eksempel-data (kjør geocode.py for full datasett)"; finish(); });

function finish() {
  buildBydelList(DATA.features);
  render();
  if (DATA.features.length) {
    try { map.fitBounds(L.geoJSON(DATA).getBounds(), { maxZoom: 13 }); } catch (e) {}
  }
}
</script>
</body>
</html>
"""


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
        w = csv.DictWriter(f, fieldnames=list(features and features[0]["properties"]
                                              or missing[0]).keys()) if (features or missing) else None
        if w:
            w.writeheader()
            for m in missing:
                w.writerow(m)

    Path("index.html").write_text(INDEX_HTML, encoding="utf-8")

    print(f"Wrote eiendommer.geojson ({len(features)} located rows).")
    print(f"Wrote index.html.")
    print(f"Wrote missing.csv ({len(missing)} rows without a located address).")
    print("\nNote: most 'missing' rows are road land (veigrunn) and unregistered")
    print("land that simply have no street address. To map those too, resolve")
    print("them against the cadastral parcel geometry (Kartverket 'Matrikkelen –")
    print("Eiendomskart Teig' WFS) keyed on the matrikkelnummer — see README.md.")


if __name__ == "__main__":
    main()
