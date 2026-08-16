"""Ranking views + static site builder (GitHub Pages)."""

import json
import pathlib
import shutil

from . import alerts
from .http import load_config

STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "static"
REPO_DIR = pathlib.Path(__file__).resolve().parent.parent

_PUB_FIELDS = (
    "rm_id", "url", "address", "postcode", "lat", "lng", "price", "beds", "subtype",
    "land_only", "acres_min", "acres_max", "acres_mid", "confidence",
    "matched", "listing_status", "first_published", "first_seen",
    "gbp_per_acre", "acres_per_100k", "region_id", "region_name",
    "region_median", "value_ratio", "communal", "verified", "flag",
    "sold_price", "sold_date", "sold_gbp_per_acre", "sold_confidence",
)


def _pub(row):
    return {k: row.get(k) for k in _PUB_FIELDS}


def _state_row(row):
    # minimal persisted row (keeps state.json small + git-diffable)
    keep = (
        "rm_id", "url", "address", "postcode", "lat", "lng", "price", "price_text",
        "beds", "subtype", "land_only", "acres_min", "acres_max", "acres_mid",
        "acre_unit", "confidence", "matched", "first_seen", "last_seen",
        "listing_status", "first_published", "active", "detail_checked",
        "region_id", "region_name", "est_acres", "est_plot_m2", "inspire_id",
        "est_shared", "est_checked", "communal",
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
        corr = corrections.get(str(r.get("rm_id")))
        if corr is not None:
            row = dict(r)
            row["acres_min"] = row["acres_max"] = row["acres_mid"] = corr
            row["confidence"] = "manual"
            row["matched"] = "user-corrected"
            row["communal"] = False
            row.pop("gbp_per_acre", None)
        if row.get("communal"):
            continue  # measured land is shared/communal, not owned
        if row.get("acres_mid") is None and row.get("est_acres"):
            est_floor = float((cfg or load_config()).get("enrich", {}).get("est_min_acres", 0.15))
            if row["est_acres"] < est_floor:
                continue  # tiny registered plots are noise (flats etc.)
            row = dict(row)
            row["acres_min"] = row["acres_max"] = row["acres_mid"] = row["est_acres"]
            row["confidence"] = "est"
            row["matched"] = "registered plot boundary" + (" (shared site)" if row.get("est_shared") else "")
        # cross-check: stated acreage vs registered plot boundary
        if row.get("confidence") != "est" and row.get("acres_mid") and row.get("est_acres"):
            ratio = row["acres_mid"] / max(row["est_acres"], 1e-9)
            if 0.5 <= ratio <= 2.0:
                row["verified"] = True
            else:
                row["flag"] = "stated-vs-plot"
        elif row.get("confidence") in ("partial", "converted"):
            row["flag"] = "low-confidence"
        if row.get("gbp_per_acre") is None and (row.get("price") or 0) >= 1000 and row.get("acres_mid"):
            row["gbp_per_acre"] = round(row["price"] / row["acres_mid"], 2)
        if row.get("acres_per_100k") is None and (row.get("price") or 0) >= 1000 and row.get("acres_mid"):
            row["acres_per_100k"] = round(row["acres_mid"] / (row["price"] / 100000), 3)
        if (row.get("active") is not False
                and row.get("gbp_per_acre") is not None
                and not row.get("est_shared")):
            out.append(row)
    out.sort(key=lambda r: r["gbp_per_acre"])
    return out


def _median(vals):
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _annotate(rows):
    by_reg = {}
    for r in rows:
        by_reg.setdefault(r.get("region_name") or "", []).append(r["gbp_per_acre"])
    for r in rows:
        med = _median(by_reg.get(r.get("region_name") or ""))
        if med:
            r["region_median"] = round(med, 2)
            r["value_ratio"] = round(med / r["gbp_per_acre"], 2) if r["gbp_per_acre"] else None
    return rows


def _region_stats(rows):
    by = {}
    for r in rows:
        by.setdefault(r.get("region_name") or "", []).append(r)
    out = []
    for name, rs in by.items():
        med = _median([r["gbp_per_acre"] for r in rs])
        med_a = _median([r["acres_mid"] for r in rs])
        best = min(rs, key=lambda r: r["gbp_per_acre"])
        out.append({
            "region": name,
            "n": len(rs),
            "land": sum(1 for r in rs if r.get("land_only")),
            "median_gbp": round(med, 0) if med else None,
            "median_acres": round(med_a, 2) if med_a else None,
            "cheapest_gbp": round(best["gbp_per_acre"], 0),
            "cheapest_acres": best["acres_mid"],
            "cheapest_address": best["address"],
            "cheapest_url": best["url"],
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


def views(state, cfg=None):
    rows = ranking(state["listings"], cfg)
    land = _annotate([r for r in rows if r.get("land_only")])
    houses = _annotate([r for r in rows if not r.get("land_only")])
    subtypes = sorted({(r.get("subtype") or "") for r in rows if r.get("subtype")})
    return {
        "ts": state["ts"],
        "stats": state["stats"],
        "meta": {
            "excluded_subtypes": sorted(excluded_subtypes(cfg or load_config())),
            "subtypes": subtypes,
            "max_price": (cfg or {}).get("search", {}).get("max_price", 300000),
        },
        "land": [_pub(r) for r in land],
        "houses": [_pub(r) for r in houses],
        "all": [_pub(r) for r in rows],
        "regions": _region_stats(rows),
        "sold": _sold_view(state["listings"], cfg),
        "events": state.get("events")[-200:] if state.get("events") else [],
    }


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
    (base / "state.json").write_text(
        json.dumps(slim, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if new_events:
        with open(pathlib.Path(events_path), "a", encoding="utf-8") as f:
            for e in new_events:
                f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")


def build_site(state, cfg, site_dir="docs", new_events=()):
    site = pathlib.Path(site_dir)
    if not site.is_absolute():
        site = REPO_DIR / site
    site.mkdir(parents=True, exist_ok=True)
    payload = views(state, cfg)
    (site / "data.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (site / ".nojekyll").write_text("", encoding="utf-8")
    for name in ("index.html", "app.js", "style.css"):
        shutil.copyfile(STATIC_DIR / name, site / name)
    rows = alerts._top_new(state["listings"], new_events, cfg, 10)
    alerts.console_banner(rows)
    alerts.notify(rows, cfg)
    return payload
