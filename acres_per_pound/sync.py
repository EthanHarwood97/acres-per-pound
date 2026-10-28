"""Publish the canonical store to Cloudflare (D1 search index + R2).

D1 is the searchable slice: one slim row per active listing with the
columns the UI filters and sorts on. R2 keeps the full snapshot
(listings.jsonl.gz) so the index can be rebuilt without re-scraping.

Credentials come from the environment:
  CLOUDFLARE_API_TOKEN   (D1 Edit + R2 Edit on the account)
  CLOUDFLARE_ACCOUNT_ID
or a .env file in the repo root.

Sync is diff-based: a content hash per row is compared against the
`synced_hash` column, so only new/changed rows are written to D1.
"""

import gzip
import hashlib
import json
import os
import pathlib
import time

import httpx

from . import store as store_mod

API = "https://api.cloudflare.com/client/v4"
D1_NAME = "acres-per-pound"
R2_BUCKET = "acres-per-pound"
ENV_FILE = store_mod.REPO_DIR / ".env"

# Columns that go to D1 (short names keep the payload small).
D1_COLUMNS = (
    "uid", "source", "url", "address", "postcode", "outcode",
    "lat", "lng", "price", "beds", "baths", "type", "tenure",
    "land_only", "in_sqm", "in_sqft", "in_src", "in_conf",
    "ex_sqm", "ex_sqft", "ex_src", "ex_conf",
    "acres", "acres_src", "epc", "epc_date",
    "gbp_ac", "gbp_in", "gbp_ex", "region_median", "value_ratio",
    "region", "country", "highlands",
    "first_seen", "last_seen", "status", "published",
    "sold_price", "sold_date", "plot_id",
)

D1_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    uid        TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    url        TEXT,
    address    TEXT,
    postcode   TEXT,
    outcode    TEXT,
    lat        REAL,
    lng        REAL,
    price      INTEGER,
    beds       INTEGER,
    baths      INTEGER,
    type       TEXT,
    tenure     TEXT,
    land_only  INTEGER DEFAULT 0,
    in_sqm     REAL,
    in_sqft    INTEGER,
    in_src     TEXT,
    in_conf    TEXT,
    ex_sqm     REAL,
    ex_sqft    INTEGER,
    ex_src     TEXT,
    ex_conf    TEXT,
    acres      REAL,
    acres_src  TEXT,
    epc        TEXT,
    epc_date   TEXT,
    gbp_ac     REAL,
    gbp_in     REAL,
    gbp_ex     REAL,
    region_median REAL,
    value_ratio   REAL,
    region     TEXT,
    country    TEXT,
    highlands  INTEGER DEFAULT 0,
    first_seen TEXT,
    last_seen  TEXT,
    status     TEXT,
    published  TEXT,
    sold_price INTEGER,
    sold_date  TEXT,
    plot_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_listings_price   ON listings(price);
CREATE INDEX IF NOT EXISTS idx_listings_gbp_in  ON listings(gbp_in);
CREATE INDEX IF NOT EXISTS idx_listings_gbp_ex  ON listings(gbp_ex);
CREATE INDEX IF NOT EXISTS idx_listings_gbp_ac  ON listings(gbp_ac);
CREATE INDEX IF NOT EXISTS idx_listings_region  ON listings(region);
CREATE INDEX IF NOT EXISTS idx_listings_in_sqft ON listings(in_sqft);
CREATE INDEX IF NOT EXISTS idx_listings_ex_sqft ON listings(ex_sqft);
CREATE INDEX IF NOT EXISTS idx_listings_beds    ON listings(beds);
CREATE INDEX IF NOT EXISTS idx_listings_latlng  ON listings(lat, lng);
CREATE INDEX IF NOT EXISTS idx_listings_updated ON listings(last_seen);
CREATE VIRTUAL TABLE IF NOT EXISTS listings_fts USING fts5(
    address, postcode, content='listings', content_rowid='rowid'
);
CREATE TABLE IF NOT EXISTS regions (
    region    TEXT PRIMARY KEY,
    country   TEXT,
    highlands INTEGER DEFAULT 0,
    n         INTEGER,
    med_gbp_ac REAL,
    med_gbp_in REAL,
    med_gbp_ex REAL,
    med_acres REAL,
    med_in_sqft REAL,
    med_ex_sqft REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS plot_geom (
    plot_id TEXT PRIMARY KEY,
    geojson TEXT NOT NULL,
    acres   REAL,
    source  TEXT
);
"""

# Columns added to an existing D1 database. Duplicate-column errors are
# expected on already-migrated databases and are ignored.
D1_ALTERS = (
    "ALTER TABLE listings ADD COLUMN region_median REAL",
    "ALTER TABLE listings ADD COLUMN value_ratio REAL",
    "ALTER TABLE listings ADD COLUMN plot_id TEXT",
)


def load_env():
    """Merge .env into os.environ (no overwrite of real env vars)."""
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _creds():
    load_env()
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not token or not acct:
        raise SystemExit(
            "Cloudflare credentials missing.\n"
            "  Create an API token (D1 Edit + R2 Edit) at\n"
            "  https://dash.cloudflare.com/profile/api-tokens and put in .env:\n"
            "    CLOUDFLARE_API_TOKEN=...\n"
            "    CLOUDFLARE_ACCOUNT_ID=...")
    return token, acct


def _client(token):
    return httpx.Client(
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        timeout=120)


def find_d1(client, acct, name=D1_NAME):
    r = client.get(f"{API}/accounts/{acct}/d1/database")
    r.raise_for_status()
    for db in r.json().get("result") or []:
        if db.get("name") == name:
            return db["uuid"]
    raise SystemExit(f"D1 database {name!r} not found")


def d1_query(client, acct, db_id, sql, params=None):
    r = client.post(f"{API}/accounts/{acct}/d1/database/{db_id}/query",
                    json={"sql": sql, "params": params or []})
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"D1 error: {data.get('errors')} sql={sql[:160]}")
    return data.get("result")


def ensure_schema(client, acct, db_id, verbose=False):
    for stmt in [s.strip() for s in D1_SCHEMA.split(";") if s.strip()]:
        d1_query(client, acct, db_id, stmt)
    for stmt in D1_ALTERS:
        try:
            d1_query(client, acct, db_id, stmt)
        except Exception:
            pass
    if verbose:
        print("  d1 schema ok")


def _sql_val(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _row_to_d1(row):
    shared = bool(row.get("est_shared"))
    acres = row.get("acres_eff")
    return {
        "uid": row["uid"],
        "source": row.get("source") or "rightmove",
        "url": row.get("url"),
        "address": row.get("address"),
        "postcode": row.get("postcode"),
        "outcode": row.get("outcode"),
        "lat": row.get("lat"),
        "lng": row.get("lng"),
        "price": row.get("price"),
        "beds": row.get("beds"),
        "baths": row.get("baths"),
        "type": row.get("subtype") or row.get("property_type"),
        "tenure": row.get("tenure"),
        "land_only": 1 if row.get("land_only") else 0,
        "in_sqm": row.get("internal_sqm"),
        "in_sqft": row.get("internal_sqft"),
        "in_src": row.get("internal_source"),
        "in_conf": row.get("internal_conf"),
        "ex_sqm": row.get("plot_eff_sqm"),
        "ex_sqft": (round(row["plot_eff_sqm"] * 10.7639, 0)
                    if row.get("plot_eff_sqm") else None),
        "ex_src": row.get("plot_eff_src"),
        "ex_conf": None if shared else row.get("plot_conf"),
        "acres": acres,
        "acres_src": row.get("confidence"),
        "epc": row.get("epc_rating"),
        "epc_date": row.get("epc_date"),
        "gbp_ac": row.get("gbp_per_acre"),
        "gbp_in": row.get("gbp_per_in_sqft"),
        "gbp_ex": row.get("gbp_per_ex_sqft"),
        "region_median": row.get("region_median"),
        "value_ratio": row.get("value_ratio"),
        "plot_id": row.get("plot_id"),
        "region": row.get("region_name"),
        "country": row.get("country"),
        "highlands": 1 if row.get("is_highlands") else 0,
        "first_seen": row.get("first_seen"),
        "last_seen": row.get("last_seen"),
        "status": row.get("listing_status"),
        "published": row.get("first_published") or row.get("first_seen"),
        "sold_price": row.get("sold_price"),
        "sold_date": row.get("sold_date"),
    }


def _content_hash(d):
    blob = json.dumps({k: d[k] for k in D1_COLUMNS}, sort_keys=True,
                      ensure_ascii=False, default=str)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


def sync_d1(conn, verbose=False, full=False, batch_rows=200):
    """Push active listings to D1. Returns (changed, written)."""
    token, acct = _creds()
    with _client(token) as client:
        db_id = find_d1(client, acct)
        ensure_schema(client, acct, db_id, verbose=verbose)
        cols = ", ".join(D1_COLUMNS)
        changed = written = 0
        batch = []
        updates = []
        cur = conn.execute("SELECT * FROM listings WHERE active=1 AND excluded=0")
        names = [c[0] for c in cur.description]
        for raw in cur:
            row = dict(zip(names, raw))
            d = _row_to_d1(row)
            h = _content_hash(d)
            if not full and row.get("synced_hash") == h:
                continue
            batch.append(d)
            updates.append((row["uid"], h))
            changed += 1
            if len(batch) >= batch_rows:
                _flush(client, acct, db_id, cols, batch)
                written += len(batch)
                store_mod.update_many(conn, [
                    (uid, {"synced_hash": h}) for uid, h in updates])
                batch, updates = [], []
                if verbose:
                    print(f"    synced {written:,} rows ...")
        if batch:
            _flush(client, acct, db_id, cols, batch)
            written += len(batch)
            store_mod.update_many(conn, [
                (uid, {"synced_hash": h}) for uid, h in updates])
        # drop rows that are no longer active or are out of scope
        conn.execute(
            "UPDATE listings SET synced_hash=NULL WHERE active=0 OR excluded=1")
        conn.commit()
        gone = conn.execute(
            "SELECT uid FROM listings WHERE active=0 OR excluded=1").fetchall()
        for i in range(0, len(gone), 100):
            chunk = [r[0] for r in gone[i:i + 100]]
            vals = ",".join(_sql_val(u) for u in chunk)
            d1_query(client, acct, db_id, f"DELETE FROM listings WHERE uid IN ({vals})")
        if verbose:
            print(f"  d1: {changed:,} changed, {written:,} written")
        return changed, written


def _flush(client, acct, db_id, cols, batch):
    """One multi-row INSERT OR REPLACE per batch, split when D1 says
    SQLITE_TOOBIG (its statement limit is well under a megabyte)."""
    if not batch:
        return
    rows_sql = []
    for d in batch:
        vals = ", ".join(_sql_val(d[c]) for c in D1_COLUMNS)
        rows_sql.append(f"({vals})")
    sql = f"INSERT OR REPLACE INTO listings ({cols}) VALUES " + ", ".join(rows_sql)
    if len(sql) > 85_000 and len(batch) > 1:
        mid = len(batch) // 2
        _flush(client, acct, db_id, cols, batch[:mid])
        _flush(client, acct, db_id, cols, batch[mid:])
        return
    d1_query(client, acct, db_id, sql)


def sync_regions(conn, verbose=False):
    """Push the local region medians (store.refresh_regions) to D1."""
    token, acct = _creds()
    rows = conn.execute("""
        SELECT region, country, highlands, n, med_gbp_ac, med_gbp_in,
               med_gbp_ex, med_acres, med_in_sqft, med_ex_sqft
        FROM regions
        ORDER BY region
    """).fetchall()
    with _client(token) as client:
        db_id = find_d1(client, acct)
        ensure_schema(client, acct, db_id)
        d1_query(client, acct, db_id, "DELETE FROM regions")
        batch = []
        for r in rows:
            vals = ", ".join(_sql_val(v) for v in (
                r[0], r[1], r[2], r[3],
                round(r[4], 1) if r[4] is not None else None,
                round(r[5], 1) if r[5] is not None else None,
                round(r[6], 1) if r[6] is not None else None,
                round(r[7], 3) if r[7] is not None else None,
                round(r[8], 0) if r[8] is not None else None,
                round(r[9], 0) if r[9] is not None else None,
            ))
            batch.append(f"({vals})")
        if batch:
            d1_query(client, acct, db_id,
                     "INSERT INTO regions (region,country,highlands,n,med_gbp_ac,"
                     "med_gbp_in,med_gbp_ex,med_acres,med_in_sqft,med_ex_sqft) VALUES "
                     + ", ".join(batch))
        d1_query(client, acct, db_id,
                 "INSERT OR REPLACE INTO meta (key,value) VALUES ('regions_ts', "
                 + _sql_val(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())) + ")")
    if verbose:
        print(f"  d1 regions: {len(rows)} rows")


def sync_plot_geoms(conn, verbose=False, full=False):
    """Push title boundaries referenced by active listings to D1.

    Geoms are only re-sent when the local geometry changed (the store
    clears `synced_hash` on update), so steady-state syncs are cheap.
    """
    token, acct = _creds()
    q = """
        SELECT g.plot_id, g.geojson, g.acres, g.source
        FROM plot_geom g
        JOIN listings l ON l.plot_id = g.plot_id
        WHERE l.active=1 AND l.excluded=0
    """
    if not full:
        q += " AND g.synced_hash IS NULL"
    rows = conn.execute(q).fetchall()
    if not rows:
        if verbose:
            print("  d1 plot geoms: up to date")
        return 0
    with _client(token) as client:
        db_id = find_d1(client, acct)
        ensure_schema(client, acct, db_id)
        written = 0
        batch, size, ids = [], 0, []
        def flush():
            nonlocal batch, size, ids, written
            if not batch:
                return
            d1_query(client, acct, db_id,
                     "INSERT OR REPLACE INTO plot_geom (plot_id,geojson,acres,source) "
                     "VALUES " + ", ".join(batch))
            conn.execute(
                "UPDATE plot_geom SET synced_hash='pushed' WHERE plot_id IN ("
                + ",".join("?" * len(ids)) + ")", ids)
            conn.commit()
            written += len(batch)
            batch, size, ids = [], 0, []
        for pid, gj, acres, src in rows:
            row_sql = "(" + ", ".join(_sql_val(v) for v in (pid, gj, acres, src)) + ")"
            if batch and size + len(row_sql) > 40_000:
                flush()
            batch.append(row_sql)
            ids.append(pid)
            size += len(row_sql)
        flush()
    if verbose:
        print(f"  d1 plot geoms: {written:,} written")
    return written


def sync_r2_snapshot(conn, verbose=False):
    """Upload a gzipped JSONL snapshot of every listing to R2."""
    token, acct = _creds()
    tmp = store_mod.REPO_DIR / "data" / "snapshot.jsonl.gz"
    n = 0
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        cur = conn.execute("SELECT * FROM listings")
        names = [c[0] for c in cur.description]
        for raw in cur:
            row = dict(zip(names, raw))
            row.pop("features_json", None)
            row.pop("summary", None)
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            n += 1
    key = "snapshot/listings.jsonl.gz"
    with _client(token) as client:
        data = tmp.read_bytes()
        r = client.put(
            f"{API}/accounts/{acct}/r2/buckets/{R2_BUCKET}/objects/{key}",
            content=data,
            headers={"Content-Type": "application/gzip"})
        if not r.json().get("success"):
            raise RuntimeError(f"R2 upload failed: {r.text[:300]}")
    tmp.unlink(missing_ok=True)
    if verbose:
        print(f"  r2: {key} ({len(data)/1e6:.1f} MB, {n:,} rows)")
    return n

