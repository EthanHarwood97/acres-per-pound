"""Family-suitability map layers (local monthly job, like enrich/sold).

- airports: UK airports with scheduled services (OurAirports, filtered
  to large/medium) - noise-zone circles drawn on the map.
- crime: street-level crime counts (data.police.uk monthly archive,
  England/Wales/NI only - Police Scotland publishes no equivalent),
  aggregated to a ~0.005 degree grid for a heatmap overlay.

Output: docs/layers.json - a small committed file the site loads lazily
when the map opens.
"""

import csv
import io
import json
import math
import pathlib
import re
import zipfile
from collections import Counter

import httpx

from .http import DATA_DIR, load_config

LAYERS_DIR = DATA_DIR.parent / "data" / "layers"
AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"


def _headers():
    return {"User-Agent": load_config()["http"]["user_agent"]}


# Airports with scheduled passenger services in the UK (England, Wales,
# Scotland, NI) + crown dependencies. Curated - military/private fields
# have IATA codes too, so we match against this list explicitly.
SCHEDULED_IATA = {
    # England
    "LHR", "LGW", "STN", "LTN", "LCY", "SEN", "MAN", "LPL", "BHX", "EMA",
    "BRS", "EXT", "NQY", "BOH", "SOU", "NCL", "MME", "LBA", "HUY", "NWI",
    # Wales
    "CWL", "VLY",
    # Scotland
    "ABZ", "EDI", "GLA", "PIK", "INV", "DND", "KOI", "LSI", "SYY", "WIC",
    "ILY", "TRE", "BEB", "BRR", "CAL", "EOI", "PPW", "WRY", "SOY", "NDY",
    # Northern Ireland
    "BFS", "BHD", "LDY",
    # Crown dependencies
    "IOM", "JER", "GCI",
}
SCHEDULED_COUNTRIES = {"GB", "IM", "JE", "GG"}


def fetch_airports(verbose=False):
    """Return UK scheduled-service airports [{name, lat, lng, iata, type}]."""
    LAYERS_DIR.mkdir(parents=True, exist_ok=True)
    cache = LAYERS_DIR / "airports.csv"
    if not cache.exists():
        if verbose:
            print("  downloading OurAirports CSV ...")
        r = httpx.get(AIRPORTS_URL, headers=_headers(), timeout=120, follow_redirects=True)
        r.raise_for_status()
        cache.with_suffix(".part").write_bytes(r.content)
        cache.with_suffix(".part").replace(cache)
    out = []
    with open(cache, encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            if row.get("iso_country") not in SCHEDULED_COUNTRIES:
                continue
            iata = (row.get("iata_code") or "").strip().upper()
            if iata not in SCHEDULED_IATA:
                continue
            if not row.get("latitude_deg") or not row.get("longitude_deg"):
                continue
            out.append({
                "name": (row.get("municipality") or row.get("name") or "").strip(),
                "lat": float(row["latitude_deg"]),
                "lng": float(row["longitude_deg"]),
                "iata": iata,
                "type": row.get("type") or "",
            })
    out.sort(key=lambda a: a["name"])
    return out


def _crime_month():
    """Latest available archive month (data.police.uk lags ~1-2 months)."""
    import datetime

    today = datetime.date.today()
    for back in range(1, 4):
        month = (today.replace(day=1) - datetime.timedelta(days=31 * back)).strftime("%Y-%m")
        url = f"https://data.police.uk/data/archive/{month}.zip"
        r = httpx.head(url, headers=_headers(), timeout=30, follow_redirects=True)
        if r.status_code == 200:
            return month
    raise RuntimeError("no recent police data archive found")


def fetch_crime(verbose=False):
    """Download + aggregate street-level crime to a grid.

    Returns list of [lat, lng, count] cells.
    """
    LAYERS_DIR.mkdir(parents=True, exist_ok=True)
    month = _crime_month()
    zip_path = LAYERS_DIR / f"crime-{month}.zip"
    if not zip_path.exists():
        if verbose:
            print(f"  downloading {month} archive (~1.7GB, one-time) ...")
        url = f"https://data.police.uk/data/archive/{month}.zip"
        with httpx.stream("GET", url, headers=_headers(), timeout=600, follow_redirects=True) as r:
            r.raise_for_status()
            with open(zip_path.with_suffix(".part"), "wb") as f:
                for chunk in r.iter_bytes(1024 * 512):
                    f.write(chunk)
        zip_path.with_suffix(".part").replace(zip_path)

    cells = Counter()
    zf = zipfile.ZipFile(zip_path)
    street_files = [n for n in zf.namelist() if n.endswith("-street.csv")]
    if verbose:
        print(f"  aggregating {len(street_files)} force CSVs ...")
    for name in street_files:
        try:
            with zf.open(name) as f:
                reader = csv.reader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"))
                header = next(reader, None)
                if not header:
                    continue
                try:
                    lat_i = header.index("Latitude")
                    lng_i = header.index("Longitude")
                except ValueError:
                    continue
                for row in reader:
                    if len(row) <= max(lat_i, lng_i):
                        continue
                    try:
                        lat = float(row[lat_i])
                        lng = float(row[lng_i])
                    except (ValueError, IndexError):
                        continue
                    if not (-90 < lat < 90 and -180 < lng < 180):
                        continue
                    cells[(round(lat * 200) / 200, round(lng * 200) / 200)] += 1
        except Exception as e:
            if verbose:
                print(f"    {name}: {e}")
    grid = [[lat, lng, n] for (lat, lng), n in sorted(cells.items())]
    return month, grid


def build_layers(out_path=None, verbose=False):
    """Write docs/layers.json with airports + crime grid."""
    airports = fetch_airports(verbose=verbose)
    month, grid = fetch_crime(verbose=verbose)
    payload = {
        "updated": f"{month}-01",
        "airports": airports,
        "crimes": grid,
        "crime_note": "street-level crime counts, England/Wales/NI only (Scotland not published)",
    }
    out = pathlib.Path(out_path) if out_path else DATA_DIR.parent / "docs" / "layers.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if verbose:
        print(f"  airports: {len(airports)}, crime cells: {len(grid)}, written to {out}")
    return payload


# ---------------------------------------------------------------------------
# GP surgeries (CQC directory - public, no key)
# ---------------------------------------------------------------------------

CQC_PAGE = "https://www.cqc.org.uk/about-us/transparency/using-cqc-data"
CQC_RE = re.compile(r'href="([^"]+directory\.csv)"')


def fetch_gps(verbose=False):
    """CQC directory -> GP practices with geocoded postcodes."""
    LAYERS_DIR.mkdir(parents=True, exist_ok=True)
    cache_out = LAYERS_DIR / "gps.json"
    if cache_out.exists():
        try:
            return json.loads(cache_out.read_text(encoding="utf-8"))
        except Exception:
            pass
    if verbose:
        print("  fetching CQC care directory ...")
    r = httpx.get(CQC_PAGE, headers=_headers(), timeout=60, follow_redirects=True)
    m = CQC_RE.search(r.text)
    if not m:
        if verbose:
            print("  CQC csv link not found")
        return []
    csv_url = m.group(1)
    cache = LAYERS_DIR / "cqc_directory.csv"
    if not cache.exists():
        resp = httpx.get(csv_url, headers=_headers(), timeout=300, follow_redirects=True)
        resp.raise_for_status()
        cache.with_suffix(".part").write_bytes(resp.content)
        cache.with_suffix(".part").replace(cache)
    rows = []
    header = None
    with open(cache, encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        for row in reader:
            if row and row[0] == "Name":
                header = row
                continue
            if not header or len(row) < 4:
                continue
            rec = dict(zip(header, row))
            if "Doctors/GPs" not in (rec.get("Service types") or ""):
                continue
            if not rec.get("Postcode"):
                continue
            rows.append((rec.get("Name") or "", rec["Postcode"].strip().upper()))
    if verbose:
        print(f"  {len(rows)} GP practices, geocoding via postcodes.io ...")
    out = []
    seen = set()
    for i in range(0, len(rows), 100):
        batch = rows[i:i + 100]
        try:
            resp = httpx.post("https://api.postcodes.io/postcodes",
                              json={"postcodes": [b[1] for b in batch]},
                              headers=_headers(), timeout=90)
            results = resp.json().get("result", [])
        except Exception as e:
            if verbose:
                print(f"    geocode batch failed: {e}")
            continue
        for (name, pc), g in zip(batch, results):
            g = g or {}
            r2 = g.get("result") or {}
            lat, lng = r2.get("latitude"), r2.get("longitude")
            if lat is None or lng is None or pc in seen:
                continue
            seen.add(pc)
            out.append({"name": name, "lat": round(lat, 5), "lng": round(lng, 5)})
    cache_out.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Flood risk (Environment Agency Flood Map for Planning, ArcGIS FeatureServer)
# ---------------------------------------------------------------------------

FLOOD_BASE = ("https://services1.arcgis.com/JZM7qJpmv7vJ0Hzx/arcgis/rest/services/"
              "Flood_Map_for_Planning/FeatureServer")
FLOOD_CELL = 0.01


def _rings_to_polys(rings):
    from shapely.geometry import Polygon

    polys = []
    for ring in rings:
        if len(ring) < 4:
            continue
        coords = [(pt[0], pt[1]) for pt in ring]
        try:
            polys.append(Polygon(coords))
        except Exception:
            continue
    return polys


def fetch_flood(verbose=False):
    """Flood Zone 3 polygons -> 0.01 degree grid cells."""
    from shapely.geometry import Point

    cache_out = LAYERS_DIR / "flood.json"
    if cache_out.exists():
        try:
            return json.loads(cache_out.read_text(encoding="utf-8"))
        except Exception:
            pass
    cells = set()
    offset = 0
    page = 0
    while True:
        url = (f"{FLOOD_BASE}/1/query?where=1%3D1&returnGeometry=true&outSR=4326"
               f"&outFields=&resultRecordCount=2000&resultOffset={offset}&f=json")
        try:
            r = httpx.get(url, headers=_headers(), timeout=180)
            d = r.json()
        except Exception as e:
            if verbose:
                print(f"  flood page {page} failed: {e}")
            break
        feats = d.get("features") or []
        if not feats:
            break
        for f in feats:
            geom = (f.get("geometry") or {}).get("rings") or []
            for poly in _rings_to_polys(geom):
                minx, miny, maxx, maxy = poly.bounds
                lat0 = math.floor(miny / FLOOD_CELL) * FLOOD_CELL
                while lat0 <= maxy:
                    lng0 = math.floor(minx / FLOOD_CELL) * FLOOD_CELL
                    while lng0 <= maxx:
                        pt = Point(lng0 + FLOOD_CELL / 2, lat0 + FLOOD_CELL / 2)
                        if poly.contains(pt):
                            cells.add((round(lat0, 2), round(lng0, 2)))
                        lng0 += FLOOD_CELL
                    lat0 += FLOOD_CELL
        page += 1
        offset += len(feats)
        if verbose and page % 20 == 0:
            print(f"  flood: {page} pages, {offset} polygons, {len(cells)} cells")
        if len(feats) < 2000:
            break
    out = [[lat, lng] for lat, lng in sorted(cells)]
    cache_out.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Parks & green space (OpenStreetMap via Geofabrik GB extract)
# ---------------------------------------------------------------------------

GEOFABRIK_GB = "https://download.geofabrik.de/europe/great-britain-latest.osm.pbf"
PARK_TAGS = {"leisure": {"park", "recreation_ground", "village_green", "garden"},
             "landuse": {"grass", "recreation_ground", "village_green"}}


def fetch_parks(verbose=False):
    """Geofabrik GB extract -> largest parks (top 3000 by area)."""
    try:
        import osmium
        from osmium import geom
    except ImportError:
        if verbose:
            print("  osmium not installed - pip install osmium (see requirements-enrich.txt)")
        return []

    from shapely.geometry import Polygon
    from shapely.wkb import loads as wkb_loads

    LAYERS_DIR.mkdir(parents=True, exist_ok=True)
    cache_out = LAYERS_DIR / "parks.json"
    if cache_out.exists():
        try:
            return json.loads(cache_out.read_text(encoding="utf-8"))
        except Exception:
            pass
    pbf = LAYERS_DIR / "great-britain-latest.osm.pbf"
    if not pbf.exists():
        if verbose:
            print("  downloading OpenStreetMap GB extract (~1.4GB, one-time) ...")
        with httpx.stream("GET", GEOFABRIK_GB, headers=_headers(), timeout=600, follow_redirects=True) as r:
            r.raise_for_status()
            with open(pbf.with_suffix(".part"), "wb") as f:
                for chunk in r.iter_bytes(1024 * 512):
                    f.write(chunk)
        pbf.with_suffix(".part").replace(pbf)
    if verbose:
        print("  extracting parks from OSM (disk-backed, memory-safe) ...")

    loc_path = LAYERS_DIR / "nodes.idx"

    class ParkHandler(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.fac = geom.WKBFactory()
            self.parks = []

        def way(self, w):
            tags = w.tags
            if tags.get("leisure") in PARK_TAGS["leisure"] or \
                    tags.get("landuse") in PARK_TAGS["landuse"]:
                if not w.is_closed():
                    return
                try:
                    wkb = self.fac.create_linestring(w)
                except Exception:
                    return
                self.parks.append((tags.get("name") or "", wkb))

    handler = ParkHandler()
    handler.apply_file(str(pbf), locations=True)
    if verbose:
        print(f"  {len(handler.parks)} candidate ways, computing areas ...")
    out = []
    for name, wkb in handler.parks:
        try:
            line = wkb_loads(wkb)
            if line is None or len(line.coords) < 4:
                continue
            poly = Polygon(line.coords)
            if not poly.is_valid or poly.is_empty:
                continue
            centroid = poly.centroid
            lat = centroid.y
            lon_per_m = 1.0 / (111320 * math.cos(math.radians(lat)))
            lat_per_m = 1.0 / 110540
            area_m2 = abs(poly.area) * (1 / lon_per_m) * (1 / lat_per_m)
            if area_m2 < 5000:
                continue
            out.append({"name": name, "lat": round(lat, 5), "lng": round(centroid.x, 5),
                        "area_ha": round(area_m2 / 10000, 1)})
        except Exception:
            continue
    out.sort(key=lambda p: p["area_ha"], reverse=True)
    out = out[:3000]
    cache_out.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Schools + Ofsted ratings (GIAS establishments CSV - user downloads once)
# ---------------------------------------------------------------------------

GIAS_CSV = LAYERS_DIR / "establishments.csv"


def fetch_schools(verbose=False):
    """Parse a locally-downloaded GIAS 'Establishments' CSV if present.

    Needs a free account at get-information-schools.service.gov.uk ->
    Downloads -> 'Establishments' CSV (all fields), saved to
    data/layers/establishments.csv. Geocodes postcodes via postcodes.io.
    """
    if not GIAS_CSV.exists():
        return None
    rows = []
    with open(GIAS_CSV, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for rec in reader:
            name = rec.get("EstablishmentName") or ""
            pc = (rec.get("Postcode") or "").strip().upper()
            if not name or not pc:
                continue
            rows.append({
                "name": name,
                "postcode": pc,
                "phase": rec.get("PhaseOfEducation (name)") or "",
                "rating": rec.get("OfstedRating (name)") or "",
                "type": rec.get("TypeOfEstablishment (name)") or "",
            })
    if verbose:
        print(f"  {len(rows)} schools from GIAS CSV, geocoding ...")
    out = []
    seen = set()
    for i in range(0, len(rows), 100):
        batch = rows[i:i + 100]
        try:
            resp = httpx.post("https://api.postcodes.io/postcodes",
                              json={"postcodes": [b["postcode"] for b in batch]},
                              headers=_headers(), timeout=90)
            results = resp.json().get("result", [])
        except Exception as e:
            if verbose:
                print(f"    geocode batch failed: {e}")
            continue
        for rec, g in zip(batch, results):
            g = g or {}
            r2 = g.get("result") or {}
            lat, lng = r2.get("latitude"), r2.get("longitude")
            if lat is None or lng is None or rec["postcode"] in seen:
                continue
            seen.add(rec["postcode"])
            out.append({"name": rec["name"], "lat": round(lat, 5), "lng": round(lng, 5),
                        "phase": rec["phase"], "rating": rec["rating"]})
    return out


def build_all_layers(out_path=None, verbose=False):
    """Build the complete layers.json (airports, crime, GP, flood, parks, schools)."""
    airports = fetch_airports(verbose=verbose)
    month, grid = fetch_crime(verbose=verbose)
    gps = fetch_gps(verbose=verbose)
    flood = fetch_flood(verbose=verbose)
    parks = fetch_parks(verbose=verbose)
    schools = fetch_schools(verbose=verbose)
    payload = {
        "updated": f"{month}-01",
        "airports": airports,
        "crimes": grid,
        "crime_note": "street-level crime counts, England/Wales/NI only (Scotland not published)",
        "gps": gps,
        "flood": flood,
        "flood_note": "Flood Zone 3 (rivers & sea), Environment Agency - England only",
        "parks": parks,
        "schools": schools,
        "schools_note": None if schools is not None else
            "schools layer needs a one-time GIAS download (free account) - see README",
    }
    out = pathlib.Path(out_path) if out_path else DATA_DIR.parent / "docs" / "layers.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if verbose:
        print(f"  airports {len(airports)} | crime cells {len(grid)} | gps {len(gps)} | "
              f"flood cells {len(flood)} | parks {len(parks)} | "
              f"schools {'n/a' if schools is None else len(schools)}")
    return payload
