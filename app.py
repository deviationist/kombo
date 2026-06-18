#!/usr/bin/env python3
"""
app.py — Read-only HTTP API over the geocoded Oslo municipal property dataset.

This is the `serve` half of the kombo container (the `sync` half regenerates
eiendommer.geojson via fetch_xlsx.py + geocode.py — see entrypoint.sh). It loads
the pre-built eiendommer.geojson from $DATA_DIR and exposes:

    GET /healthz                 liveness + dataset summary
    GET /meta                    the dataset metadata block (vintage, counts, …)
    GET /eiendommer.geojson      the full FeatureCollection (what index.html loads)
    GET /nearby?lat&lon&radius   properties near a point (the forkjopsradar case)

The dataset is held in memory and transparently reloaded when the file's mtime
changes, so the weekly `sync` run is picked up with no restart.

Proximity math is a direct port of index.html's geometryClosest(): an
equirectangular local projection around the query point + point-to-segment
distance to polygon edges, with a ray-cast inside test. Keeping the algorithm
identical means the API and the map agree to the metre. Accurate at city scale
(< a few km), which is all the in-Oslo register needs — see CLAUDE.md.

Config (all via env; sensible defaults so it runs with zero config):
    DATA_DIR        directory holding eiendommer.geojson   (default ".")
    CORS_ORIGINS    comma-separated allowed origins; overrides the built-in
                    default set (kombo Pages + forkjopsradar + localhost dev)
"""

import json
import math
import os
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response

DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
GEOJSON_PATH = DATA_DIR / "eiendommer.geojson"

# Owner short codes — mirror index.html's OWNER_CODE so the API speaks the same
# vocabulary the map (and shareable URLs) already use.
OWNER_CODE = {
    "Eiendoms- og byfornyelsesetaten": "eby",
    "Oslobygg KF": "bygg",
    "Boligbygg Oslo KF": "bolig",
    "Oslo Havn KF": "havn",
}
CODE_TO_OWNER = {v: k for k, v in OWNER_CODE.items()}

# CORS. Default set covers the kombo map (GitHub Pages), the forkjopsradar
# real-estate tool (prod + dev), and local development. Override wholesale with
# CORS_ORIGINS="https://a,https://b" in the environment.
DEFAULT_CORS = [
    "https://kombo.ichiva.no",
    "https://forkjopsradar.ichiva.no",
    "https://forkjopsradar-dev.ichiva.no",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
_cors_env = os.environ.get("CORS_ORIGINS", "").strip()
CORS_ORIGINS = ([o.strip() for o in _cors_env.split(",") if o.strip()]
                if _cors_env else DEFAULT_CORS)

MEarthRadius = 6371008.8   # mean Earth radius (m), matches Leaflet's distanceTo


# ---------------------------------------------------------------------------
# Geometry — ported 1:1 from index.html so distances match the map exactly.
# ---------------------------------------------------------------------------
def haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres (Leaflet L.latLng.distanceTo equivalent)."""
    p = math.pi / 180
    a = (0.5 - math.cos((lat2 - lat1) * p) / 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2)
    return 2 * MEarthRadius * math.asin(math.sqrt(a))


def _seg_closest(px, py, ax, ay, bx, by):
    """Closest point on segment AB to P, in a flat metric plane → (dist, cx, cy)."""
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        cx, cy = ax, ay
    else:
        t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
        t = max(0.0, min(1.0, t))
        cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy), cx, cy


def _point_in_ring(x, y, ring):
    """Ray-cast inside test for a ring of [x, y] points (local metric plane)."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-30) + xi):
            inside = not inside
        j = i
    return inside


def geometry_closest(lat, lon, geom):
    """Nearest point + distance (m) from (lat, lon) to a GeoJSON geometry.

    Returns (dist_m, [lon, lat] of closest point, inside: bool). Mirrors
    index.html geometryClosest(): Point → haversine; (Multi)Polygon → local
    equirectangular projection + point-to-segment distance with inside test.
    """
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Point":
        glon, glat = coords[0], coords[1]
        return haversine(lat, lon, glat, glon), [glon, glat], False

    cos_lat = math.cos(lat * math.pi / 180)
    m_lat = 111320.0
    m_lon = 111320.0 * cos_lat

    def to_local(lng, la):
        return ((lng - lon) * m_lon, (la - lat) * m_lat)

    def from_local(x, y):
        return [lon + x / m_lon, lat + y / m_lat]

    polys = [coords] if gtype == "Polygon" else coords
    min_d, closest, inside = math.inf, None, False
    for poly in polys:
        local_rings = [[to_local(pt[0], pt[1]) for pt in ring] for ring in poly]
        if not local_rings:
            continue
        if (_point_in_ring(0.0, 0.0, local_rings[0])
                and not any(_point_in_ring(0.0, 0.0, h) for h in local_rings[1:])):
            inside = True
        for ring in local_rings:
            for i in range(len(ring) - 1):
                d, cx, cy = _seg_closest(0.0, 0.0, ring[i][0], ring[i][1],
                                         ring[i + 1][0], ring[i + 1][1])
                if d < min_d:
                    min_d = d
                    closest = from_local(cx, cy)
    if closest is None:
        return math.inf, [lon, lat], inside
    return min_d, closest, inside


def _geom_bbox(geom):
    """(min_lon, min_lat, max_lon, max_lat) for a Point/Polygon/MultiPolygon."""
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Point":
        return (coords[0], coords[1], coords[0], coords[1])
    # Flatten all rings to vertices.
    rings = [coords] if gtype == "Polygon" else [r for poly in coords for r in [poly]]
    if gtype == "MultiPolygon":
        verts = [pt for poly in coords for ring in poly for pt in ring]
    else:  # Polygon
        verts = [pt for ring in coords for pt in ring]
    lons = [v[0] for v in verts]
    lats = [v[1] for v in verts]
    return (min(lons), min(lats), max(lons), max(lats))


def feature_anchor(feat):
    """[lon, lat] marker anchor: props.center, else Point coords, else bbox seed."""
    props = feat["properties"]
    if isinstance(props.get("center"), list) and len(props["center"]) == 2:
        return props["center"]
    geom = feat["geometry"]
    if geom.get("type") == "Point":
        return [geom["coordinates"][0], geom["coordinates"][1]]
    bb = feat["_bbox"]
    return [(bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2]


# ---------------------------------------------------------------------------
# Dataset cache — load eiendommer.geojson, reload transparently on mtime change.
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_state = {"mtime": None, "fc": None, "features": [], "raw": b"", "etag": None}


def _load():
    """(Re)load the dataset. Precomputes a bbox per feature for the prefilter."""
    raw = GEOJSON_PATH.read_bytes()
    fc = json.loads(raw)
    feats = fc.get("features", [])
    for f in feats:
        try:
            f["_bbox"] = _geom_bbox(f["geometry"])
        except (KeyError, TypeError, ValueError):
            f["_bbox"] = None
    mtime = GEOJSON_PATH.stat().st_mtime
    _state.update(mtime=mtime, fc=fc, features=feats, raw=raw,
                  etag=f'"{int(mtime)}-{len(raw)}"')


def get_dataset():
    """Return the loaded FeatureCollection, reloading if the file changed."""
    with _lock:
        if not GEOJSON_PATH.exists():
            raise HTTPException(status_code=503,
                                detail=f"dataset not available ({GEOJSON_PATH})")
        mtime = GEOJSON_PATH.stat().st_mtime
        if _state["mtime"] != mtime:
            _load()
        return _state


def _resolve_eier(eier_param):
    """Parse the `eier` query (codes and/or full names, comma-separated) into a
    set of canonical owner names, or None for 'all'."""
    if not eier_param:
        return None
    wanted = set()
    for tok in eier_param.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok in CODE_TO_OWNER:
            wanted.add(CODE_TO_OWNER[tok])
        else:
            wanted.add(tok)
    return wanted or None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="kombo property API",
    description="Read-only proximity API over Oslo kommune's property register.",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["GET"],
    allow_headers=["*"],
    max_age=86400,
)
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.get("/healthz")
def healthz():
    try:
        ds = get_dataset()
    except HTTPException:
        return JSONResponse({"status": "no-data"}, status_code=503)
    meta = ds["fc"].get("metadata", {})
    return {
        "status": "ok",
        "features": len(ds["features"]),
        "vintage": meta.get("vintage"),
        "generatedAt": meta.get("generatedAt"),
        "dataChangedAt": meta.get("dataChangedAt"),
    }


@app.get("/meta")
def meta():
    ds = get_dataset()
    return ds["fc"].get("metadata", {})


@app.get("/eiendommer.geojson")
def full_geojson():
    """The complete FeatureCollection — what index.html bulk-loads.

    Served from the in-memory bytes (minus our internal _bbox keys, which we
    strip by returning the original raw file). ETag + long-ish cache so the map
    and CDN can revalidate cheaply; the weekly sync changes the ETag."""
    ds = get_dataset()
    return Response(
        content=ds["raw"],
        media_type="application/geo+json",
        headers={"ETag": ds["etag"], "Cache-Control": "public, max-age=3600"},
    )


@app.get("/nearby")
def nearby(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius: float = Query(150, gt=0, le=20000, description="search radius in metres"),
    eier: str = Query("", description="owner filter: codes (eby,bygg,bolig,havn) "
                                      "or full names, comma-separated; empty = all"),
    mode: str = Query("edge", pattern="^(edge|marker)$",
                      description="distance to nearest parcel edge, or to the marker"),
    limit: int = Query(50, ge=1, le=500),
    utenbys: str = Query("exclude", pattern="^(exclude|include|only)$",
                         description="out-of-Oslo parcels"),
    geometry: bool = Query(False, description="include full parcel geometry in results"),
):
    """Properties within `radius` metres of (lat, lon), nearest first.

    The forkjopsradar use case: GET /nearby?lat=…&lon=…&radius=300&eier=bolig
    answers "how close is this listing to a municipal housing property?"
    """
    ds = get_dataset()
    wanted = _resolve_eier(eier)

    # Degree padding for the bbox prefilter — generous so we never miss an
    # edge that's within `radius` of the point.
    cos_lat = max(0.01, math.cos(lat * math.pi / 180))
    dlat = radius / 111320.0
    dlon = radius / (111320.0 * cos_lat)
    box = (lon - dlon, lat - dlat, lon + dlon, lat + dlat)

    results = []
    for f in ds["features"]:
        props = f["properties"]
        is_utb = bool(props.get("utenbys"))
        if utenbys == "exclude" and is_utb:
            continue
        if utenbys == "only" and not is_utb:
            continue
        if wanted is not None and (props.get("eier", "").strip() not in wanted):
            continue
        bb = f.get("_bbox")
        if bb is None:
            continue
        # bbox intersect test (cheap reject before the exact distance).
        if bb[2] < box[0] or bb[0] > box[2] or bb[3] < box[1] or bb[1] > box[3]:
            continue

        if mode == "marker":
            a = feature_anchor(f)
            dist = haversine(lat, lon, a[1], a[0])
            closest = a
            # Inside flag still meaningful for polygons.
            inside = (geometry_closest(lat, lon, f["geometry"])[2]
                      if f["geometry"].get("type") != "Point" else False)
        else:
            dist, closest, inside = geometry_closest(lat, lon, f["geometry"])

        if dist > radius and not inside:
            continue

        owner = props.get("eier", "")
        item = {
            "eiendom": props.get("eiendom"),
            "eier": owner,
            "eier_code": OWNER_CODE.get(owner),
            "adresse": props.get("adresse") or None,
            "postnummer": props.get("postnummer"),
            "poststed": props.get("poststed"),
            "bydel": props.get("bydel") or None,
            "bruksnavn": props.get("bruksnavn") or None,
            "areal": props.get("areal"),
            "utenbys": is_utb,
            "distance_m": round(dist, 1),
            "inside": inside,
            "closest": [round(closest[0], 6), round(closest[1], 6)],
            "geometry_type": f["geometry"].get("type"),
        }
        if geometry:
            item["geometry"] = f["geometry"]
        results.append(item)

    results.sort(key=lambda r: r["distance_m"])
    truncated = len(results) > limit
    results = results[:limit]

    return {
        "query": {"lat": lat, "lon": lon, "radius_m": radius,
                  "eier": sorted(wanted) if wanted else None,
                  "mode": mode, "utenbys": utenbys},
        "count": len(results),
        "truncated": truncated,
        "nearest_m": results[0]["distance_m"] if results else None,
        "results": results,
    }
