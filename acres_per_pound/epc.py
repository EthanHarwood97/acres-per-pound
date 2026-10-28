"""EPC enrichment: internal floor area for every property.

Data sources
------------
England & Wales (OGL):
  - bulk CSVs from epc.opendatacommunities.org (GOV.UK One Login for the
    download; put the zips or extracted certificates.csv files under
    data/epc/ew/), or
  - the domestic API (email + API key -> config `epc.email` / `epc.api_key`)
    for on-demand postcode lookups.
Scotland (OGL, non-address fields):
  - quarterly extract published at statistics.gov.scot (no auth).
OS Open UPRN (OGL):
  - GB address points, free download from the OS Data Hub (no auth);
    used to turn a listing's lat/lng into a UPRN so EPC joins are exact.

Matching order
--------------
  1. UPRN: listing lat/lng -> nearest OS Open UPRN point (<= N metres,
     default 60) -> EPC certificate carrying that UPRN.
  2. Full postcode + address tokens (street / house number).
  3. Postcode only, choosing the certificate whose floor area is closest
     to any portal-stated floor size.

Everything lands in the canonical store's `epc` + `uprn` tables, and
matched listings get internal_sqm / internal_sqft / epc_rating.
"""

import csv
import io
import json
import math
import pathlib
import re
import shutil
import time
import zipfile

import httpx

from . import store as store_mod
from .http import load_config

EPC_DIR = store_mod.REPO_DIR / "data" / "epc"
SCOTLAND_URL = ("https://statistics.gov.scot/downloads/file?id="
                "44bc198f-219a-42f3-9277-a656481a9bc3%2F"
                "Domestic+EPC+-+Data+to+2026+Q1.7z")
UPRN_URL = ("https://api.os.uk/downloads/v1/products/OpenUPRN/downloads"
            "?area=GB&format=CSV&redirect")
EW_API = "https://epc.opendatacommunities.org/api/v1/domestic/search"

UA = load_config()["http"]["user_agent"]

# EPC columns we keep (E&W names; Scottish extract uses the same
# TOTAL_FLOOR_AREA convention - both are mapped in _row_from_csv).
_EW_KEEP = ("LMK_KEY", "ADDRESS1", "ADDRESS2", "ADDRESS3", "POSTCODE", "UPRN",
            "TOTAL_FLOOR_AREA", "PROPERTY_TYPE", "BUILT_FORM", "INSPECTION_DATE",
            "LODGEMENT_DATE", "CURRENT_ENERGY_RATING", "TENURE", "LOCAL_AUTHORITY")


def _log(verbose, msg):
    if verbose:
        print(msg)


# --- downloads ------------------------------------------------------------

def _download(url, dest, verbose=False, chunk=1024 * 256):
    dest = pathlib.Path(dest)
    if dest.exists() and dest.stat().st_size > 0:
        _log(verbose, f"  cached {dest.name} ({dest.stat().st_size/1e6:.0f} MB)")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    _log(verbose, f"  downloading {url[:90]} ...")
    last = None
    for attempt in range(4):
        try:
            with httpx.stream("GET", url, headers={"User-Agent": UA},
                              timeout=600, follow_redirects=True) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length") or 0)
                done = 0
                with open(tmp, "wb") as f:
                    for block in r.iter_bytes(chunk):
                        f.write(block)
                        done += len(block)
                        if verbose and total and done % (50 * 1024 * 1024) < chunk:
                            print(f"    {done/1e6:.0f}/{total/1e6:.0f} MB")
            tmp.replace(dest)
            return dest
        except Exception as e:
            last = e
            time.sleep(10 * (attempt + 1))
    tmp.unlink(missing_ok=True)
    raise RuntimeError(f"download failed: {url}: {last}")


def download_scotland(cfg=None, verbose=False):
    """Fetch + extract the Scottish EPC quarterly extract (free)."""
    cfg = cfg or load_config()
    url = (cfg.get("epc") or {}).get("scotland_url") or SCOTLAND_URL
    out = EPC_DIR / "scotland"
    out.mkdir(parents=True, exist_ok=True)
    archive = _download(url, out / "scotland_epc.7z", verbose=verbose)
    extracted = list(out.glob("*.csv"))
    if extracted:
        _log(verbose, f"  {len(extracted)} csv files already extracted")
        return extracted
    try:
        import py7zr
    except ImportError:
        raise SystemExit("py7zr is required to extract the Scottish .7z "
                         "(pip install py7zr) - or extract it manually into "
                         f"{out}")
    _log(verbose, "  extracting .7z ...")
    with py7zr.SevenZipFile(archive, mode="r") as z:
        z.extractall(path=out)
    files = list(out.rglob("*.csv"))
    _log(verbose, f"  extracted {len(files)} csv files")
    return files


def download_uprn(cfg=None, verbose=False):
    """Fetch + extract OS Open UPRN (GB address points, free)."""
    cfg = cfg or load_config()
    out = EPC_DIR / "uprn"
    out.mkdir(parents=True, exist_ok=True)
    existing = list(out.glob("*.csv"))
    if existing:
        _log(verbose, f"  {len(existing)} uprn csv files already present")
        return existing
    archive = _download(UPRN_URL, out / "osopenuprn_csv.zip", verbose=verbose)
    _log(verbose, "  extracting zip ...")
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            if name.lower().endswith(".csv"):
                with z.open(name) as src, open(out / pathlib.Path(name).name, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
    files = list(out.glob("*.csv"))
    _log(verbose, f"  extracted {len(files)} csv files")
    return files


def download_ew(cfg=None, verbose=False):
    """England & Wales bulk: requires a logged-in download.

    Either drop the zips / certificates.csv files into data/epc/ew/, or
    set `epc.ew_url` to a signed download URL captured from the browser
    after signing in, or use the API key path (`epc.email` + `epc.api_key`).
    """
    cfg = cfg or load_config()
    out = EPC_DIR / "ew"
    out.mkdir(parents=True, exist_ok=True)
    existing = list(out.rglob("certificates*.csv")) + list(out.glob("*.zip"))
    if existing:
        _log(verbose, f"  {len(existing)} E&W files already present")
        return existing
    url = (cfg.get("epc") or {}).get("ew_url")
    if not url:
        raise SystemExit(
            "England & Wales EPC bulk needs a signed-in download.\n"
            "  Option A: register free at https://epc.opendatacommunities.org\n"
            "            (GOV.UK One Login) and download 'all domestic'\n"
            f"            (or per-LA) zips into {out}\n"
            "  Option B: set epc.ew_url in config.json to the signed URL you\n"
            "            get after clicking the download button.\n"
            "  Option C: set epc.email + epc.api_key in config.json and run\n"
            "            `acres epc --match` to fetch by postcode via the API.")
    return [_download(url, out / "ew_bulk.zip", verbose=verbose)]


def download_bulk(cfg=None, which="all", verbose=False):
    """Dispatcher: which in {all, ew, scotland, uprn}."""
    cfg = cfg or load_config()
    out = {}
    if which in ("all", "scotland"):
        try:
            out["scotland"] = download_scotland(cfg, verbose=verbose)
        except SystemExit:
            raise
        except Exception as e:
            print(f"  scotland download failed: {e}")
    if which in ("all", "uprn"):
        try:
            out["uprn"] = download_uprn(cfg, verbose=verbose)
        except Exception as e:
            print(f"  uprn download failed: {e}")
    if which in ("all", "ew"):
        out["ew"] = download_ew(cfg, verbose=verbose)
    return out


# --- index loading --------------------------------------------------------

def _clean_pc(pc):
    return re.sub(r"\s+", " ", (pc or "").strip().upper())


def _to_float(v):
    try:
        f = float(str(v).replace(",", "").strip())
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _load_csv_rows(conn, path, source, verbose=False):
    """Stream one EPC csv into the store. Column names are matched
    case-insensitively so both the E&W and Scottish extracts work."""
    path = pathlib.Path(path)
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        return _load_stream(conn, f, source, verbose=verbose)


def _load_stream(conn, f, source, verbose=False):
    """Parse an EPC csv from any text file object (path or zip member)."""
    n = 0
    batch = []
    reader = csv.DictReader(f)
    if not reader.fieldnames:
        return 0
    cols = {c.strip().lstrip("\ufeff").upper(): c for c in reader.fieldnames}

    def g(row, *names):
        for name in names:
            key = cols.get(name)
            if key and row.get(key):
                return row[key].strip()
        return ""

    for row in reader:
        lmk = g(row, "LMK_KEY", "LMKKEY", "CERTIFICATE_NUMBER", "REFERENCE",
                "BUILDING_REFERENCE_NUMBER", "OSG_REFERENCE_NUMBER")
        area = _to_float(g(row, "TOTAL_FLOOR_AREA", "TOTALFLOORAREA", "FLOOR_AREA"))
        pc = _clean_pc(g(row, "POSTCODE", "POST_CODE"))
        if not lmk or not pc:
            continue
        uprn = g(row, "UPRN")
        try:
            uprn_i = int(float(uprn)) if uprn else None
        except (TypeError, ValueError):
            uprn_i = None
        batch.append((
            lmk, source,
            g(row, "ADDRESS1"), g(row, "ADDRESS2"), g(row, "ADDRESS3"),
            pc, uprn_i, area,
            g(row, "PROPERTY_TYPE"), g(row, "BUILT_FORM"),
            g(row, "INSPECTION_DATE"), g(row, "LODGEMENT_DATE"),
            g(row, "CURRENT_ENERGY_RATING", "ENERGY_RATING"),
            g(row, "TENURE"), g(row, "LOCAL_AUTHORITY"),
        ))
        if len(batch) >= 20000:
            conn.executemany(
                "INSERT OR REPLACE INTO epc (lmk_key,source,address1,address2,"
                "address3,postcode,uprn,total_floor_area,property_type,built_form,"
                "inspection_date,lodgement_date,energy_rating,tenure,local_authority)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            conn.commit()
            n += len(batch)
            batch = []
            if verbose and n % 200000 == 0:
                print(f"    {n:,} rows ...")
    if batch:
        conn.executemany(
            "INSERT OR REPLACE INTO epc (lmk_key,source,address1,address2,"
            "address3,postcode,uprn,total_floor_area,property_type,built_form,"
            "inspection_date,lodgement_date,energy_rating,tenure,local_authority)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        conn.commit()
        n += len(batch)
    return n


def _load_zip(conn, zip_path, verbose=False, resume=True):
    """Stream certificates-*.csv members straight out of a bulk zip.

    The published 'all domestic' zip is ~7.7 GB containing 19
    certificates-YYYY.csv files (~28 GB uncompressed) plus far larger
    recommendations-*.csv files we never need. Streaming avoids both the
    extraction time and the disk cost. Completed members are recorded in
    the meta table so an interrupted run resumes where it stopped.
    """
    import io
    import zipfile

    total = 0
    zname = pathlib.Path(zip_path).name
    with zipfile.ZipFile(zip_path) as z:
        members = [n for n in z.namelist()
                   if pathlib.PurePosixPath(n).name.lower().startswith("certificates")
                   and n.lower().endswith(".csv")]
        if not members:
            # legacy per-local-authority layout: <LA>/certificates.csv
            members = [n for n in z.namelist()
                       if n.lower().endswith("certificates.csv")]
        if verbose:
            print(f"  {zname}: {len(members)} certificate files")
        for name in sorted(members):
            key = f"epc_loaded:{zname}:{name}"
            if resume:
                prev = store_mod.get_meta(conn, key)
                if prev:
                    _log(verbose, f"    {pathlib.Path(name).name}: already loaded ({prev:,})")
                    continue
            with z.open(name) as raw:
                f = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
                n = _load_stream(conn, f, "ew", verbose=verbose)
                total += n
            store_mod.set_meta(conn, key, n)
            _log(verbose, f"    {pathlib.Path(name).name}: {n:,} rows")
    return total


# Secondary indexes are dropped for bulk loads and rebuilt afterwards -
# maintaining them row-by-row roughly triples the load time.
_EPC_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_epc_postcode ON epc(postcode)",
    "CREATE INDEX IF NOT EXISTS idx_epc_uprn ON epc(uprn)",
)


def _drop_epc_indexes(conn):
    conn.execute("DROP INDEX IF EXISTS idx_epc_postcode")
    conn.execute("DROP INDEX IF EXISTS idx_epc_uprn")
    conn.commit()


def _create_epc_indexes(conn):
    for ddl in _EPC_INDEXES:
        conn.execute(ddl)
    conn.commit()


def load_index(conn, verbose=False, reset=False, bulk=True):
    """Load every downloaded EPC csv/zip into the store index."""
    if reset:
        conn.execute("DELETE FROM epc")
        conn.commit()
    if bulk:
        _drop_epc_indexes(conn)
    total = 0
    files = []
    for d in (EPC_DIR / "ew", EPC_DIR / "scotland"):
        if d.exists():
            files += sorted(d.rglob("*.csv"))
    for f in files:
        src = "scotland" if "scotland" in str(f).lower() else "ew"
        key = f"epc_loaded:file:{f.name}"
        if store_mod.get_meta(conn, key):
            _log(verbose, f"  {f.name}: already loaded")
            continue
        n = _load_csv_rows(conn, f, src, verbose=verbose)
        total += n
        store_mod.set_meta(conn, key, n)
        _log(verbose, f"  {f.name}: {n:,} rows")
    # bulk zips: stream the certificate members directly, skip the rest
    zips = []
    for d in (EPC_DIR / "ew", EPC_DIR):
        if d.exists():
            zips += sorted(d.glob("*.zip"))
    for z in zips:
        try:
            total += _load_zip(conn, z, verbose=verbose)
        except Exception as e:
            _log(verbose, f"  {z.name}: skipped ({e})")
    if bulk:
        if verbose:
            print("  rebuilding epc indexes ...")
        _create_epc_indexes(conn)
    _log(verbose, f"  epc index: {total:,} new rows")
    return conn.execute("SELECT COUNT(*) FROM epc").fetchone()[0]


def load_uprn_index(conn, verbose=False):
    """Load OS Open UPRN csv(s) into the uprn table with grid cells."""
    files = sorted((EPC_DIR / "uprn").glob("*.csv")) if (EPC_DIR / "uprn").exists() else []
    if not files:
        return 0
    from .inspire import wgs84_to_bng
    n = 0
    for f in files:
        with open(f, newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            cols = {c.strip().lstrip("\ufeff").upper(): c
                    for c in (reader.fieldnames or [])}
            lat_c = cols.get("LATITUDE") or cols.get("LAT")
            lon_c = cols.get("LONGITUDE") or cols.get("LON") or cols.get("LNG")
            uprn_c = cols.get("UPRN")
            e_c = cols.get("X_COORDINATE") or cols.get("EASTING")
            n_c = cols.get("Y_COORDINATE") or cols.get("NORTHING")
            if not (lat_c and lon_c and uprn_c):
                _log(verbose, f"  {f.name}: unexpected columns {(reader.fieldnames or [])[:6]}")
                continue
            batch = []
            for row in reader:
                try:
                    lat = float(row[lat_c]); lng = float(row[lon_c])
                    uprn = int(float(row[uprn_c]))
                except (TypeError, ValueError, KeyError):
                    continue
                if e_c and n_c:
                    try:
                        e, nn = float(row[e_c]), float(row[n_c])
                    except (TypeError, ValueError):
                        e, nn = wgs84_to_bng(lat, lng)
                else:
                    e, nn = wgs84_to_bng(lat, lng)
                # 100 m grid cell for the nearest-point search
                cell = (int(e // 100), int(nn // 100))
                batch.append((uprn, lat, lng, e, nn, cell[0], cell[1]))
                if len(batch) >= 50000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO uprn (uprn,lat,lng,easting,northing,"
                        "cell_e,cell_n) VALUES (?,?,?,?,?,?,?)", batch)
                    conn.commit()
                    n += len(batch); batch = []
                    if verbose and n % 500000 == 0:
                        print(f"    {n:,} uprn rows ...")
            if batch:
                conn.executemany(
                    "INSERT OR REPLACE INTO uprn (uprn,lat,lng,easting,northing,"
                    "cell_e,cell_n) VALUES (?,?,?,?,?,?,?)", batch)
                conn.commit()
                n += len(batch)
        _log(verbose, f"  {f.name}: {n:,} rows")
    return conn.execute("SELECT COUNT(*) FROM uprn").fetchone()[0]


# --- matching -------------------------------------------------------------

def _tokens(s):
    return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) > 2}


def _house_no(s):
    """Leading house number of an address ('29, Montrose Street' -> '29')."""
    m = re.match(r"\s*(\d+[a-z]?)\b", (s or "").strip(), re.I)
    return m.group(1).lower() if m else ""


def _match_by_uprn(conn, listings, max_m, verbose=False):
    """listing lat/lng -> nearest UPRN cell -> EPC row (generator of
    (uid, uprn, cert))."""
    from .inspire import wgs84_to_bng

    cur = conn.cursor()
    for uid, row in listings.items():
        lat, lng = row.get("lat"), row.get("lng")
        if lat is None or lng is None:
            continue
        e, n = wgs84_to_bng(lat, lng)
        ce, cn = int(e // 100), int(n // 100)
        best = None
        best_d2 = float("inf")
        # 3x3 cells = 300m square, enough for the 60m default
        rows = cur.execute(
            "SELECT uprn, easting, northing FROM uprn WHERE cell_e BETWEEN ? AND ? "
            "AND cell_n BETWEEN ? AND ?", (ce - 1, ce + 1, cn - 1, cn + 1)).fetchall()
        for uprn, ue, un in rows:
            d2 = (ue - e) ** 2 + (un - n) ** 2
            if d2 < best_d2:
                best_d2, best = d2, uprn
        if best is None or math.sqrt(best_d2) > max_m:
            continue
        cert = cur.execute(
            "SELECT total_floor_area, energy_rating, inspection_date FROM epc "
            "WHERE uprn=? AND total_floor_area IS NOT NULL "
            "ORDER BY inspection_date DESC LIMIT 1", (best,)).fetchone()
        if cert and cert[0]:
            yield uid, best, cert


def match_listings(conn, verbose=False, limit=None):
    """Attach internal floor areas to listings (UPRN first, postcode second)."""
    cfg = load_config()
    ecfg = cfg.get("epc") or {}
    max_m = float(ecfg.get("uprn_max_distance_m", 60))

    listings = {uid: row for uid, row in store_mod.listings_map(conn).items()
                if row.get("internal_sqm") is None and not row.get("land_only")
                and not row.get("excluded")}
    if limit:
        listings = dict(list(listings.items())[:limit])
    _log(verbose, f"  {len(listings):,} listings need floor area")
    if not listings:
        return 0

    updates = []
    uprn_matched = 0
    for uid, uprn, cert in _match_by_uprn(conn, listings, max_m, verbose=verbose):
        area, rating, date = cert
        updates.append((uid, {
            "internal_sqm": round(area, 1),
            "internal_sqft": round(area * 10.7639, 0),
            "internal_source": "epc",
            "internal_conf": "uprn",
            "uprn": uprn,
            "epc_rating": rating,
            "epc_date": date,
        }))
        uprn_matched += 1
    if updates:
        store_mod.update_many(conn, updates)
    _log(verbose, f"  uprn matches: {uprn_matched:,}")

    # postcode fallback for the rest
    done = {u for u, _ in updates}
    rest = {uid: r for uid, r in listings.items() if uid not in done}
    pc_matched = _match_by_postcode(conn, rest, verbose=verbose)
    _log(verbose, f"  postcode matches: {pc_matched:,}")
    return uprn_matched + pc_matched


def _match_by_postcode(conn, listings, verbose=False):
    """Full postcode + house number + address-token scoring.

    Flats are excluded upstream; for houses the house number is
    decisive, and a shared street token is required so a postcode-wide
    best guess can never silently pick a different street.
    """
    matched = 0
    cur = conn.cursor()
    updates = []
    by_pc = {}
    for uid, row in listings.items():
        pc = _clean_pc(row.get("postcode") or "")
        if len(pc) < 5:
            continue
        by_pc.setdefault(pc, []).append((uid, row))
    for pc, items in by_pc.items():
        certs = cur.execute(
            "SELECT lmk_key, address1, address2, total_floor_area, energy_rating, "
            "inspection_date FROM epc WHERE postcode=? AND total_floor_area IS NOT NULL",
            (pc,)).fetchall()
        if not certs:
            continue
        for uid, row in items:
            toks = _tokens(row.get("address"))
            if not toks:
                continue
            no = _house_no(row.get("address"))
            floor = row.get("floor_sqm")
            if not floor and row.get("internal_sqm"):
                floor = row["internal_sqm"] * 10.7639
            best, best_score = None, 0.0
            for c in certs:
                ctoks = _tokens(f"{c[1]} {c[2]}")
                shared = toks & ctoks
                if not shared:
                    continue  # different street: never a match
                cno = _house_no(c[1])
                if no and cno and no != cno:
                    continue  # different house number: reject
                score = len(shared) / max(len(toks), 1)
                if no and cno == no:
                    score += 0.5
                if floor and c[3]:
                    try:
                        f = float(floor)
                        if abs(float(c[3]) - f) <= max(3.0, 0.15 * f):
                            score += 0.25
                    except (TypeError, ValueError):
                        pass
                if score > best_score:
                    best, best_score = c, score
            if best and best_score >= 0.4:
                area, rating, date = best[3], best[4], best[5]
                updates.append((uid, {
                    "internal_sqm": round(area, 1),
                    "internal_sqft": round(area * 10.7639, 0),
                    "internal_source": "epc",
                    "internal_conf": "postcode",
                    "epc_rating": rating,
                    "epc_date": date,
                }))
                matched += 1
    if updates:
        store_mod.update_many(conn, updates)
    return matched


def stats(conn):
    c = conn.cursor()
    out = {
        "epc_rows": c.execute("SELECT COUNT(*) FROM epc").fetchone()[0],
        "epc_with_area": c.execute(
            "SELECT COUNT(*) FROM epc WHERE total_floor_area IS NOT NULL").fetchone()[0],
        "uprn_rows": c.execute("SELECT COUNT(*) FROM uprn").fetchone()[0],
        "listings": c.execute("SELECT COUNT(*) FROM listings WHERE active=1").fetchone()[0],
        "with_internal": c.execute(
            "SELECT COUNT(*) FROM listings WHERE active=1 AND internal_sqm IS NOT NULL").fetchone()[0],
    }
    for conf, n in c.execute(
            "SELECT internal_conf, COUNT(*) FROM listings WHERE active=1 "
            "AND internal_conf IS NOT NULL GROUP BY internal_conf"):
        out[f"conf_{conf}"] = n
    return out
