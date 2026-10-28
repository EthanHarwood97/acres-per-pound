"""Ranking views + static site builder (GitHub Pages)."""

import json
import pathlib
import shutil
import time

from . import alerts
from .http import load_config
from .regions import classify_region, load_regions

STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "static"
REPO_DIR = pathlib.Path(__file__).resolve().parent.parent
STATIC_ROW_CAP = 20000
# Same floor as store.recompute_metrics: below this a "price" is a POA
# placeholder, not a price, so no ratio may be derived from it.
MIN_PRICE_FOR_METRICS = 10000

_PUB_FIELDS = (
    "uid", "source", "rm_id", "url", "address", "postcode", "lat", "lng",
    "price", "price_text", "price_qualifier", "beds", "baths",
    "floor_sqm", "internal_sqm", "internal_sqft", "internal_source", "internal_conf",
    "plot_sqm", "plot_sqft", "plot_source", "plot_conf",
    "epc_rating", "epc_date", "council_tax", "tenure", "subtype",
    "land_only", "acres_min", "acres_max", "acres_mid", "confidence",
    "matched", "listing_status", "first_published", "first_seen",
    "gbp_per_acre", "acres_per_100k", "gbp_per_in_sqft", "gbp_per_ex_sqft",
    "region_id", "region_name", "country", "is_highlands",
    "region_median", "value_ratio", "communal", "verified", "flag",
    "est_acres", "est_plot_m2",
    "sold_price", "sold_date", "sold_gbp_per_acre", "sold_confidence",
)


def _pub(row):
    return {k: row.get(k) for k in _PUB_FIELDS}


def _state_row(row):
    # minimal persisted row (keeps state.json small + git-diffable)
    keep = (
        "uid", "source", "rm_id", "url", "address", "postcode", "lat", "lng",
        "price", "price_text", "price_qualifier", "beds", "baths",
        "floor_sqm", "internal_sqm", "internal_sqft", "internal_source", "internal_conf",
        "plot_sqm", "plot_sqft", "plot_source", "plot_id", "plot_conf",
        "epc_rating", "epc_date", "council_tax", "tenure", "subtype",
        "land_only", "acres_min", "acres_max", "acres_mid",
        "acre_unit", "confidence", "matched", "first_seen", "last_seen",
        "listing_status", "first_published", "active", "detail_checked",
        "region_id", "region_name", "est_acres", "est_plot_m2", "inspire_id",
        "est_shared", "est_checked", "communal", "uprn",
        "sold_price", "sold_date", "sold_gbp_per_acre", "sold_confidence",
    )
    return {k: v for k in keep if (v := row.get(k)) is not None}


def excluded_subtypes(cfg):
    return set(cfg.get("search", {}).get("exclude_subtypes") or [])


def _load_corrections():
    """Site-wide manual acreage overrides from corrections.json (committed)."""
    p = REPO_DIR / "corrections.json"
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return {str(k): float(v) for k, v in raw.items() if v is not None}


def ranking(listings, cfg=None):
    corrections = _load_corrections()
    out = []
    for r in listings.values():
        row = r
        corr = corrections.get(str(r.get("uid"))) or corrections.get(str(r.get("rm_id")))
        if corr is not None:
            row = dict(r)
            row["acres_min"] = row["acres_max"] = row["acres_mid"] = corr
            row["confidence"] = "manual"
            row["matched"] = "user-corrected"
            row["communal"] = False
            row.pop("gbp_per_acre", None)
        if row.get("communal"):
            continue  # measured land is shared/communal, not owned
        # `acres_eff` is the single acreage the store decided to show:
        # stated acreage when it is credible, else the registered plot
        # (gross title area, house included), minus shared titles. The
        # displayed figure and the £/acre must come from the same column.
        if row.get("acres_eff"):
            enrich_cfg = (cfg or load_config()).get("enrich", {})
            est_floor = float(enrich_cfg.get("est_min_acres", 0.15))
            row = dict(row)
            row["acres_min"] = row["acres_max"] = row["acres_mid"] = row["acres_eff"]
            if row.get("confidence") in (None, "est"):
                row["confidence"] = "est"
                row["matched"] = "registered plot boundary" + (
                    " (shared site)" if row.get("est_shared") else "")
            if not row.get("land_only") and row["acres_eff"] < est_floor:
                row["flag"] = "small_plot"
        # cross-check: stated acreage vs registered plot boundary
        if row.get("confidence") != "est" and row.get("acres_mid") and row.get("est_acres"):
            ratio = row["acres_mid"] / max(row["est_acres"], 1e-9)
            if 0.5 <= ratio <= 2.0:
                row["verified"] = True
            else:
                row["flag"] = "stated-vs-plot"
        elif row.get("confidence") in ("partial", "converted"):
            row["flag"] = "low-confidence"
        if row.get("gbp_per_acre") is None and (row.get("price") or 0) >= MIN_PRICE_FOR_METRICS and row.get("acres_mid"):
            row["gbp_per_acre"] = round(row["price"] / row["acres_mid"], 2)
        if row.get("acres_per_100k") is None and (row.get("price") or 0) >= MIN_PRICE_FOR_METRICS and row.get("acres_mid"):
            row["acres_per_100k"] = round(row["acres_mid"] / (row["price"] / 100000), 3)
        if row.get("gbp_per_in_sqft") is None and (row.get("price") or 0) >= MIN_PRICE_FOR_METRICS and row.get("internal_sqm"):
            row["gbp_per_in_sqft"] = round(row["price"] / (row["internal_sqm"] * 10.7639), 2)
        if row.get("gbp_per_ex_sqft") is None and (row.get("price") or 0) >= MIN_PRICE_FOR_METRICS and row.get("plot_sqm"):
            row["gbp_per_ex_sqft"] = round(row["price"] / (row["plot_sqm"] * 10.7639), 2)

        cls = classify_region(row.get("region_name") or "")
        row["country"] = cls["country"]
        row["is_highlands"] = cls["is_highlands"]

        if (row.get("active") is not False
                and not row.get("est_shared")
                and any(row.get(k) is not None for k in
                        ("gbp_per_acre", "gbp_per_in_sqft", "gbp_per_ex_sqft"))):
            out.append(row)
    out.sort(key=lambda r: r.get("gbp_per_acre") or 1e12)
    return out


def _median(vals):
    s = sorted(v for v in vals if v is not None)
    if not s:
        return None
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _annotate(rows):
    by_reg = {}
    for r in rows:
        v = r.get("gbp_per_acre")
        if v is not None:
            by_reg.setdefault(r.get("region_name") or "", []).append(v)
    meds = {name: _median(v) for name, v in by_reg.items()}
    for r in rows:
        _apply_median(r, meds.get(r.get("region_name") or ""))
    return rows


def _apply_median(r, med):
    if med and r.get("region_median") is None:
        r["region_median"] = round(med, 2)
        r["value_ratio"] = round(med / r["gbp_per_acre"], 2) if r.get("gbp_per_acre") else None
    return r


def _region_stats(rows):
    by = {}
    for r in rows:
        by.setdefault(r.get("region_name") or "", []).append(r)
    out = []
    for name, rs in by.items():
        med = _median([r.get("gbp_per_acre") for r in rs])
        med_a = _median([r.get("acres_mid") for r in rs])
        priced = [r for r in rs if r.get("gbp_per_acre") is not None]
        if not priced:
            continue
        best = min(priced, key=lambda r: r["gbp_per_acre"])
        cls = classify_region(name)
        out.append({
            "region": name,
            "country": cls["country"],
            "is_highlands": cls["is_highlands"],
            "n": len(rs),
            "land": sum(1 for r in rs if r.get("land_only")),
            "median_gbp": round(med, 0) if med else None,
            "median_acres": round(med_a, 2) if med_a else None,
            "cheapest_gbp": round(best["gbp_per_acre"], 0),
            "cheapest_acres": best.get("acres_mid"),
            "cheapest_address": best.get("address"),
            "cheapest_url": best.get("url"),
        })
    out.sort(key=lambda x: x["median_gbp"] if x["median_gbp"] is not None else 1e12)
    return out


def _sold_view(listings, cfg):
    est_floor = float((cfg or load_config()).get("enrich", {}).get("est_min_acres", 0.15))
    sold = []
    for r in listings.values():
        if not r.get("sold_price"):
            continue
        acres = r.get("acres_mid")
        if acres is None and r.get("est_acres"):
            if r["est_acres"] < est_floor:
                continue
            acres = r["est_acres"]
        if not acres:
            continue
        cls = classify_region(r.get("region_name") or "")
        r["country"] = cls["country"]
        r["is_highlands"] = cls["is_highlands"]
        entry = _pub(r)
        entry["acres_mid"] = acres
        entry["acres_min"] = entry["acres_max"] = acres
        if entry.get("sold_gbp_per_acre") is None:
            entry["sold_gbp_per_acre"] = round(r["sold_price"] / acres, 2)
        if r.get("price"):
            entry["discount_pct"] = round((r["sold_price"] / r["price"] - 1) * 100, 1)
        sold.append(entry)
    sold.sort(key=lambda x: x.get("sold_gbp_per_acre") or 1e12)
    return sold


def _slim_house(r):
    """Compact row for the UK-wide 'all houses' view (houses.json).
    URL is rebuilt client-side from rm_id for Rightmove rows; other
    sources carry the full url in 'u'."""
    cls = classify_region(r.get("region_name") or "")
    lat, lng = r.get("lat"), r.get("lng")
    src = r.get("source") or "rightmove"
    out = {
        "i": str(r.get("source_id") or r.get("rm_id")),
        "a": (r.get("address") or "")[:70],
        "p": r.get("price"),
        "b": r.get("beds") or 0,
        "bt": r.get("baths"),
        "f": r.get("floor_sqm"),
        "t": r.get("subtype") or "",
        "la": round(lat, 4) if isinstance(lat, (int, float)) else None,
        "ln": round(lng, 4) if isinstance(lng, (int, float)) else None,
        "r": r.get("region_name") or "",
        "c": cls["country"],
        "am": r.get("acres_eff") or r.get("acres_mid"),
        "ea": None if r.get("est_shared") else r.get("est_acres"),
        "l": bool(r.get("land_only")),
        "s": r.get("listing_status") or "",
        "fp": r.get("first_published") or r.get("first_seen"),
        "is": r.get("internal_sqft"),
        "xs": r.get("plot_sqft"),
        "pis": r.get("gbp_per_in_sqft"),
        "pes": r.get("gbp_per_ex_sqft"),
        "epc": r.get("epc_rating"),
        "ps": r.get("plot_source"),
    }
    if src != "rightmove":
        out["src"] = src
        out["u"] = r.get("url")
    return out


def houses_all_view(listings):
    """Every active listing (houses + land) in slim form for UK-wide comparison."""
    out = []
    for r in listings.values():
        if r.get("active") is False:
            continue
        if not r.get("price"):
            continue
        out.append(_slim_house(r))
    out.sort(key=lambda x: x["p"] or 0)
    return out


def views(state, cfg=None):
    rows = ranking(state["listings"], cfg)
    # Medians stay on comparable stock: tiny overall plots (house + garden +
    # driveway under 0.15ac) join the Houses list but don't move medians.
    core = [r for r in rows if r.get("flag") != "small_plot"]
    land = _annotate([r for r in core if r.get("land_only")])
    houses = _annotate([r for r in core if not r.get("land_only")])
    # data.json is a committed static artifact; the API/D1 carry the full
    # set. Keep the cheapest land first so the cap drops the tail, never
    # the listings this site exists to surface.
    houses = houses[:STATIC_ROW_CAP]
    meds = {}
    for r in houses:
        if r.get("region_median") is not None:
            meds.setdefault(r.get("region_name") or "", r["region_median"])
    small = []
    for r in rows:
        if not r.get("land_only") and r.get("flag") == "small_plot":
            _apply_median(r, meds.get(r.get("region_name") or ""))
            small.append(r)
    houses = (houses + small)[:STATIC_ROW_CAP]
    subtypes = sorted({(r.get("subtype") or "") for r in rows if r.get("subtype")})
    all_regs = load_regions()
    highlands_list = sorted({r["name"] for r in all_regs if classify_region(r)["is_highlands"]})
    return {
        "ts": state["ts"],
        "stats": state["stats"],
        "meta": {
            "excluded_subtypes": sorted(excluded_subtypes(cfg or load_config())),
            "subtypes": subtypes,
            "max_price": (cfg or {}).get("search", {}).get("max_price", 300000),
            "countries": ["England", "Wales", "Scotland"],
            "highlands_regions": highlands_list,
        },
        "land": [_pub(r) for r in land],
        "houses": [_pub(r) for r in houses],
        "all": [_pub(r) for r in rows[:STATIC_ROW_CAP]],
        "regions": _region_stats(core),
        "sold": _sold_view(state["listings"], cfg),
        "events": state.get("events")[-200:] if state.get("events") else [],
    }


def _write_state_gz(path, slim):
    """Write the state gzipped: ~110 MB of JSON is over GitHub's 100 MB
    file limit, and CI commits this file every run."""
    import gzip
    tmp = pathlib.Path(str(path) + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        json.dump(slim, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)


def write_state(state, snapshots_dir, events_path, new_events):
    base = pathlib.Path(snapshots_dir)
    if not base.is_absolute():
        base = REPO_DIR / base
    base.mkdir(parents=True, exist_ok=True)
    slim = {
        "ts": state["ts"],
        "stats": state["stats"],
        "listings": {k: _state_row(r) for k, r in sorted(state["listings"].items())},
    }
    _write_state_gz(base / "state.json.gz", slim)
    if new_events:
        with open(pathlib.Path(events_path), "a", encoding="utf-8") as f:
            for e in new_events:
                f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")


def export_state(conn, snapshots_dir=None, verbose=False):
    """Write the store back to snapshots/state.json.

    The SQLite store lives outside git (it holds the EPC/UPRN indexes),
    so state.json is the portable carrier of enrichment between machines
    and CI runs: enrichment passes update the store, this exports it, and
    the next cycle merges those fields back in.
    """
    from . import store as store_mod

    snaps = pathlib.Path(snapshots_dir or REPO_DIR / "snapshots")
    if not snaps.is_absolute():
        snaps = REPO_DIR / snaps
    snaps.mkdir(parents=True, exist_ok=True)
    listings = {}
    cur = conn.execute("SELECT * FROM listings")
    names = [c[0] for c in cur.description]
    for raw in cur:
        row = dict(zip(names, raw))
        feats = row.pop("features_json", None)
        if feats:
            try:
                row["features"] = json.loads(feats)
            except Exception:
                row["features"] = []
        row = {k: v for k, v in row.items()
               if v is not None and k not in ("synced_hash", "content_hash")}
        listings[row["uid"]] = _state_row(row)
    slim = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "stats": store_mod.counts(conn),
        "listings": dict(sorted(listings.items())),
    }
    _write_state_gz(snaps / "state.json.gz", slim)
    if verbose:
        print(f"exported {len(listings):,} listings to {snaps / 'state.json.gz'}")
    return len(listings)


def build_site(state, cfg, site_dir="docs", new_events=()):
    site = pathlib.Path(site_dir)
    if not site.is_absolute():
        site = REPO_DIR / site
    site.mkdir(parents=True, exist_ok=True)
    payload = views(state, cfg)
    (site / "data.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    houses = houses_all_view(state["listings"])
    (site / "houses.json").write_text(
        json.dumps({"ts": state["ts"], "n": len(houses), "houses": houses},
                   ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (site / ".nojekyll").write_text("", encoding="utf-8")
    for name in ("index.html", "app.js", "style.css"):
        shutil.copyfile(STATIC_DIR / name, site / name)
    rows = alerts._top_new(state["listings"], new_events, cfg, 10)
    alerts.console_banner(rows)
    alerts.notify(rows, cfg)
    return payload

