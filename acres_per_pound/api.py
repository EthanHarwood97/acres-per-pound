"""Local API mirroring the Cloudflare Worker (worker/src/index.ts).

Serves /api/search, /api/map, /api/listing/:uid, /api/regions, /api/stats
straight from the canonical SQLite store, so the frontend can run
locally without Cloudflare.
"""

import json
import math
import re

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from . import store as store_mod

# Sort keys must match what the row actually displays. The land columns
# fall back to the registered-plot estimate (est_acres) and hide shared
# titles (est_shared), so sorting on the raw column would rank rows by
# values the user cannot see.
SORTS = {
    "gbp_in": "gbp_per_in_sqft",
    "gbp_ex": "CASE WHEN est_shared=0 THEN gbp_per_ex_sqft END",
    "gbp_ac": "gbp_per_acre",
    "price": "price",
    "in_sqft": "internal_sqft",
    "ex_sqft": "plot_eff_sqm",
    "acres": "acres_eff",
    "beds": "beds",
    "published": "first_published",
    "last_seen": "last_seen",
}

# D1 column name -> SQLite column name (the API speaks D1 names)
_COLS = {
    "in_sqm": "internal_sqm",
    "in_sqft": "internal_sqft",
    "in_src": "internal_source",
    "in_conf": "internal_conf",
    "ex_sqm": "plot_sqm",
    "ex_sqft": "plot_sqft",
    "ex_src": "plot_source",
    "ex_conf": "plot_conf",
    "gbp_in": "gbp_per_in_sqft",
    "gbp_ex": "gbp_per_ex_sqft",
    "gbp_ac": "gbp_per_acre",
    "acres": "acres_mid",
    "region": "region_name",
    "highlands": "is_highlands",
    "type": "subtype",
    "status": "listing_status",
    "published": "first_published",
}

_D1_KEYS = (
    "uid", "source", "url", "address", "postcode", "outcode", "lat", "lng",
    "price", "beds", "baths", "type", "tenure", "land_only",
    "in_sqm", "in_sqft", "in_src", "in_conf", "ex_sqm", "ex_sqft", "ex_src",
    "ex_conf", "acres", "acres_src", "epc", "epc_date", "gbp_ac", "gbp_in",
    "gbp_ex", "region_median", "value_ratio", "plot_id",
    "region", "country", "highlands", "first_seen", "last_seen",
    "status", "published", "sold_price", "sold_date",
)

# Portal subtype labels are inconsistent ("detached", "detached bungalow",
# "link detached house"), so a type filter matches the whole family.
TYPE_GROUPS = {
    "detached": ("detached", "detached bungalow", "detached villa",
                 "link detached house", "link-detached house"),
    "semi-detached": ("semi-detached", "semi-detached bungalow", "semi detached",
                      "link detached house"),
    "terraced": ("terraced", "end of terrace", "terraced bungalow",
                 "end terrace", "mid terrace"),
    "bungalow": ("bungalow", "detached bungalow", "semi-detached bungalow",
                 "terraced bungalow"),
    "house": ("house", "town house", "cottage", "villa", "character property",
              "mews", "link detached house", "end of terrace"),
}


def _row_out(row):
    d = dict(row)
    shared = bool(d.get("est_shared"))
    acres = d.get("acres_eff")
    if acres is not None and acres <= 0:
        acres = None
    # a large shared title (farm/estate the pin happens to land in) is not
    # this property's plot - never publish it as one
    ex_sqm = d.get("plot_eff_sqm")
    ex_sqft = round(ex_sqm * 10.7639, 0) if ex_sqm else None
    ex_src = d.get("plot_eff_src")
    out = {
        "uid": d.get("uid"),
        "source": d.get("source"),
        "url": d.get("url"),
        "address": d.get("address"),
        "postcode": d.get("postcode"),
        "outcode": d.get("outcode"),
        "lat": d.get("lat"),
        "lng": d.get("lng"),
        "price": d.get("price"),
        "beds": d.get("beds"),
        "baths": d.get("baths"),
        "type": d.get("subtype") or d.get("property_type"),
        "tenure": d.get("tenure"),
        "land_only": 1 if d.get("land_only") else 0,
        "in_sqm": d.get("internal_sqm"),
        "in_sqft": d.get("internal_sqft"),
        "in_src": d.get("internal_source"),
        "in_conf": d.get("internal_conf"),
        "ex_sqm": ex_sqm,
        "ex_sqft": ex_sqft,
        "ex_src": ex_src,
        "ex_conf": None if shared else d.get("plot_conf"),
        "acres": acres,
        "acres_src": d.get("confidence"),
        "epc": d.get("epc_rating"),
        "epc_date": d.get("epc_date"),
        "gbp_ac": d.get("gbp_per_acre"),
        "gbp_in": d.get("gbp_per_in_sqft"),
        "gbp_ex": d.get("gbp_per_ex_sqft"),
        "region_median": d.get("region_median"),
        "value_ratio": d.get("value_ratio"),
        "plot_id": d.get("plot_id"),
        "region": d.get("region_name"),
        "country": d.get("country"),
        "highlands": 1 if d.get("is_highlands") else 0,
        "first_seen": d.get("first_seen"),
        "last_seen": d.get("last_seen"),
        "status": d.get("listing_status"),
        "published": d.get("first_published") or d.get("first_seen"),
        "sold_price": d.get("sold_price"),
        "sold_date": d.get("sold_date"),
    }
    return {k: out.get(k) for k in _D1_KEYS}


def _filters(q, price_min, price_max, in_sqft_min, in_sqft_max, ex_sqft_min,
             ex_sqft_max, acres_min, acres_max, beds_min, beds_max, gbp_in_min,
             gbp_in_max, gbp_ex_min, gbp_ex_max, gbp_ac_min, gbp_ac_max, type,
             source, country, region, land_only, has_in, has_ex, no_highlands,
             lat, lng, radius_mi, bbox):
    w, p = ["active = 1", "excluded = 0"], []
    if q:
        w.append("(address LIKE ? OR postcode LIKE ?)")
        p += [f"%{q}%", f"%{q}%"]
    for val, col, op in (
        (price_min, "price", ">="), (price_max, "price", "<="),
        (in_sqft_min, "internal_sqft", ">="), (in_sqft_max, "internal_sqft", "<="),
        (ex_sqft_min, "plot_sqft", ">="), (ex_sqft_max, "plot_sqft", "<="),
        (acres_min, "acres_eff", ">="), (acres_max, "acres_eff", "<="),
        (beds_min, "beds", ">="), (beds_max, "beds", "<="),
        (gbp_in_min, "gbp_per_in_sqft", ">="), (gbp_in_max, "gbp_per_in_sqft", "<="),
        (gbp_ex_min, "gbp_per_ex_sqft", ">="), (gbp_ex_max, "gbp_per_ex_sqft", "<="),
        (gbp_ac_min, "gbp_per_acre", ">="), (gbp_ac_max, "gbp_per_acre", "<="),
    ):
        if val is not None:
            w.append(f"{col} {op} ?")
            p.append(val)
    if type:
        types = [t.strip().lower() for t in type.split(",") if t.strip()]
        clauses = []
        for t in types:
            if t == "land":
                clauses.append("land_only = 1")
            elif t in TYPE_GROUPS:
                subs = TYPE_GROUPS[t]
                clauses.append(f"subtype IN ({','.join('?' * len(subs))})")
                p += list(subs)
            else:
                clauses.append("subtype = ?")
                p.append(t)
        if clauses:
            w.append("(" + " OR ".join(clauses) + ")")
    if source:
        w.append("source = ?")
        p.append(source)
    if country:
        w.append("country = ?")
        p.append(country)
    if region:
        w.append("region_name = ?")
        p.append(region)
    if land_only == "1":
        w.append("land_only = 1")
    elif land_only == "0":
        w.append("land_only = 0")
    if has_in == "1":
        w.append("internal_sqft IS NOT NULL")
    if has_ex == "1":
        # est_shared titles are hidden from the land columns, so they must
        # not count as "has measured land" either
        w.append("plot_sqft IS NOT NULL AND est_shared = 0")
    if no_highlands == "1":
        w.append("is_highlands = 0")
    if bbox:
        try:
            min_lng, min_lat, max_lng, max_lat = [float(x) for x in bbox.split(",")]
            w.append("lng BETWEEN ? AND ? AND lat BETWEEN ? AND ?")
            p += [min_lng, max_lng, min_lat, max_lat]
        except ValueError:
            pass
    if lat is not None and lng is not None and radius_mi is not None:
        d_lat = radius_mi / 69.0
        d_lng = radius_mi / (69.0 * max(0.2, math.cos(math.radians(lat))))
        w.append("lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?")
        p += [lat - d_lat, lat + d_lat, lng - d_lng, lng + d_lng]
    return ("WHERE " + " AND ".join(w) if w else ""), p

def register(app: FastAPI, conn) -> None:
    """Attach /api routes backed by the store connection."""

    def rows(sql, params):
        cur = conn.execute(sql, params)
        names = [c[0] for c in cur.description]
        return [dict(zip(names, r)) for r in cur]

    @app.get("/api/search")
    def search(
        q: str = "", sort: str = "gbp_in", dir: str = "asc", limit: int = 100,
        offset: int = 0, count: int = 0,
        price_min: float | None = None, price_max: float | None = None,
        in_sqft_min: float | None = None, in_sqft_max: float | None = None,
        ex_sqft_min: float | None = None, ex_sqft_max: float | None = None,
        acres_min: float | None = None, acres_max: float | None = None,
        beds_min: float | None = None, beds_max: float | None = None,
        gbp_in_min: float | None = None, gbp_in_max: float | None = None,
        gbp_ex_min: float | None = None, gbp_ex_max: float | None = None,
        gbp_ac_min: float | None = None, gbp_ac_max: float | None = None,
        type: str = "", source: str = "", country: str = "", region: str = "",
        land_only: str = "", has_in: str = "", has_ex: str = "",
        no_highlands: str = "", lat: float | None = None, lng: float | None = None,
        radius_mi: float | None = None, bbox: str = "",
    ):
        where, params = _filters(
            q, price_min, price_max, in_sqft_min, in_sqft_max, ex_sqft_min,
            ex_sqft_max, acres_min, acres_max, beds_min, beds_max, gbp_in_min,
            gbp_in_max, gbp_ex_min, gbp_ex_max, gbp_ac_min, gbp_ac_max, type,
            source, country, region, land_only, has_in, has_ex, no_highlands,
            lat, lng, radius_mi, bbox)
        col = SORTS.get(sort, "gbp_per_in_sqft")
        direction = "DESC" if dir.lower() == "desc" else "ASC"
        sql = (f"SELECT * FROM listings {where} "
               f"ORDER BY ({col} IS NULL) ASC, {col} {direction}, price ASC "
               f"LIMIT ? OFFSET ?")
        data = rows(sql, params + [min(limit, 500), max(offset, 0)])
        out = [_row_out(r) for r in data]
        if lat is not None and lng is not None and radius_mi is not None:
            out = [r for r in out if _within(r, lat, lng, radius_mi)]
        total = None
        if count:
            total = rows(f"SELECT COUNT(*) AS n FROM listings {where}", params)[0]["n"]
        return {"rows": out, "total": total, "limit": limit, "offset": offset}

    @app.get("/api/map")
    def map_points(
        limit: int = 20000,
        q: str = "", sort: str = "gbp_in", dir: str = "asc",
        price_min: float | None = None, price_max: float | None = None,
        in_sqft_min: float | None = None, in_sqft_max: float | None = None,
        ex_sqft_min: float | None = None, ex_sqft_max: float | None = None,
        acres_min: float | None = None, acres_max: float | None = None,
        beds_min: float | None = None, beds_max: float | None = None,
        gbp_in_min: float | None = None, gbp_in_max: float | None = None,
        gbp_ex_min: float | None = None, gbp_ex_max: float | None = None,
        gbp_ac_min: float | None = None, gbp_ac_max: float | None = None,
        type: str = "", source: str = "", country: str = "", region: str = "",
        land_only: str = "", has_in: str = "", has_ex: str = "",
        no_highlands: str = "", lat: float | None = None, lng: float | None = None,
        radius_mi: float | None = None, bbox: str = "",
    ):
        where, p = _filters(
            q, price_min, price_max, in_sqft_min, in_sqft_max, ex_sqft_min,
            ex_sqft_max, acres_min, acres_max, beds_min, beds_max, gbp_in_min,
            gbp_in_max, gbp_ex_min, gbp_ex_max, gbp_ac_min, gbp_ac_max, type,
            source, country, region, land_only, has_in, has_ex, no_highlands,
            lat, lng, radius_mi, bbox)
        data = rows(
            f"SELECT * FROM listings {where} AND lat IS NOT NULL AND lng IS NOT NULL LIMIT ?",
            p + [min(limit, 50000)])
        return {"points": [_row_out(r) for r in data], "limit": limit}

    @app.get("/api/listing/{uid}")
    def listing(uid: str):
        r = rows("SELECT * FROM listings WHERE uid = ?", [uid])
        if not r:
            return JSONResponse({"error": "not found"}, status_code=404)
        out = _row_out(r[0])
        pid = r[0].get("plot_id")
        if pid:
            g = rows("SELECT geojson FROM plot_geom WHERE plot_id = ?", [pid])
            if g and g[0].get("geojson"):
                try:
                    out["plot_geojson"] = json.loads(g[0]["geojson"])
                except Exception:
                    pass
        return {"listing": out}

    @app.get("/api/regions")
    def regions():
        data = rows("""
            SELECT region, country, highlands, n, med_gbp_ac, med_gbp_in,
                   med_gbp_ex, med_acres, med_in_sqft, med_ex_sqft
            FROM regions
            ORDER BY med_gbp_ac ASC NULLS LAST
        """, [])
        return {"regions": data}

    @app.get("/api/stats")
    def stats():
        r = rows("""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN internal_sqft IS NOT NULL THEN 1 ELSE 0 END) AS with_in,
                   SUM(CASE WHEN plot_eff_sqm IS NOT NULL THEN 1 ELSE 0 END) AS with_ex,
                   SUM(CASE WHEN acres_eff IS NOT NULL THEN 1 ELSE 0 END) AS with_acres,
                   SUM(CASE WHEN source='rightmove' THEN 1 ELSE 0 END) AS rightmove,
                   SUM(CASE WHEN source='onthemarket' THEN 1 ELSE 0 END) AS onthemarket,
                   SUM(CASE WHEN source='zoopla' THEN 1 ELSE 0 END) AS zoopla,
                   MIN(price) AS min_price, MAX(price) AS max_price,
                   MAX(last_seen) AS last_seen,
                   -- land and plots never have an EPC, so coverage is
                   -- measured over dwellings only
                   SUM(CASE WHEN land_only = 0 THEN 1 ELSE 0 END) AS dwellings,
                   SUM(CASE WHEN land_only = 0 AND internal_sqft IS NOT NULL
                            THEN 1 ELSE 0 END) AS dwellings_with_in,
                   SUM(CASE WHEN land_only = 0 AND plot_eff_sqm IS NOT NULL
                            THEN 1 ELSE 0 END) AS dwellings_with_ex
            FROM listings WHERE active=1 AND excluded=0
        """, [])
        return {"stats": r[0] if r else {}}


def _within(r, lat, lng, radius_mi):
    if r.get("lat") is None or r.get("lng") is None:
        return False
    d_lat = math.radians(r["lat"] - lat)
    d_lng = math.radians(r["lng"] - lng)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat)) * math.cos(math.radians(r["lat"]))
         * math.sin(d_lng / 2) ** 2)
    return 3958.8 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)) <= radius_mi
