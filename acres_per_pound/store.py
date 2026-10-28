"""Canonical listing store (SQLite).

The JSON state file was fine for one source and 100k rows; multi-portal
scanning, EPC/plot enrichment and the Cloudflare search index all need
real queries, indexes and cheap field updates. This module owns:

  - the schema (listings + enrichment tables)
  - migration from the legacy snapshots/state.json
  - upserts from scanner rows
  - field updates from enrichment passes (EPC, INSPIRE, RoS, sold)
  - the state.json-shaped export that publish.py still consumes

Listing identity is `uid = "<source>:<source_id>"` (e.g. rm:123).
The legacy `rm_id` field is preserved on Rightmove rows.
"""

import json
import pathlib
import re
import sqlite3
import time

REPO_DIR = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_DIR / "data" / "apf.db"

# Columns persisted from a scanner row (JSON key -> column).
_LISTING_COLUMNS = (
    "uid", "source", "source_id", "url", "dedupe_key",
    "address", "postcode", "outcode", "lat", "lng",
    "price", "price_text", "price_qualifier",
    "beds", "baths", "receptions",
    "property_type", "subtype", "tenure", "council_tax",
    "land_only", "summary", "features_json",
    "internal_sqm", "internal_sqft", "internal_source", "internal_conf",
    "epc_rating", "epc_date", "uprn",
    "plot_sqm", "plot_sqft", "plot_source", "plot_id", "plot_conf",
    "est_shared", "est_checked", "est_acres", "est_plot_m2", "inspire_id",
    "acres_min", "acres_max", "acres_mid", "acre_unit",
    "confidence", "matched", "communal",
    "region_id", "region_name", "county", "country", "is_highlands",
    "first_seen", "last_seen", "active", "listing_status", "first_published",
    "detail_checked",
    "sold_price", "sold_date", "sold_gbp_per_acre", "sold_confidence",
    "gbp_per_acre", "acres_per_100k", "gbp_per_in_sqft", "gbp_per_ex_sqft",
    "updated_ts",
)

# Columns added after the first release; applied via ALTER TABLE so
# existing databases upgrade in place.
_MIGRATIONS = (
    "est_shared INTEGER DEFAULT 0",
    "est_checked INTEGER DEFAULT 0",
    "est_acres REAL",
    "est_plot_m2 REAL",
    "inspire_id TEXT",
    "synced_hash TEXT",
    "excluded INTEGER DEFAULT 0",
    "acres_eff REAL",
    "plot_eff_sqm REAL",
    "plot_eff_src TEXT",
    "shared_reason TEXT",
    "region_median REAL",
    "value_ratio REAL",
)

# JSON key aliases: scanner rows carry these names, columns differ slightly.
_ALIASES = {
    "features_json": "features",
    "property_type": "type_full",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    uid             TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    url             TEXT,
    dedupe_key      TEXT,
    address         TEXT,
    postcode        TEXT,
    outcode         TEXT,
    lat             REAL,
    lng             REAL,
    price           INTEGER,
    price_text      TEXT,
    price_qualifier TEXT,
    beds            INTEGER,
    baths           INTEGER,
    receptions      INTEGER,
    property_type   TEXT,
    subtype         TEXT,
    tenure          TEXT,
    council_tax     TEXT,
    land_only       INTEGER DEFAULT 0,
    summary         TEXT,
    features_json   TEXT,
    internal_sqm    REAL,
    internal_sqft   REAL,
    internal_source TEXT,
    internal_conf   TEXT,
    epc_rating      TEXT,
    epc_date        TEXT,
    uprn            INTEGER,
    plot_sqm        REAL,
    plot_sqft       REAL,
    plot_source     TEXT,
    plot_id         TEXT,
    plot_conf       TEXT,
    est_shared      INTEGER DEFAULT 0,
    est_checked     INTEGER DEFAULT 0,
    est_acres       REAL,
    est_plot_m2     REAL,
    inspire_id      TEXT,
    excluded        INTEGER DEFAULT 0,
    acres_eff       REAL,
    plot_eff_sqm    REAL,
    plot_eff_src    TEXT,
    acres_min       REAL,
    acres_max       REAL,
    acres_mid       REAL,
    acre_unit       TEXT,
    confidence      TEXT,
    matched         TEXT,
    communal        INTEGER,
    region_id       TEXT,
    region_name     TEXT,
    county          TEXT,
    country         TEXT,
    is_highlands    INTEGER DEFAULT 0,
    first_seen      TEXT,
    last_seen       TEXT,
    active          INTEGER DEFAULT 1,
    listing_status  TEXT,
    first_published TEXT,
    detail_checked  INTEGER DEFAULT 0,
    sold_price      INTEGER,
    sold_date       TEXT,
    sold_gbp_per_acre REAL,
    sold_confidence TEXT,
    gbp_per_acre    REAL,
    acres_per_100k  REAL,
    gbp_per_in_sqft REAL,
    gbp_per_ex_sqft REAL,
    updated_ts      TEXT
);
CREATE INDEX IF NOT EXISTS idx_listings_active  ON listings(active);
CREATE INDEX IF NOT EXISTS idx_listings_source  ON listings(source);
CREATE INDEX IF NOT EXISTS idx_listings_price   ON listings(price);
CREATE INDEX IF NOT EXISTS idx_listings_region  ON listings(region_name);
CREATE INDEX IF NOT EXISTS idx_listings_postcode ON listings(postcode);
CREATE INDEX IF NOT EXISTS idx_listings_dedupe  ON listings(dedupe_key);

-- EPC certificates (England & Wales bulk + Scottish extracts).
CREATE TABLE IF NOT EXISTS epc (
    lmk_key          TEXT PRIMARY KEY,
    source           TEXT NOT NULL DEFAULT 'ew',
    address1         TEXT,
    address2         TEXT,
    address3         TEXT,
    postcode         TEXT,
    uprn             INTEGER,
    total_floor_area REAL,
    property_type    TEXT,
    built_form       TEXT,
    inspection_date  TEXT,
    lodgement_date   TEXT,
    energy_rating    TEXT,
    tenure           TEXT,
    local_authority  TEXT
);
CREATE INDEX IF NOT EXISTS idx_epc_postcode ON epc(postcode);
CREATE INDEX IF NOT EXISTS idx_epc_uprn     ON epc(uprn);

-- OS Open UPRN address points (Great Britain) for spatial joins.
CREATE TABLE IF NOT EXISTS uprn (
    uprn    INTEGER PRIMARY KEY,
    lat     REAL,
    lng     REAL,
    easting REAL,
    northing REAL,
    cell_e  INTEGER,
    cell_n  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_uprn_cell    ON uprn(cell_e, cell_n);
CREATE INDEX IF NOT EXISTS idx_uprn_easting ON uprn(easting, northing);

-- Registered title boundaries, simplified and converted to WGS84 at
-- enrichment time (the worker cannot run a CRS transform). Keyed by the
-- polygon id so one title is stored once however many listings sit in it.
CREATE TABLE IF NOT EXISTS plot_geom (
    plot_id     TEXT PRIMARY KEY,
    geojson     TEXT NOT NULL,
    acres       REAL,
    source      TEXT,
    synced_hash TEXT,
    updated_ts  TEXT
);

-- Per-region medians. The D1 `regions` table is a copy of this; both
-- must hold true medians, not averages.
CREATE TABLE IF NOT EXISTS regions (
    region       TEXT PRIMARY KEY,
    country      TEXT,
    highlands    INTEGER DEFAULT 0,
    n            INTEGER,
    med_gbp_ac   REAL,
    med_gbp_in   REAL,
    med_gbp_ex   REAL,
    med_acres    REAL,
    med_in_sqft  REAL,
    med_ex_sqft  REAL,
    updated_ts   TEXT
);

-- Scan bookkeeping so cycles are resumable / auditable.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path=None):
    p = pathlib.Path(path or DEFAULT_DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    init_db(conn)
    return conn


def init_db(conn):
    conn.executescript(_SCHEMA)
    # upgrade older databases in place
    have = {r[1] for r in conn.execute("PRAGMA table_info(listings)")}
    for ddl in _MIGRATIONS:
        col = ddl.split()[0]
        if col not in have:
            try:
                conn.execute(f"ALTER TABLE listings ADD COLUMN {ddl}")
            except sqlite3.OperationalError:
                pass
    conn.commit()


def annotate_regions(conn, verbose=False):
    """Fill country / is_highlands from region_name (used by the API)."""
    from .regions import classify_region

    rows = conn.execute(
        "SELECT DISTINCT region_name FROM listings "
        "WHERE region_name IS NOT NULL AND (country IS NULL OR is_highlands IS NULL)"
    ).fetchall()
    n = 0
    for (name,) in rows:
        cls = classify_region(name or "")
        cur = conn.execute(
            "UPDATE listings SET country=?, is_highlands=? WHERE region_name=?",
            (cls["country"], 1 if cls["is_highlands"] else 0, name))
        n += cur.rowcount
    conn.commit()
    if verbose:
        print(f"regions annotated: {len(rows)} regions, {n} rows")
    return n


# --- row conversion ------------------------------------------------------

def _json_val(key, val):
    if key == "features_json":
        return json.dumps(val or [], ensure_ascii=False)
    return val


def _to_db(row):
    """Map a scanner/state JSON row to column values."""
    out = {}
    for col in _LISTING_COLUMNS:
        src_key = None
        for alias, target in _ALIASES.items():
            if target == col:
                src_key = alias
                break
        if src_key is None:
            src_key = col
        v = row.get(src_key)
        if col == "features_json" and v is None:
            v = row.get("features")
        if col == "land_only":
            v = 1 if v else 0
        if col == "communal":
            v = 1 if v else 0
        if col == "is_highlands":
            v = 1 if v else 0
        if col == "detail_checked":
            v = 1 if v else 0
        if col == "active":
            v = 0 if v is False else 1
        if col == "features_json":
            v = json.dumps(v or [], ensure_ascii=False)
        out[col] = v
    uid = out.get("uid")
    if uid and ":" in uid:
        src, sid = uid.split(":", 1)
    else:
        sid = str(row.get("rm_id") or row.get("source_id") or "")
        src = row.get("source") or "rightmove"
        uid = f"{src}:{sid}" if sid else ""
    out["uid"] = uid
    out["source"] = src or "rightmove"
    out["source_id"] = str(row.get("rm_id") or row.get("source_id") or sid or "")
    # portal-stated floor size (Rightmove displaySize, Zoopla floorSize)
    # becomes the internal size until EPC supplies an authoritative one.
    if out.get("internal_sqm") is None and row.get("floor_sqm"):
        try:
            v = float(row["floor_sqm"])
            out["internal_sqm"] = round(v, 1)
            out["internal_sqft"] = round(v * 10.7639, 0)
            out["internal_source"] = "portal"
            out["internal_conf"] = "stated"
        except (TypeError, ValueError):
            pass
    if not out.get("updated_ts"):
        out["updated_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    return out


def upsert_many(conn, rows):
    """Insert or update listing rows. Returns (inserted, updated)."""
    cur = conn.cursor()
    inserted = updated = 0
    cols = ", ".join(_LISTING_COLUMNS)
    ph = ", ".join("?" for _ in _LISTING_COLUMNS)
    upd = ", ".join(f"{c}=excluded.{c}" for c in _LISTING_COLUMNS if c != "uid")
    sql = (f"INSERT INTO listings ({cols}) VALUES ({ph}) "
           f"ON CONFLICT(uid) DO UPDATE SET {upd}")
    for row in rows:
        d = _to_db(row)
        if not d["uid"]:
            continue
        exists = cur.execute("SELECT 1 FROM listings WHERE uid=?", (d["uid"],)).fetchone()
        cur.execute(sql, [d[c] for c in _LISTING_COLUMNS])
        if exists:
            updated += 1
        else:
            inserted += 1
    conn.commit()
    return inserted, updated


def update_fields(conn, uid, **fields):
    """Patch arbitrary columns on one listing."""
    if not fields:
        return
    fields["updated_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE listings SET {sets} WHERE uid=?", (*fields.values(), uid))


def update_many(conn, updates):
    """updates: iterable of (uid, {col: value})."""
    for uid, fields in updates:
        update_fields(conn, uid, **fields)
    conn.commit()


def save_plot_geoms(conn, items):
    """Store simplified title boundaries keyed by polygon id.

    items: iterable of (plot_id, geojson, acres, source). The geometry is
    already in WGS84 (converted at enrichment time) because the Worker
    cannot run a CRS transform.
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    rows = [(gid, gj, round(acres, 3), src, ts)
            for gid, gj, acres, src in items if gid and gj]
    if not rows:
        return 0
    conn.executemany("""
        INSERT INTO plot_geom (plot_id, geojson, acres, source, updated_ts)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(plot_id) DO UPDATE SET
            geojson=excluded.geojson, acres=excluded.acres,
            source=excluded.source, updated_ts=excluded.updated_ts,
            synced_hash=CASE WHEN plot_geom.geojson = excluded.geojson
                             THEN plot_geom.synced_hash END
    """, rows)
    conn.commit()
    return len(rows)


# --- state.json compatibility -------------------------------------------

def _from_db_row(r):
    """DB row -> state.json shaped dict (features list restored)."""
    d = {k: r[k] for k in r.keys()}
    feats = d.pop("features_json", None)
    if feats:
        try:
            d["features"] = json.loads(feats)
        except Exception:
            d["features"] = []
    else:
        d["features"] = []
    d["rm_id"] = d.get("source_id") if d.get("source") == "rightmove" else d.get("rm_id")
    for k in ("land_only", "communal", "is_highlands", "detail_checked"):
        if d.get(k) is not None:
            d[k] = bool(d[k])
    if d.get("active") is not None:
        d["active"] = bool(d["active"])
    return {k: v for k, v in d.items() if v is not None}


def iter_listings(conn, active_only=True, source=None):
    q = "SELECT * FROM listings"
    where, args = [], []
    if active_only:
        where.append("active=1")
    if source:
        where.append("source=?")
        args.append(source)
    if where:
        q += " WHERE " + " AND ".join(where)
    for r in conn.execute(q, args):
        yield _from_db_row(r)


def listings_map(conn, active_only=True, include_excluded=False):
    q = "SELECT * FROM listings"
    where = []
    if active_only:
        where.append("active=1")
    if not include_excluded:
        where.append("excluded=0")
    if where:
        q += " WHERE " + " AND ".join(where)
    return {r["uid"]: _from_db_row(r) for r in conn.execute(q)}


def counts(conn):
    c = conn.cursor()
    out = {"total": c.execute("SELECT COUNT(*) FROM listings").fetchone()[0],
           "active": c.execute(
               "SELECT COUNT(*) FROM listings WHERE active=1 AND excluded=0").fetchone()[0],
           "excluded": c.execute(
               "SELECT COUNT(*) FROM listings WHERE active=1 AND excluded=1").fetchone()[0]}
    for src, n in c.execute(
            "SELECT source, COUNT(*) FROM listings WHERE active=1 AND excluded=0 "
            "GROUP BY source"):
        out[f"active_{src}"] = n
    out["with_internal"] = c.execute(
        "SELECT COUNT(*) FROM listings WHERE active=1 AND excluded=0 "
        "AND internal_sqm IS NOT NULL").fetchone()[0]
    out["with_plot"] = c.execute(
        "SELECT COUNT(*) FROM listings WHERE active=1 AND excluded=0 "
        "AND plot_sqm IS NOT NULL").fetchone()[0]
    out["with_acres"] = c.execute(
        "SELECT COUNT(*) FROM listings WHERE active=1 AND excluded=0 "
        "AND acres_mid IS NOT NULL").fetchone()[0]
    out["with_acre_metric"] = c.execute(
        "SELECT COUNT(*) FROM listings WHERE active=1 AND excluded=0 "
        "AND gbp_per_acre IS NOT NULL").fetchone()[0]
    out["with_plot_geom"] = c.execute(
        "SELECT COUNT(*) FROM listings l JOIN plot_geom g ON g.plot_id = l.plot_id "
        "WHERE l.active=1 AND l.excluded=0").fetchone()[0]
    return out


def mark_excluded(conn, subtypes, verbose=False):
    """Flag listings whose subtype is out of scope (flats etc.).

    Kept in the store for the record but filtered out of the API, the
    D1 index and every enrichment pass.
    """
    subs = [s.lower() for s in (subtypes or []) if s]
    conn.execute("UPDATE listings SET excluded=0")
    if subs:
        ph = ",".join("?" * len(subs))
        conn.execute(
            f"UPDATE listings SET excluded=1 WHERE lower(subtype) IN ({ph}) OR "
            f"lower(property_type) IN ({ph})", subs + subs)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM listings WHERE excluded=1").fetchone()[0]
    if verbose:
        print(f"excluded {n:,} out-of-scope listings ({len(subs)} subtypes)")
    return n


# --- migration -----------------------------------------------------------

def import_state_json(conn, path=None, verbose=False):
    """One-shot migration of snapshots/state.json into the store."""
    p = pathlib.Path(path or REPO_DIR / "snapshots" / "state.json")
    if not p.exists():
        raise SystemExit(f"no state file at {p}")
    state = json.loads(p.read_text(encoding="utf-8"))
    rows = []
    for rm_id, row in (state.get("listings") or {}).items():
        row = dict(row)
        row.setdefault("rm_id", rm_id)
        row.setdefault("source", "rightmove")
        row["source_id"] = str(row.get("rm_id") or rm_id)
        row["uid"] = f"rightmove:{row['source_id']}"
        # est_plot_m2 carried the INSPIRE area before plot_sqm existed
        if row.get("plot_sqm") is None and row.get("est_plot_m2"):
            row["plot_sqm"] = row["est_plot_m2"]
            row["plot_source"] = "inspire"
        rows.append(row)
    ins, upd = upsert_many(conn, rows)
    if verbose:
        print(f"imported {len(rows)} listings ({ins} new, {upd} updated)")
    return len(rows)


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()


def get_meta(conn, key, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r[0])
    except Exception:
        return r[0]


# --- derived metrics -----------------------------------------------------

SQFT_PER_SQM = 10.7639
MIN_PRICE_FOR_METRICS = 10000  # £1k/POA placeholders must not rank

# Registered plots above these sizes are implausible for the property
# type - but the product is *about* houses with land, so a detached house
# with a few acres is the target, not an error. Ceilings only fire when
# the listing's own stated acreage does not corroborate the polygon.
_SUBTYPE_PLOT_CEILINGS = (
    ("terraced", 0.5),
    ("end of terrace", 0.5),
    ("terraced bungalow", 0.5),
    ("mews", 0.6),
    ("coach house", 0.6),
    ("garages", 0.6),
    ("garage", 0.2),
    ("parking", 0.1),
    ("parking space", 0.1),
    ("semi-detached", 1.0),
    ("semi-detached bungalow", 1.0),
    ("semi-detached villa", 1.0),
    ("link detached house", 1.0),
    ("town house", 1.2),
    ("chalet", 3.0),
    ("detached", 6.0),
    ("detached bungalow", 6.0),
    ("bungalow", 6.0),
    ("house", 6.0),
    ("not specified", 6.0),
)

# A stated acreage within this band of the polygon means the two
# independent sources agree: the title is this property's land.
# COALESCE matters: without it a missing stated acreage makes the whole
# predicate NULL, and `NOT NULL` never fires - the exact rows that most
# need a ceiling (no ad figure, huge parcel) would slip through.
_CORROBORATED = (
    "COALESCE(acres_mid > 0 AND plot_sqm > 0 "
    "AND acres_mid * 4046.86 BETWEEN plot_sqm * 0.5 AND plot_sqm * 2.0, 0)"
)

# An uncorroborated registered polygon larger than this is a registration
# or estate parcel, not one property's land (Scottish RoS parcels are
# especially coarse). Corroborated holdings of any size are kept.
_ABSURD_PLOT_ACRES = 50.0

# An ad claiming this many times the registered polygon, in absolute
# terms, is describing the wider estate or island rather than the plot
# ("the island extends to 2,948 hectares" on a 0.25-acre croft).
_ESTATE_COPY_RATIO = 20
_ESTATE_COPY_MIN_ACRES = 50.0


# Stated acreage above these sizes is a parse of marketing copy (the
# wider estate/village) rather than the property's own land, and is
# ignored. Detached houses keep a very high ceiling because genuine
# estates and farms are sold with a house on them; the estate-copy
# patterns in landparse.py catch those cases instead.
_SUBTYPE_STATED_CEILINGS = (
    ("terraced", 5.0),
    ("end of terrace", 5.0),
    ("terraced bungalow", 5.0),
    ("mews", 5.0),
    ("coach house", 5.0),
    ("garages", 5.0),
    ("chalet", 5.0),
    ("town house", 10.0),
    ("link detached house", 10.0),
    ("semi-detached", 10.0),
    ("semi-detached bungalow", 10.0),
    ("semi-detached villa", 10.0),
    ("detached", 1000.0),
    ("detached bungalow", 1000.0),
    ("bungalow", 1000.0),
    ("house", 1000.0),
    ("cottage", 1000.0),
    ("villa", 1000.0),
    ("character property", 1000.0),
    ("not specified", 1000.0),
    ("barn conversion", 50.0),
)


def mark_communal_acreage(conn, verbose=False):
    """Flag stated acreages that cannot be this property's own land.

    Estate-agent copy regularly quotes the wider development ("set
    within 500 acres of clay country", "a private 550 acre estate").
    The parser catches most of it; this catches what slips through by
    property type, where an acreage is simply impossible. A figure the
    registered polygon corroborates is never flagged.
    """
    cur = conn.cursor()
    n = cur.execute(
        "UPDATE listings SET communal = 0 WHERE active=1 AND excluded=0 AND communal = 1"
    ).rowcount
    for subtype, ceiling in _SUBTYPE_STATED_CEILINGS:
        n += cur.execute(f"""
            UPDATE listings SET communal = 1
            WHERE active=1 AND excluded=0 AND land_only = 0 AND communal = 0
              AND LOWER(COALESCE(subtype, '')) = ? AND acres_mid > ?
              AND NOT ({_CORROBORATED})
        """, (subtype, ceiling)).rowcount
    n += cur.execute(f"""
        UPDATE listings SET communal = 1
        WHERE active=1 AND excluded=0 AND communal = 0
          AND acres_mid > ? AND est_acres > 0
          AND acres_mid > est_acres * ?
    """, (_ESTATE_COPY_MIN_ACRES, _ESTATE_COPY_RATIO)).rowcount
    conn.commit()
    if verbose and n:
        print(f"communal acreage flagged: {n:,} implausible stated figures")
    return n


def mark_shared_plots(conn, verbose=False):
    """Flag registered plots that cannot belong to a single dwelling.

    Two independent signals:

      1. Reuse - the same title polygon matched to listings at different
         locations is a developer or communal title (a whole street's
         land), not any one house's garden. Two portals listing the same
         property share a coordinate, so they are not counted as reuse.
      2. Implausible size for the property type.

    Both are overridden when the listing's own stated acreage agrees with
    the polygon (within 0.5x-2x): two independent sources agreeing means
    the title really is this property's land, however large it is.

    The pass is recomputed from scratch on every run, so a rule change
    brings previously hidden listings back instead of freezing them.
    """
    cur = conn.cursor()
    cur.execute("""
        UPDATE listings SET est_shared = 0, shared_reason = NULL
        WHERE est_shared = 1 OR shared_reason IS NOT NULL
    """)
    n1 = cur.execute(f"""
        UPDATE listings SET est_shared = 1, shared_reason = 'reused'
        WHERE active=1 AND excluded=0 AND plot_id IS NOT NULL
          AND plot_id IN (
            SELECT plot_id FROM listings
            WHERE active=1 AND excluded=0 AND plot_id IS NOT NULL
            GROUP BY plot_id
            HAVING COUNT(DISTINCT ROUND(lat, 4) || ',' || ROUND(lng, 4)) > 1)
          AND NOT ({_CORROBORATED})
    """).rowcount
    n2 = 0
    for subtype, ceiling in _SUBTYPE_PLOT_CEILINGS:
        n2 += cur.execute(f"""
            UPDATE listings SET est_shared = 1, shared_reason = 'type_size'
            WHERE active=1 AND excluded=0 AND land_only = 0 AND est_shared = 0
              AND LOWER(COALESCE(subtype, '')) = ? AND est_acres > ?
              AND NOT ({_CORROBORATED})
        """, (subtype, ceiling)).rowcount
    n3 = cur.execute(f"""
        UPDATE listings SET est_shared = 1, shared_reason = 'absurd_size'
        WHERE active=1 AND excluded=0 AND est_shared = 0
          AND est_acres > ?
          AND NOT ({_CORROBORATED})
    """, (_ABSURD_PLOT_ACRES,)).rowcount
    conn.commit()
    if verbose:
        print(f"shared plots flagged: {n1:,} reused polygons, "
              f"{n2:,} implausible sizes, {n3:,} absurd sizes")
    return n1 + n2 + n3


def recompute_metrics(conn, verbose=False):
    """(Re)compute £/acre, £/land-sqft and £/internal-sqft for all rows.

    Listings priced below MIN_PRICE_FOR_METRICS are almost always POA
    placeholders ("£1,000"), so their value ratios are left NULL.

    Also materialises `acres_eff`: the acreage the UI actually shows
    (stated, else the registered plot, minus shared titles and sliver
    estimates). Sorting and display must agree, so both read this column.
    """
    from .http import load_config

    enrich = load_config().get("enrich", {})
    house_floor = float(enrich.get("house_min_est_acres", 0.01))
    land_floor = float(enrich.get("est_min_acres", 0.15))

    mark_shared_plots(conn, verbose=verbose)
    mark_communal_acreage(conn, verbose=verbose)

    cur = conn.cursor()
    cur.execute("""
        UPDATE listings SET acres_eff = COALESCE(
            CASE WHEN acres_mid > 0 AND communal = 0 THEN acres_mid END,
            CASE WHEN est_shared = 0 THEN
                CASE WHEN land_only = 1
                     THEN CASE WHEN est_acres >= :land_floor THEN est_acres END
                     ELSE CASE WHEN est_acres >= :house_floor THEN est_acres END
                END
            END)
    """, {"land_floor": land_floor, "house_floor": house_floor})
    # Effective land area for the £/land-sqft metric. The registered
    # polygon is authoritative for a title, but on farms and holdings it
    # often covers only the house curtilage (or is a large shared title)
    # while the listing states the whole acreage - so a stated figure
    # always wins when the polygon is missing, shared, or much smaller.
    _STATED_WINS = ("acres_mid > 0 AND communal = 0 AND (plot_sqm IS NULL "
                    "OR est_shared = 1 OR acres_mid * 4046.86 > plot_sqm * 2)")
    cur.execute(f"""
        UPDATE listings SET
            plot_eff_sqm = CASE
                WHEN {_STATED_WINS} THEN ROUND(acres_mid * 4046.86, 1)
                WHEN est_shared = 1 THEN NULL
                ELSE plot_sqm END,
            plot_eff_src = CASE
                WHEN {_STATED_WINS} THEN 'stated'
                WHEN est_shared = 1 THEN NULL
                WHEN plot_sqm IS NOT NULL THEN plot_source
                ELSE NULL END
    """)
    cur.execute(f"""
        UPDATE listings SET
            gbp_per_acre = CASE
                WHEN price >= {MIN_PRICE_FOR_METRICS} AND acres_eff > 0
                    THEN ROUND(price / acres_eff, 2)
                ELSE NULL END,
            acres_per_100k = CASE
                WHEN price >= {MIN_PRICE_FOR_METRICS} AND acres_eff > 0
                    THEN ROUND(acres_eff / (price / 100000.0), 3)
                ELSE NULL END,
            gbp_per_ex_sqft = CASE
                WHEN price >= {MIN_PRICE_FOR_METRICS} AND plot_eff_sqm > 0
                    THEN ROUND(price / (plot_eff_sqm * 10.7639), 2)
                ELSE NULL END,
            gbp_per_in_sqft = CASE
                WHEN price >= {MIN_PRICE_FOR_METRICS} AND internal_sqm > 0
                    THEN ROUND(price / (internal_sqm * 10.7639), 2)
                ELSE NULL END
        WHERE active=1
    """)
    refresh_regions(conn, verbose=verbose)
    conn.commit()
    if verbose:
        print(f"metrics recomputed for {cur.rowcount} active rows")
    return cur.rowcount


_MEDIAN_FIELDS = (
    ("med_gbp_ac", "gbp_per_acre"),
    ("med_gbp_in", "gbp_per_in_sqft"),
    ("med_gbp_ex", "gbp_per_ex_sqft"),
    ("med_acres", "acres_eff"),
    ("med_in_sqft", "internal_sqft"),
    ("med_ex_sqft", "plot_sqft"),
)

# £/acre is only comparable between properties that actually have land: a
# terraced house on a 0.01-acre garden is £20m/acre, which would drag a
# region's "median land value" into the millions and make every real
# land listing look like a bargain. Both the region median and the
# per-row value ratio use this band.
LAND_COMPARE_MIN_ACRES = 0.5
_LAND_BANDS = ((2.0,), (5.0,), (20.0,), (100.0,), (float("inf"),))


def _land_band(acres):
    for i, (hi,) in enumerate(_LAND_BANDS):
        if acres < hi:
            return i
    return len(_LAND_BANDS) - 1


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def refresh_regions(conn, verbose=False):
    """Rebuild the per-region medians and the row-level value ratio.

    `value_ratio = region median / row value`, so above 1 means the
    property is cheaper per acre than its region. Medians are computed
    in Python because SQLite has no median aggregate.
    """
    rows = conn.execute("""
        SELECT uid, region_name, country, is_highlands,
               gbp_per_acre, gbp_per_in_sqft, gbp_per_ex_sqft,
               acres_eff, internal_sqft, plot_sqft, land_only
        FROM listings
        WHERE active=1 AND excluded=0 AND region_name IS NOT NULL
    """).fetchall()

    by = {}
    for r in rows:
        by.setdefault(r[1], []).append(r)

    ts = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    payload = []
    for name, rs in by.items():
        med = []
        for i in (4, 5, 6, 7, 8, 9):
            vals = [r[i] for r in rs]
            if i == 4:  # gbp_per_acre: land-bearing properties only
                vals = [v for r, v in zip(rs, vals)
                        if (r[7] or 0) >= LAND_COMPARE_MIN_ACRES]
            med.append(_median(vals))
        payload.append((name, rs[0][2], rs[0][3] or 0, len(rs), *med, ts))

    # £/acre falls as plot size rises, so a 350-acre holding compared with
    # a region of half-acre plots always looks like a steal. Compare each
    # listing with its region *and* size band; fall back to the national
    # band median when a cell is too thin.
    cells, national = {}, {}
    for r in rows:
        acres, value = r[7] or 0, r[4]
        if value is None or acres < LAND_COMPARE_MIN_ACRES:
            continue
        key = (r[1], int(r[10] or 0), _land_band(acres))
        cells.setdefault(key, []).append(value)
        national.setdefault(key[1:], []).append(value)
    nat_med = {b: _median(v) for b, v in national.items()}
    cell_med = {k: (_median(v) if len(v) >= 8 else nat_med.get(k[1:]))
                for k, v in cells.items()}

    ratio_rows = []
    for r in rows:
        acres, value = r[7] or 0, r[4]
        if value is None or acres < LAND_COMPARE_MIN_ACRES:
            continue
        key = (r[1], int(r[10] or 0), _land_band(acres))
        med = cell_med.get(key)
        if med and value > 0:
            ratio_rows.append((med, round(med / value, 2), r[0]))

    cur = conn.cursor()
    cur.execute("DELETE FROM regions")
    cur.executemany("""
        INSERT INTO regions (region, country, highlands, n, med_gbp_ac,
                             med_gbp_in, med_gbp_ex, med_acres, med_in_sqft,
                             med_ex_sqft, updated_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, payload)
    cur.execute("UPDATE listings SET region_median = NULL, value_ratio = NULL")
    cur.executemany(
        "UPDATE listings SET region_median = ?, value_ratio = ? WHERE uid = ?",
        ratio_rows)
    conn.commit()
    if verbose:
        print(f"regions refreshed: {len(payload)} regions, "
              f"{len(ratio_rows):,} comparable listings")
    return len(payload)


def backfill_sqft(conn):
    """Derive sqft columns from sqm (and vice versa) where missing."""
    conn.execute("""
        UPDATE listings SET internal_sqft = ROUND(internal_sqm * 10.7639, 0)
        WHERE internal_sqm IS NOT NULL AND internal_sqft IS NULL
    """)
    conn.execute("""
        UPDATE listings SET plot_sqft = ROUND(plot_sqm * 10.7639, 0)
        WHERE plot_sqm IS NOT NULL AND plot_sqft IS NULL
    """)
    conn.commit()


def dedupe_key(lat, lng, postcode, price, beds):
    """Cross-portal fingerprint: same pin + price band + beds."""
    if lat is None or lng is None:
        return None
    try:
        p = f"{round(float(lat), 4)}|{round(float(lng), 4)}"
    except (TypeError, ValueError):
        return None
    pc = re.sub(r"\s+", "", (postcode or "").upper())
    return f"{p}|{pc}|{int(price or 0) // 1000}|{int(beds or 0)}"
