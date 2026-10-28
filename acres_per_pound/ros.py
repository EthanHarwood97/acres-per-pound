"""Registers of Scotland cadastral parcels (Scottish plot boundaries).

The RoS INSPIRE download service publishes 33 registration-county
shapefiles under the Open Government Licence. Each zip contains both
BNG (EPSG:27700) and ETRS89 copies; we use the BNG one so the shared
INSPIRE matching code applies unchanged.

Coverage note: only titles in the Land Register are mapped. Land still
on the older Sasines register (a shrinking but real share of Scotland)
has no polygon, so Scottish plot coverage is inherently partial.

Download endpoint:
  /maps/download/ros-cp.cadastralparcel/<CODE>  -> 302 -> signed S3 zip
"""

import pathlib
import tempfile
import zipfile

import httpx

from . import inspire
from .http import load_config

ROS_DIR = inspire.INSPIRE_DIR.parent / "ros"
BASE = "https://ros-inspire.themapcloud.com"
DOWNLOAD = BASE + "/maps/download/ros-cp.cadastralparcel/"

# Registration counties (dropdown codes on the download service).
COUNTIES = {
    "ABN": "Aberdeen", "ANG": "Angus", "ARG": "Argyll", "AYR": "Ayr",
    "BNF": "Banff", "BER": "Berwick", "BUT": "Bute", "CTH": "Caithness",
    "CLK": "Clackmannan", "DMB": "Dumbarton", "DMF": "Dumfries",
    "ELN": "East Lothian", "FFE": "Fife", "GLA": "Glasgow", "INV": "Inverness",
    "KNC": "Kincardine", "KNR": "Kinross", "KRK": "Kirkcudbright",
    "LAN": "Lanark", "MID": "Midlothian", "MOR": "Moray", "NRN": "Nairn",
    "OAZ": "Orkney and Zetland", "PBL": "Peebles", "PTH": "Perth",
    "REN": "Renfrew", "ROS": "Ross and Cromarty", "ROX": "Roxburgh",
    "SEL": "Selkirk", "STG": "Stirling", "STH": "Sutherland",
    "WLN": "West Lothian", "WGN": "Wigtown",
}


def download_county(code, force=False, verbose=False):
    """Fetch one county zip (follows the signed S3 redirect)."""
    ROS_DIR.mkdir(parents=True, exist_ok=True)
    dest = ROS_DIR / f"{code}.zip"
    if dest.exists() and not force and dest.stat().st_size > 0:
        return dest
    cfg = load_config()
    ua = cfg["http"]["user_agent"]
    if verbose:
        print(f"  downloading {code} ({COUNTIES.get(code, code)}) ...")
    tmp = dest.with_suffix(".part")
    with httpx.stream("GET", DOWNLOAD + code, headers={"User-Agent": ua},
                      timeout=600, follow_redirects=True) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for block in r.iter_bytes(1024 * 256):
                f.write(block)
    tmp.replace(dest)
    return dest


def parse_zip(zip_path, verbose=False):
    """Parse the BNG shapefile inside a county zip -> (ids, polygons)."""
    from shapely.geometry import shape as _shape

    try:
        import shapefile  # pyshp
    except ImportError:
        raise SystemExit("pyshp is required for RoS parcels "
                         "(pip install pyshp)")

    ids, polys = [], []
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        shp = next((n for n in names
                    if n.lower().endswith("_bng.shp")), None) \
            or next((n for n in names if n.lower().endswith(".shp")), None)
        if not shp:
            return ids, polys
        stem = shp[:-4]
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="ros_"))
        for ext in (".shp", ".shx", ".dbf"):
            member = next((n for n in names if n.lower() == (stem + ext).lower()), None)
            if member:
                (tmp / pathlib.Path(member).name).write_bytes(z.read(member))
        reader = shapefile.Reader(str(tmp / pathlib.Path(shp).name))
        field_names = [f[0].lower() for f in reader.fields[1:]]
        id_idx = field_names.index("inspireid") if "inspireid" in field_names \
            else (field_names.index("label") if "label" in field_names else None)
        for i, shp_rec in enumerate(reader.iterShapes()):
            try:
                geom = _shape(shp_rec.__geo_interface__)
            except Exception:
                continue
            if geom.is_empty or geom.area <= 0:
                continue
            if id_idx is not None:
                rec = reader.record(i)
                gid = str(rec[id_idx])
            else:
                gid = f"{pathlib.Path(shp).stem}:{i}"
            if not geom.is_valid:
                geom = geom.buffer(0)
            polys.append(geom)
            ids.append(gid)
        if verbose:
            print(f"  {pathlib.Path(zip_path).name}: {len(polys):,} parcels")
    return ids, polys


def build_index(code, verbose=False):
    """Download + parse a county, cache polygons (WKB) to <CODE>.pkl."""
    import pickle

    pkl = ROS_DIR / f"{code}.pkl"
    if pkl.exists():
        try:
            with open(pkl, "rb") as f:
                head = pickle.load(f)
            if head.get("v") == 2:
                return pkl
        except Exception:
            pass
    zip_path = download_county(code, verbose=verbose)
    ids, polys = parse_zip(zip_path, verbose=verbose)
    if not polys:
        return None
    bbox = None
    wkb = []
    for p in polys:
        wkb.append(p.wkb)
        b = p.bounds
        bbox = (min(bbox[0], b[0]), min(bbox[1], b[1]),
                max(bbox[2], b[2]), max(bbox[3], b[3])) if bbox else b
    del polys
    payload = {"v": 2, "ids": ids, "wkb": wkb, "bbox": bbox, "source": "ros"}
    with open(pkl.with_suffix(".tmp"), "wb") as f:
        pickle.dump(payload, f, protocol=5)
    pkl.with_suffix(".tmp").replace(pkl)
    if verbose:
        print(f"  {code}: {len(ids):,} parcels cached")
    return pkl


def enrich_scotland(conn, codes=None, verbose=False):
    """Match every Scottish listing to its RoS parcel (plot_sqm/plot_source)."""
    from . import store as store_mod

    codes = codes or list(COUNTIES)
    by_id = {uid: row for uid, row in store_mod.listings_map(conn).items()
             if row.get("lat") and row.get("lng")
             and (row.get("country") in (None, "Scotland")
                  or str(row.get("postcode") or "").upper().startswith(
                      ("AB", "DD", "DG", "EH", "FK", "G", "HS", "IV", "KA",
                       "KW", "KY", "ML", "PA", "PH", "TD", "ZE")))
             and row.get("plot_source") != "ros"}
    if verbose:
        print(f"  {len(by_id):,} Scottish listings with coordinates")
    matched = 0
    attempted = 0
    for code in codes:
        try:
            pkl = build_index(code, verbose=verbose)
        except Exception as e:
            print(f"  {code}: failed: {e}")
            continue
        if pkl is None:
            continue
        bbox = inspire.bbox_of(pkl)
        pts = []
        for uid, row in by_id.items():
            if row.get("est_checked") and row.get("plot_source") == "ros":
                continue
            e, n = inspire.wgs84_to_bng(row["lat"], row["lng"])
            if bbox[0] <= e <= bbox[2] and bbox[1] <= n <= bbox[3]:
                pts.append((uid, e, n))
        if not pts:
            continue
        hits = inspire.match_points(pts, bbox, pkl)
        updates = []
        geoms = {}
        for uid, (area, gid, poly) in hits.items():
            acres = round(area / 4046.86, 3)
            updates.append((uid, {
                "plot_sqm": round(area, 1),
                "plot_sqft": round(area * 10.7639, 0),
                "plot_source": "ros",
                "plot_id": gid,
                "plot_conf": "registered",
                "est_plot_m2": round(area, 1),
                "est_acres": acres,
                "inspire_id": gid,
                "est_checked": 1,
            }))
            geoms.setdefault(gid, (area, poly))
        for uid, _, _ in pts:
            if uid not in hits:
                updates.append((uid, {"est_checked": 1}))
        store_mod.update_many(conn, updates)
        store_mod.save_plot_geoms(conn, [
            (gid, inspire.polygon_geojson(poly), area / 4046.86, "ros")
            for gid, (area, poly) in geoms.items()])
        matched += len(hits)
        attempted += len(pts)
        if verbose:
            print(f"  {code}: {len(hits)}/{len(pts)} matched")
    store_mod.recompute_metrics(conn)
    if verbose:
        print(f"  roS total: {matched:,}/{attempted:,} matched")
    return matched
