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
