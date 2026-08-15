"""INSPIRE / National Polygon Service enrichment.

Downloads HM Land Registry's free INSPIRE Index Polygons (registered
freehold plot boundaries, England & Wales only), parses the GML, and
point-matches every listing's lat/lng to its plot -> estimated plot
size in acres, even when the ad never mentions land.

Data flow (all cached under data/inspire/):
  download page -> <LA>.zip -> parse GML -> <LA>.pkl (polygons + bbox)
  -> bboxes.json (sidecar for candidate selection) -> state.json rows
     get est_acres + inspire_id.

Coordinates: GML is EPSG:27700 (OSGB36 National Grid, metres), listing
lat/lng is WGS84 - converted with the standard OSGB36 datum transform
(no external CRS library needed).

Scotland / Northern Ireland are not in the dataset - listings there get
est_acres = None (flagged "n/a").
"""

import io
import json
import math
import pathlib
import re
import time
import zipfile

import httpx

from .http import DATA_DIR, load_config

BASE = "https://use-land-property-data.service.gov.uk"
DOWNLOAD_PAGE = BASE + "/datasets/inspire/download"
INSPIRE_DIR = DATA_DIR.parent / "data" / "inspire"

# --- OSGB36 transform -------------------------------------------------

_A, _B = 6377563.396, 6356256.909          # Airy 1830
_E2 = (_A ** 2 - _B ** 2) / _A ** 2
_N = (_A - _B) / (_A + _B)
_F0 = 0.9996012717
_LAT0 = math.radians(49.0)
_LON0 = math.radians(-2.0)
_E0, _N0 = 400000.0, -100000.0

# Helmert WGS84 -> OSGB36 (EPSG:1314)
_TX, _TY, _TZ = -446.448, 125.157, -542.060
_S = 20.4894e-6
_RX = math.radians(0.1502 / 3600)
_RY = math.radians(0.2470 / 3600)
_RZ = math.radians(0.8421 / 3600)


def _wgs84_to_bng_manual(lat, lon):
    """Manual 7-param transform (fallback when pyproj is unavailable)."""
    lat, lon = math.radians(lat), math.radians(lon)
    h = 0.0
    a, e2 = 6378137.0, 0.00669437999014  # WGS84
    v = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    x = (v + h) * math.cos(lat) * math.cos(lon)
    y = (v + h) * math.cos(lat) * math.sin(lon)
    z = ((1 - e2) * v + h) * math.sin(lat)
    # Helmert to OSGB36
    x2 = _TX + (1 + _S) * x - _RZ * y + _RY * z
    y2 = _TY + _RZ * x + (1 + _S) * y - _RX * z
    z2 = _TZ - _RY * x + _RX * y + (1 + _S) * z
    # geodetic on Airy
    p = math.hypot(x2, y2)
    lat2 = math.atan2(z2, p * (1 - _E2))
    for _ in range(6):
        v2 = _A / math.sqrt(1 - _E2 * math.sin(lat2) ** 2)
        lat2 = math.atan2(z2 + _E2 * v2 * math.sin(lat2), p)
    lon2 = math.atan2(y2, x2)
    # Transverse Mercator
    dlat = lat2 - _LAT0
    dlon = lon2 - _LON0
    v3 = _A * _F0 / math.sqrt(1 - _E2 * math.sin(lat2) ** 2)
    rho = _A * _F0 * (1 - _E2) / (1 - _E2 * math.sin(lat2) ** 2) ** 1.5
    eta2 = v3 / rho - 1.0
    t = math.tan(lat2)
    m = _B * _F0 * (
        (1 + _N + 5 / 4 * _N ** 2 + 5 / 4 * _N ** 3) * dlat
        - (3 * _N + 3 * _N ** 2 + 21 / 8 * _N ** 3) * math.sin(dlat) * math.cos(lat2 + _LAT0)
        + (15 / 8 * _N ** 2 + 15 / 8 * _N ** 3) * math.sin(2 * dlat) * math.cos(2 * (lat2 + _LAT0))
        - (35 / 24 * _N ** 3) * math.sin(3 * dlat) * math.cos(3 * (lat2 + _LAT0)))
    i = m + _N0
    ii = v3 / 2 * t * math.cos(lat2) ** 2
    iii = v3 / 24 * t * math.cos(lat2) ** 4 * (5 - t ** 2 + 9 * eta2)
    iiia = v3 / 720 * t * math.cos(lat2) ** 6 * (61 - 58 * t ** 2 + t ** 4)
    iv = v3 * math.cos(lat2)
    v5 = v3 / 6 * math.cos(lat2) ** 3 * (v3 / rho - t ** 2)
    vi = v3 / 120 * math.cos(lat2) ** 5 * (
        5 - 18 * t ** 2 + t ** 4 + 14 * eta2 - 58 * t ** 2 * eta2)
    easting = _E0 + iv * dlon + v5 * dlon ** 3 + vi * dlon ** 5
    northing = i + ii * dlon ** 2 + iii * dlon ** 4 + iiia * dlon ** 6
    return easting, northing


try:
    from pyproj import Transformer

    _TRANSFORMER = Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)

    def wgs84_to_bng(lat, lon):
        e, n = _TRANSFORMER.transform(lon, lat)
        return e, n
except Exception:  # pragma: no cover
    def wgs84_to_bng(lat, lon):
        return _wgs84_to_bng_manual(lat, lon)


# --- LA list + download -------------------------------------------------

_MEMBER_RE = re.compile(rb"<wfs:member>(.*?)</wfs:member>", re.S)
_ID_RE = re.compile(rb'gml:id="([^"]+)"')
_POS_RE = re.compile(rb"<gml:posList[^>]*>([^<]+)</gml:posList>")


def list_las(fetcher=None):
    """Return list of LA zip names from the download page."""
    r = httpx.get(DOWNLOAD_PAGE, headers={"User-Agent": load_config()["http"]["user_agent"]},
                  timeout=30, follow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f"download page status {r.status_code}")
    names = re.findall(r"/datasets/inspire/download/([^/\"]+\.zip)", r.text)
    return sorted(set(names))


def download_la(name, force=False, verbose=False):
    """Download one LA zip into data/inspire/, returns zip path."""
    INSPIRE_DIR.mkdir(parents=True, exist_ok=True)
    out = INSPIRE_DIR / name
    if out.exists() and not force:
        return out
    url = f"{BASE}/datasets/inspire/download/{name}"
    if verbose:
        print(f"  downloading {name} ...")
    last_err = None
    for attempt in range(4):
        try:
            with httpx.stream("GET", url,
                              headers={"User-Agent": load_config()["http"]["user_agent"]},
                              timeout=300, follow_redirects=True) as r:
                r.raise_for_status()
                with open(out.with_suffix(".part"), "wb") as f:
                    for chunk in r.iter_bytes(1024 * 256):
                        f.write(chunk)
            out.with_suffix(".part").replace(out)
            return out
        except Exception as e:
            last_err = e
            time.sleep(15 * (2 ** attempt))
    out.with_suffix(".part").unlink(missing_ok=True)
    raise RuntimeError(f"download failed for {name}: {last_err}")


# --- GML parsing ----------------------------------------------------------

def _members(stream):
    """Yield GML <wfs:member> chunks from a binary stream, buffered."""
    buf = b""
    while True:
        block = stream.read(8 * 1024 * 1024)
        if not block:
            break
        buf += block
        while True:
            end = buf.find(b"</wfs:member>")
            if end < 0:
                break
            start = buf.find(b"<wfs:member>")
            if start < 0 or start > end:
                buf = buf[end + len(b"</wfs:member>"):]
                break
            yield buf[start:end + len(b"</wfs:member>")]
            buf = buf[end + len(b"</wfs:member>"):]
    if b"<wfs:member>" in buf:
        yield buf


def parse_zip(zip_path, verbose=False):
    """Parse an LA zip into (gml_ids, polygons). Returns ([], []) on failure.

    Streams the GML member-by-member so peak memory stays bounded even
    for multi-GB county files.
    """
    from shapely.geometry import Polygon

    ids, polys = [], []
    try:
        zf = zipfile.ZipFile(zip_path)
        gml_name = next(n for n in zf.namelist() if n.lower().endswith(".gml"))
    except Exception as e:
        if verbose:
            print(f"  {zip_path.name}: unzip failed: {e}")
        return [], []
    with zf.open(gml_name) as f:
        for chunk in _members(f):
            mid = _ID_RE.search(chunk)
            if not mid:
                continue
            rings = []
            for pos in _POS_RE.finditer(chunk):
                nums = [float(x) for x in pos.group(1).split()]
                if len(nums) < 6:
                    continue
                rings.append(list(zip(nums[0::2], nums[1::2])))
            if not rings:
                continue
            try:
                poly = Polygon(rings[0], rings[1:])
            except Exception:
                continue
            if not poly.is_valid or poly.is_empty:
                continue
            ids.append(mid.group(1).decode("ascii", "replace"))
            polys.append(poly)
    return ids, polys


def build_index(name, verbose=False):
    """Download + parse an LA, cache polygons (WKB) to <name>.pkl.

    Deletes the zip after parsing (pkl is what subsequent runs use).
    """
    import pickle

    zip_path = download_la(name, verbose=verbose)
    pkl = INSPIRE_DIR / (name[:-4] + ".pkl")
    if pkl.exists():
        try:
            with open(pkl, "rb") as f:
                head = pickle.load(f)
            if head.get("v") == 2:
                return pkl
        except Exception:
            pass
    if verbose:
        print(f"  parsing {name} ...")
    ids, polys = parse_zip(zip_path, verbose=verbose)
    if not polys:
        return None
    bbox = None
    wkb = []
    for p in polys:
        wkb.append(p.wkb)
        b = p.bounds
        bbox = (min(bbox[0], b[0]), min(bbox[1], b[1]), max(bbox[2], b[2]), max(bbox[3], b[3])) \
            if bbox else b
    del polys
    payload = {"v": 2, "ids": ids, "wkb": wkb, "bbox": bbox}
    with open(pkl.with_suffix(".tmp"), "wb") as f:
        pickle.dump(payload, f, protocol=5)
    pkl.with_suffix(".tmp").replace(pkl)
    zip_path.unlink(missing_ok=True)
    if verbose:
        print(f"  {name}: {len(ids)} polygons cached")
    return pkl


def bbox_of(pkl_path):
    import pickle

    with open(pkl_path, "rb") as f:
        data = pickle.load(f)
    if data.get("v") == 2 and data.get("bbox"):
        return tuple(data["bbox"])
    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    for p in data["polys"]:
        b = p.bounds
        minx = min(minx, b[0]); miny = min(miny, b[1])
        maxx = max(maxx, b[2]); maxy = max(maxy, b[3])
    return (minx, miny, maxx, maxy)


def bboxes_map(names):
    """{la_name: bbox} for all built LA indexes (writes sidecar cache)."""
    import json as _json

    sidecar = INSPIRE_DIR / "bboxes.json"
    cache = {}
    if sidecar.exists():
        try:
            cache = _json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    out = {}
    for name in names:
        key = name[:-4]
        if key in cache:
            out[name] = tuple(cache[key])
            continue
        pkl = INSPIRE_DIR / (key + ".pkl")
        if pkl.exists():
            b = bbox_of(pkl)
            out[name] = b
            cache[key] = list(b)
        else:
            out[name] = None
    sidecar.write_text(_json.dumps(cache), encoding="utf-8")
    return out


def match_points(points, bbox, pkl_path):
    """For (rm_id, easting, northing) points inside bbox, return {rm_id: (area_m2, gml_id)}."""
    import pickle

    import numpy as np
    from shapely import wkb as _wkb
    from shapely.geometry import Point
    from shapely.strtree import STRtree

    with open(pkl_path, "rb") as f:
        data = pickle.load(f)
    if data.get("v") == 2:
        polys = [_wkb.loads(w) for w in data["wkb"]]
    else:
        polys = data["polys"]
    tree = STRtree(polys)
    pts = np.array([Point(x, y) for _, x, y in points], dtype=object)
    hits = tree.query(pts, predicate="intersects")
    result = {}
    if hits is not None and len(hits):
        for pi, gi in zip(hits[0], hits[1]):
            rid = points[int(pi)][0]
            area = polys[int(gi)].area
            cur = result.get(rid)
            if cur is None or area < cur[0]:  # smallest containing plot wins
                result[rid] = (area, data["ids"][int(gi)])
    del polys, tree
    return result
